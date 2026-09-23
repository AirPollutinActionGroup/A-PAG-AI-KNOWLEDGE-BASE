"""Structure-aware chunking.

Cuts a document at its own headings rather than at fixed intervals, because chunk boundaries
decide two things at once: what the embedding model sees as a single idea, and what a citation
can point at. A fixed-size cut through the middle of a clause separates an obligation from the
condition that qualifies it — retrieval still returns something, and it is confidently wrong.

The structure is free. Extraction already recovered it from each format's own declarations: Word
paragraph styles, PowerPoint title placeholders, worksheet grids, and for PDF a font-size
heuristic. This stage inherits that and never re-derives it.

Sizing is in characters, not tokens, because the tokenizer belongs to the embedding model and
that arrives a stage later. The budget is deliberately conservative: Devanagari runs roughly 2-3x
more tokens per character than English, so a target tuned on English prose would silently
overflow the model's window on Hindi documents. The failure mode there is truncation at embed
time, surfacing months later as unexplained poor retrieval on Hindi content.
"""

import re

from src.core.config import settings
from src.modules.document_pipeline.chunking.models import SECTION_SCALE, Chunk
from src.modules.document_pipeline.extraction.models import ExtractedTable, Heading
from src.modules.document_pipeline.normalization.models import (
    NormalizationResult,
    NormalizedUnit,
)

