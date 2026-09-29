"""Turns retrieved passages into a written answer that can be checked.

Three rules do the work here, and all three exist to make a wrong answer visible rather than
plausible.

**1. The model is not asked when retrieval found nothing.** The grounding gate runs first. A model
handed no relevant passages and a question will still write a confident paragraph — that is what
they do. Not calling it is both cheaper and safer than calling it and hoping the prompt holds.

**2. The prompt supplies the only permitted source.** Passages are numbered and the model is told
to answer from them alone and to mark each claim. Prompts are not a guarantee, which is why:

**3. Every citation is verified against the passages actually supplied.** The model is handed
[1]..[n]; a [7] when it was given five is fabricated provenance. Those markers are stripped from
the answer text and reported separately. This is the check that matters most: a false claim
carrying a citation is worse than no answer, because the citation is what makes a reader stop
checking.
"""

import logging
import re

from src.core.config import settings
from src.modules.generation.models import Citation, GeneratedAnswer
from src.modules.generation.provider import AnswerProvider, GenerationError
from src.modules.retrieval.models import RetrievedChunk

logger = logging.getLogger(__name__)

_MARKER = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT = """You answer questions for A-PAG, an Indian air quality policy organisation, \
using only the numbered passages supplied with each question.

Rules:
- Use only the passages. Do not use anything you know from outside them.
- Mark every factual claim with the passage it came from, like [2]. A sentence drawing on two \
passages carries both, like [1][3].
- If the passages do not answer the question, say exactly what is missing. Do not fill the gap.
- Quote figures, dates and section numbers exactly as the passages give them.
- Be brief and plain. No preamble, no restating the question, no closing summary.
- Write in the language the question was asked in."""


class AnswerService:
    """Composes the prompt, calls the provider, and verifies what comes back."""

    def __init__(self, provider: AnswerProvider):
        self._provider = provider

    @staticmethod
    def build_context(chunks: list[RetrievedChunk]) -> str:
        """Numbers the passages and labels each with where it came from.

        The source line is included deliberately: the model does better at attributing a claim
        when it can see that passage 2 is a different document from passage 3, and a reader
        comparing the answer against the citation list sees the same labels.
        """
        parts = []
        for i, c in enumerate(chunks, 1):
            where = [c.filename]
            if c.page_number is not None:
                where.append(f"p.{c.page_number}")
            if c.section_heading:
                where.append(c.section_heading)
            parts.append(f"[{i}] ({' · '.join(where)})\n{c.text}")
        return "\n\n".join(parts)

    def answer(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        *,
        grounded: bool,
    ) -> GeneratedAnswer:
        if not grounded or not chunks:
            # Deliberately no model call. See rule 1.
            return GeneratedAnswer(
                question=question, grounded=False, model=self._provider.model_name
            )

        usable = chunks[: settings.GENERATION_MAX_PASSAGES]
        user = (
            f"{self.build_context(usable)}\n\n"
            f"---\n\nQuestion: {question}"
        )

        try:
            text, in_tokens, out_tokens = self._provider.complete(SYSTEM_PROMPT, user)
        except GenerationError:
            raise
        except Exception as e:
            raise GenerationError(f"Generation failed: {e}") from e

        cleaned, citations, invalid = self._verify_citations(text, usable)

        if invalid:
            logger.warning(
                "Model cited %d passage(s) it was not given (%s) for question %r",
                len(invalid), invalid, question[:80],
            )

        return GeneratedAnswer(
            question=question,
            answer=cleaned,
            grounded=True,
            citations=citations,
            invalid_markers=invalid,
            model=self._provider.model_name,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )

    @staticmethod
    def _verify_citations(
        text: str, chunks: list[RetrievedChunk]
    ) -> tuple[str, list[Citation], list[int]]:
        """Strips markers that point at nothing, and returns only the passages actually cited.

        Returning only cited passages, rather than everything retrieved, is what lets a reader
        check the answer without reading ten passages to find the two it used.
        """
        valid_range = range(1, len(chunks) + 1)
        seen: list[int] = []
        invalid: list[int] = []

        for match in _MARKER.finditer(text):
            n = int(match.group(1))
            if n in valid_range:
                if n not in seen:
                    seen.append(n)
            elif n not in invalid:
                invalid.append(n)

        cleaned = text
        for n in invalid:
            cleaned = cleaned.replace(f"[{n}]", "")
        # Removing a marker can leave doubled spaces or a space before punctuation.
        cleaned = re.sub(r" {2,}", " ", cleaned)
        cleaned = re.sub(r" +([.,;:])", r"\1", cleaned).strip()

        citations = [
            Citation(
                marker=n,
                chunk_id=chunks[n - 1].chunk_id,
                document_id=chunks[n - 1].document_id,
                filename=chunks[n - 1].filename,
                page_number=chunks[n - 1].page_number,
                section_heading=chunks[n - 1].section_heading,
                text=chunks[n - 1].text,
            )
            for n in sorted(seen)
        ]
        return cleaned, citations, sorted(invalid)
