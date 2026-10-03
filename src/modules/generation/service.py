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
from src.modules.gateway.service import BoundaryRefusal, DataBoundaryGateway
from src.modules.generation.models import Citation, GeneratedAnswer
from src.modules.generation.provider import GenerationError
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
    """Composes the prompt, sends it **through the boundary gateway**, and verifies what comes
    back.

    It takes a gateway rather than a provider deliberately. A redaction step that callers invoke
    politely is not a boundary; a boundary is one that cannot be gone around because there is no
    other route to the model. Holding the gateway here means no future caller can add a second
    path out by accident.
    """

    def __init__(self, gateway: DataBoundaryGateway):
        self._gateway = gateway

    @staticmethod
    def build_context(chunks: list[RetrievedChunk], texts: list[str] | None = None) -> str:
        """Numbers the passages and labels each with where it came from.

        The source line is included deliberately: the model does better at attributing a claim
        when it can see that passage 2 is a different document from passage 3, and a reader
        comparing the answer against the citation list sees the same labels.
        """
        bodies = texts if texts is not None else [c.text for c in chunks]
        parts = []
        for i, (c, body) in enumerate(zip(chunks, bodies, strict=True), 1):
            where = [c.filename]
            if c.page_number is not None:
                where.append(f"p.{c.page_number}")
            if c.section_heading:
                where.append(c.section_heading)
            # `body` is the redacted text when the gateway supplied one. The citation metadata
            # around it is never redacted: a filename and a page number are how a reader checks
            # the claim, and masking those would defeat the point of citing at all.
            parts.append(f"[{i}] ({' · '.join(where)})\n{body}")
        return "\n\n".join(parts)

    def answer(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        *,
        grounded: bool,
        tiers: list[str | None] | None = None,
    ) -> GeneratedAnswer:
        """Writes an answer from `chunks`, sending nothing that the boundary would not allow.

        `tiers` is each passage's classification, positionally aligned with `chunks`. It has no
        default value on purpose at the call site: omitting it here means "unknown", and the
        gateway treats unknown as the highest tier rather than the lowest — a passage whose
        classification nobody recorded is not evidence that it is public.
        """
        if not grounded or not chunks:
            # Deliberately no model call. See rule 1.
            return GeneratedAnswer(
                question=question, grounded=False, model=self._gateway.model_name
            )

        usable = chunks[: settings.GENERATION_MAX_PASSAGES]
        supplied = list(tiers or [None] * len(chunks))[: len(usable)]

        # What the model was actually shown, which is not always what it was offered:
        # classification can drop passages at the boundary, and a citation marker has to point
        # at a passage that was really sent. Filled in by `compose` below, from the indices the
        # gateway reports back.
        sent: list[RetrievedChunk] = []

        def compose(kept: list[tuple[int, str]]) -> str:
            sent.clear()
            sent.extend(usable[i] for i, _ in kept)
            bodies = [body for _, body in kept]
            return (
                f"{self.build_context(sent, bodies)}\n\n"
                f"---\n\nQuestion: {question}"
            )

        try:
            text, in_tokens, out_tokens, record = self._gateway.send(
                SYSTEM_PROMPT, [c.text for c in usable], supplied, build_user=compose,
            )
        except BoundaryRefusal as e:
            # Not a failure: the boundary did its job. Reported as ungrounded rather than as an
            # error, because from the reader's side the honest statement is "I have nothing I
            # can answer this from", and saying so is the same contract as an empty corpus.
            logger.info("Boundary refused the request for %r: %s", question[:80], e)
            return GeneratedAnswer(
                question=question, grounded=False, model=self._gateway.model_name,
                excluded_restricted=len(usable),
            )
        except GenerationError:
            raise
        except Exception as e:
            raise GenerationError(f"Generation failed: {e}") from e

        cleaned, citations, invalid = self._verify_citations(text, sent)

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
            model=self._gateway.model_name,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            boundary=record,
            excluded_restricted=record.passages_withheld_restricted,
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