# Split points in descending order of how much meaning the boundary carries. A hard character
# wrap is the last resort and only fires on a single run of text with no paragraph or sentence
# break in it, which in practice means a pathological document.
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class Chunker:
    """Splits a normalized document into retrievable passages."""

    def __init__(
        self,
        target_chars: int | None = None,
        max_chars: int | None = None,
    ):
        self.target_chars = target_chars or settings.CHUNK_TARGET_CHARS
        self.max_chars = max_chars or settings.CHUNK_MAX_CHARS

    def chunk(self, result: NormalizationResult) -> list[Chunk]:
        chunks: list[Chunk] = []
        tables_by_unit = self._tables_by_unit(result.tables)
        headings_by_unit = self._headings_by_unit(result.headings)

        # Carried across units: a section started by a heading on page 3 still governs the prose
        # that runs onto page 4, and a citation should keep naming that section.
        current_heading: str | None = None

        for unit in result.units:
            for heading, body in self._split_unit_by_headings(
                unit, headings_by_unit.get(unit.index, [])
            ):
                if heading is not None:
                    current_heading = heading
                for piece in self._split_to_size(body):
                    chunks.append(
                        Chunk(
                            index=len(chunks),
                            text=piece,
                            page_number=unit.index,
                            section_heading=current_heading,
                            is_table=False,
                            scale=SECTION_SCALE,
                        )
                    )

            # A table's position *within* a unit is not recorded — extraction gives it only a
            # unit index. Where a unit carries several headings we therefore cannot tell which
            # section the table sat under, and must say so: a DOCX is a single unit, so guessing
            # would silently attribute every table to the document's last heading. A wrong
            # citation is worse than an absent one. See KNOWN_DEBTS.md #19.
            unit_headings = headings_by_unit.get(unit.index, [])
            table_heading = None if len(unit_headings) > 1 else current_heading

            for table in tables_by_unit.get(unit.index, []):
                for piece in self._render_table(table):
                    chunks.append(
                        Chunk(
                            index=len(chunks),
                            text=piece,
                            page_number=unit.index,
                            section_heading=table_heading,
                            is_table=True,
                            scale=SECTION_SCALE,
                        )
                    )

        return chunks

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    @staticmethod
    def _tables_by_unit(tables: list[ExtractedTable]) -> dict[int, list[ExtractedTable]]:
        grouped: dict[int, list[ExtractedTable]] = {}
        for table in tables:
            grouped.setdefault(table.unit_index, []).append(table)
        return grouped

    @staticmethod
    def _headings_by_unit(headings: list[Heading]) -> dict[int, list[Heading]]:
        grouped: dict[int, list[Heading]] = {}
        for heading in headings:
            grouped.setdefault(heading.unit_index, []).append(heading)
        return grouped

    def _split_unit_by_headings(
        self, unit: NormalizedUnit, headings: list[Heading]
    ) -> list[tuple[str | None, str]]:
        """Cuts one unit's text into (heading, body) sections.

        Headings record which unit they appear in but not where inside it, so each one is located
        by finding its text in the body. A heading that cannot be found — cleaning may have
        altered spacing, or the extractor may have inferred it from font size alone — is skipped
        rather than guessed at, which degrades to unit-level chunking instead of cutting in the
        wrong place.
        """
        text = unit.text.strip()
        if not text:
            return []
        if not headings:
            return [(None, text)]

        cuts: list[tuple[int, Heading]] = []
        search_from = 0
        for heading in headings:
            needle = heading.text.strip()
            if not needle:
                continue
            at = text.find(needle, search_from)
            if at == -1:
                continue
            cuts.append((at, heading))
            search_from = at + len(needle)

        if not cuts:
            return [(None, text)]

        sections: list[tuple[str | None, str]] = []
        # Prose before the first heading belongs to whatever section was already open.
        if cuts[0][0] > 0:
            lead = text[: cuts[0][0]].strip()
            if lead:
                sections.append((None, lead))

        for i, (at, heading) in enumerate(cuts):
            end = cuts[i + 1][0] if i + 1 < len(cuts) else len(text)
            body = text[at:end].strip()
            if body:
                sections.append((heading.text.strip(), body))

        return sections

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def _split_to_size(self, text: str) -> list[str]:
        """Recursively splits on the strongest boundary available until pieces fit."""
        text = text.strip()
        if not text:
            return []
        if len(text) <= self.max_chars:
            return [text]

        for pattern in (_PARAGRAPH_BREAK, _SENTENCE_END):
            parts = [p.strip() for p in pattern.split(text) if p.strip()]
            if len(parts) > 1:
                return self._pack(parts)

        # No usable boundary — a single enormous run of text. Hard wrap.
        return [
            text[i : i + self.max_chars].strip()
            for i in range(0, len(text), self.max_chars)
        ]

    def _pack(self, parts: list[str]) -> list[str]:
        """Greedily recombines parts up to the target, so splitting doesn't shatter a section
        into fragments far smaller than they need to be."""
        packed: list[str] = []
        buffer = ""
        for part in parts:
            if len(part) > self.max_chars:
                if buffer:
                    packed.append(buffer)
                    buffer = ""
                packed.extend(self._split_to_size(part))
                continue
            candidate = f"{buffer}\n\n{part}" if buffer else part
            if len(candidate) > self.target_chars and buffer:
                packed.append(buffer)
                buffer = part
            else:
                buffer = candidate
        if buffer:
            packed.append(buffer)
        return packed

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def _render_table(self, table: ExtractedTable) -> list[str]:
        """Renders a table as text, split into row groups if it is large.

        Never split mid-row, and every group repeats the header. A 500-row budget sheet as one
        chunk would blow past any embedding window; split naively, chunk 7 is a wall of numbers
        with no idea what the columns mean. Repeating the header keeps each group a readable
        table in its own right.
        """
        rows = [
            [("" if cell is None else str(cell)).strip() for cell in row]
            for row in table.rows
            if row
        ]
        if not rows:
            return []

        header, body = rows[0], rows[1:]
        header_line = " | ".join(header)
        if not body:
            return [header_line]

        groups: list[str] = []
        buffer: list[str] = []
        budget = self.max_chars - len(header_line)

        for row in body:
            line = " | ".join(row)
            projected = sum(len(b) + 1 for b in buffer) + len(line)
            if buffer and projected > budget:
                groups.append("\n".join([header_line, *buffer]))
                buffer = []
            buffer.append(line)

        if buffer:
            groups.append("\n".join([header_line, *buffer]))
        return groups
