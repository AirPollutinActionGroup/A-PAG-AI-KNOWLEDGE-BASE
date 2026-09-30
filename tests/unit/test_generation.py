"""Answer generation — the grounding gate, the prompt, and citation verification.

No model is called. A fake provider stands in, because what is under test is everything around
the model: whether it is asked at all, what it is handed, and whether what comes back is allowed
to claim what it claims.

The citation tests are the ones that matter. A model handed passages [1]..[5] that writes [7] has
fabricated provenance, and that is the worst error this system can make — a false statement
carrying a citation, because the citation is what makes a reader stop checking.
"""

import uuid

import pytest

from src.modules.gateway.service import DataBoundaryGateway
from src.modules.generation.provider import AnswerProvider, GenerationError
from src.modules.generation.service import SYSTEM_PROMPT, AnswerService
from src.modules.retrieval.models import RetrievedChunk


class FakeProvider(AnswerProvider):
    def __init__(self, reply: str = "The limit is 50 mg/Nm3 [1].", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    @property
    def model_name(self) -> str:
        return "fake/model"

    def complete(self, system, user):
        if self.fail:
            raise GenerationError("model unreachable")
        self.calls.append((system, user))
        return self.reply, 1200, 60


def chunk(n: int, text: str = "Body text.", **kw) -> RetrievedChunk:
    defaults = {
        "chunk_id": uuid.uuid4(), "document_id": uuid.uuid4(),
        "filename": f"doc{n}.pdf", "text": text, "page_number": n,
        "section_heading": f"{n}. Section", "is_table": False, "score": 0.016,
        "semantic_rank": n, "lexical_rank": None, "similarity": 0.7,
        "token_count": 50, "truncated": False,
    }
    return RetrievedChunk(**{**defaults, **kw})


def service(reply="The limit is 50 mg/Nm3 [1].", fail=False):
    """The service under test, assembled the only way it can be: around a gateway.

    `AnswerService` takes a gateway rather than a provider precisely so there is no arrangement
    in which the model is reachable without crossing the boundary — including in a test, where
    a convenience constructor that skipped it would quietly stop exercising the thing that
    matters.
    """
    provider = FakeProvider(reply=reply, fail=fail)
    return AnswerService(gateway=DataBoundaryGateway(provider=provider)), provider


def public(n: int) -> list[str]:
    """`n` passages classified PUBLIC.

    Spelled out in every call rather than defaulted, because the gateway withholds anything not
    explicitly public — a test that omitted this would pass for the wrong reason, having sent
    nothing at all.
    """
    return ["PUBLIC"] * n


# ==============================================================================
# The model is not asked when there is nothing to answer from
# ==============================================================================

def test_an_ungrounded_question_never_reaches_the_model():
    """Both cheaper and safer. A model handed no relevant passages still writes a confident
    paragraph — that is what they do — so the protection is not calling it."""
    svc, provider = service()

    result = svc.answer("what is the best cake recipe", [chunk(1)], grounded=False)

    assert provider.calls == [], "no request should have been made"
    assert result.grounded is False
    assert result.answer == ""
    assert result.input_tokens == 0, "an uncalled model costs nothing"


def test_no_passages_means_no_call_even_if_marked_grounded():
    svc, provider = service()

    result = svc.answer("anything", [], grounded=True, tiers=public(99))

    assert provider.calls == []
    assert result.grounded is False


# ==============================================================================
# What the model is handed
# ==============================================================================

def test_passages_are_numbered_and_labelled_with_their_source():
    """The model attributes better when it can see that passage 2 is a different document from
    passage 3, and the reader sees the same labels in the citation list."""
    svc, provider = service()

    svc.answer("q", [chunk(1, "First."), chunk(2, "Second.")], grounded=True, tiers=public(99))

    _system, user = provider.calls[0]
    assert "[1] (doc1.pdf · p.1 · 1. Section)" in user
    assert "[2] (doc2.pdf · p.2 · 2. Section)" in user
    assert "First." in user and "Second." in user


def test_the_question_is_included_after_the_passages():
    svc, provider = service()

    svc.answer("what are the limits", [chunk(1)], grounded=True, tiers=public(99))

    _system, user = provider.calls[0]
    assert user.index("[1]") < user.index("what are the limits"), "context first, then the ask"


def test_the_system_prompt_forbids_outside_knowledge():
    svc, provider = service()

    svc.answer("q", [chunk(1)], grounded=True, tiers=public(99))

    system, _user = provider.calls[0]
    assert system == SYSTEM_PROMPT
    assert "only the passages" in system.lower()


def test_passages_are_capped(monkeypatch):
    """More context is not free: it costs per token, dilutes attention, and past a point lowers
    answer quality rather than raising it."""
    from src.core import config
    monkeypatch.setattr(config.settings, "GENERATION_MAX_PASSAGES", 3)
    svc, provider = service()

    svc.answer("q", [chunk(i) for i in range(1, 11)], grounded=True, tiers=public(99))

    _system, user = provider.calls[0]
    assert "[3]" in user
    assert "[4]" not in user


# ==============================================================================
# Citation verification — the safety-critical part
# ==============================================================================

def test_only_cited_passages_are_returned():
    """Returning all ten retrieved passages would make the reader hunt for the two the answer
    actually used."""
    svc, _ = service(reply="Limits are set in [2] and enforced under [4].")

    result = svc.answer("q", [chunk(i) for i in range(1, 6)], grounded=True, tiers=public(99))

    assert [c.marker for c in result.citations] == [2, 4]
    assert result.citations[0].filename == "doc2.pdf"


def test_a_fabricated_citation_is_stripped_and_reported():
    """The worst error this system can make. The model was given three passages and cited a
    seventh; leaving [7] in the text would attach real-looking provenance to an invented claim."""
    svc, _ = service(reply="Emissions fell sharply [7] and limits tightened [2].")

    result = svc.answer("q", [chunk(1), chunk(2), chunk(3)], grounded=True, tiers=public(99))

    assert result.invalid_markers == [7]
    assert "[7]" not in result.answer, "the marker must not survive into the answer"
    assert [c.marker for c in result.citations] == [2], "only the real one is cited"


def test_stripping_a_marker_leaves_clean_text():
    """A removed marker must not leave a doubled space or a gap before punctuation — the answer
    is read by people, and the repair should be invisible."""
    svc, _ = service(reply="Emissions fell [9] , then rose [1] .")

    result = svc.answer("q", [chunk(1)], grounded=True, tiers=public(99))

    assert "  " not in result.answer
    assert " ," not in result.answer and " ." not in result.answer


def test_a_marker_at_the_boundary_is_valid():
    """Off-by-one here would either reject a real citation or accept a fabricated one."""
    svc, _ = service(reply="See [3].")

    result = svc.answer("q", [chunk(1), chunk(2), chunk(3)], grounded=True, tiers=public(99))

    assert result.invalid_markers == []
    assert [c.marker for c in result.citations] == [3]


def test_zero_is_not_a_valid_marker():
    svc, _ = service(reply="See [0].")

    result = svc.answer("q", [chunk(1)], grounded=True, tiers=public(99))

    assert result.invalid_markers == [0]
    assert result.citations == []


def test_a_repeated_citation_is_listed_once():
    svc, _ = service(reply="It says [1]. It also says [1] elsewhere.")

    result = svc.answer("q", [chunk(1), chunk(2)], grounded=True, tiers=public(99))

    assert [c.marker for c in result.citations] == [1]


def test_citations_carry_full_provenance():
    """A citation the reader cannot follow is decoration."""
    svc, _ = service(reply="Stated in [1].")
    source = chunk(1, "The limit is 50 mg/Nm3.")

    result = svc.answer("q", [source], grounded=True, tiers=public(99))

    c = result.citations[0]
    assert c.chunk_id == source.chunk_id
    assert c.document_id == source.document_id
    assert c.filename == source.filename
    assert c.page_number == source.page_number
    assert c.section_heading == source.section_heading
    assert c.text == source.text


def test_an_answer_with_no_citations_still_returns():
    """The model may legitimately say the passages do not answer the question. That is a useful
    answer and must not be treated as a failure."""
    svc, _ = service(reply="The passages do not state a deadline.")

    result = svc.answer("q", [chunk(1)], grounded=True, tiers=public(99))

    assert result.grounded is True
    assert result.citations == []
    assert "do not state" in result.answer


# ==============================================================================
# Failure and cost
# ==============================================================================

def test_a_provider_failure_is_raised_not_swallowed():
    """A model outage must not be indistinguishable from an empty corpus."""
    svc, _ = service(fail=True)

    with pytest.raises(GenerationError):
        svc.answer("q", [chunk(1)], grounded=True, tiers=public(99))


def test_token_usage_and_cost_are_reported():
    """A per-question cost is what decides whether this scales to the whole organisation."""
    svc, _ = service()

    result = svc.answer("q", [chunk(1)], grounded=True, tiers=public(99))

    assert result.input_tokens == 1200
    assert result.output_tokens == 60
    assert result.cost_inr == pytest.approx((1200 / 1e6) * 29.28 + (60 / 1e6) * 73.2)
    assert result.cost_inr < 0.1, "a question should cost well under a rupee"
