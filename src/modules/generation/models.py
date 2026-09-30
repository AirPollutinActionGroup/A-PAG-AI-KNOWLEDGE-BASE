"""Data contracts for answer generation."""

import uuid

from pydantic import BaseModel, Field, computed_field

from src.modules.gateway.models import BoundaryRecord


class Citation(BaseModel):
    """A passage the answer actually drew on.

    Carries the same provenance the retrieval layer produced, because the point of the whole
    system is that a claim can be checked against the document it came from. An answer without
    these is a paragraph of plausible text.
    """

    marker: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    page_number: int | None = None
    section_heading: str | None = None
    text: str


class GeneratedAnswer(BaseModel):
    """What the model wrote, and what it is allowed to claim.

    `answer` is empty when `grounded` is false: the model is not asked at all when retrieval found
    nothing close enough, which saves the call and removes the opportunity to invent.
    """

    question: str
    answer: str = ""
    grounded: bool = True
    citations: list[Citation] = Field(default_factory=list)

    # Markers the model produced that pointed at no real passage. The model is handed passages
    # numbered [1]..[n]; a [7] when it was given five is fabricated provenance, and the most
    # dangerous kind of error this system can make — a false claim wearing a citation. They are
    # stripped from the answer and counted here so the failure is visible rather than silent.
    invalid_markers: list[int] = Field(default_factory=list)

    # Passages the caller was entitled to see but which were withheld from the model because
    # they are RESTRICTED. The reader must be told: an answer built from six of ten passages is
    # not the answer to their question, and silently returning it is the kind of omission that
    # looks like the corpus being thin.
    excluded_restricted: int = 0

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    # What the boundary did on the way out: the tier the request took, how many passages were
    # withheld, and what was masked. Carried on the answer rather than logged alone, because the
    # person reading the answer is the one who needs to know it was built from redacted
    # evidence — an answer containing <PHONE_1> should be explainable without reading a log.
    boundary: BoundaryRecord | None = None

    @computed_field
    @property
    def cost_inr(self) -> float:
        """Rupees for this answer, at Sarvam's published rates.

        `@computed_field`, not a bare `@property`: pydantic does not serialise plain properties,
        so this was absent from every API response while appearing to work in Python. The UI
        read it as missing and showed nothing.

        `output_tokens` is Sarvam's `completion_tokens`, which **includes the reasoning the
        model does before writing** — usually most of it. That is correct for cost, because
        reasoning is billed, but it means the number is not "tokens of answer".
        """
        return (self.input_tokens / 1e6) * 29.28 + (self.output_tokens / 1e6) * 73.2
