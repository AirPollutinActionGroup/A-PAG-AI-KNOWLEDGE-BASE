"""The Data Boundary Gateway — what may leave, and what is recorded about it.

These are the tests that matter most in this codebase. Everything else here affects whether an
answer is good; these affect whether a phone number, a PAN or a restricted passage reaches a
third party's servers. A regression in retrieval quality is visible to the person searching. A
regression here is visible to nobody until it is far too late.

Two properties are worth stating up front, because they drive most of what follows:

- **Fail closed.** Anything not *explicitly* PUBLIC is withheld. A passage whose classification
  could not be found is not evidence that it is safe to send.
- **The record is always written.** A record kept only when something fired cannot distinguish
  "nothing sensitive was present" from "the scan never ran", and those are opposite facts.
"""

import pytest

from src.core import config
from src.modules.gateway.models import BoundaryRecord
from src.modules.gateway.recognizers import find_all
from src.modules.gateway.service import BoundaryRefusal, DataBoundaryGateway
from src.modules.generation.provider import AnswerProvider


class SpyProvider(AnswerProvider):
    """Records exactly what it was asked to send, which is the whole point of these tests."""

    def __init__(self):
        self.system = None
        self.user = None
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "spy/model"

    def complete(self, system, user):
        self.calls += 1
        self.system, self.user = system, user
        return "An answer.", 100, 20


def gateway():
    spy = SpyProvider()
    return DataBoundaryGateway(provider=spy), spy


def compose(kept):
    """Minimal stand-in for AnswerService's prompt builder."""
    return "\n".join(f"[{i + 1}] {body}" for i, (_, body) in enumerate(kept))


# ==============================================================================
# Classification — the highest tier present, and unknown counts as highest
# ==============================================================================

def test_one_restricted_passage_makes_the_whole_request_restricted():
    """The architecture is explicit: seven Internal passages and one Restricted passage is a
    Restricted request. There is no averaging and no majority rule."""
    assert DataBoundaryGateway.classify(["PUBLIC"] * 7 + ["RESTRICTED"]) == "RESTRICTED"


def test_an_unknown_tier_is_treated_as_the_highest():
    """A passage whose classification nobody recorded is not evidence that it is public."""
    assert DataBoundaryGateway.classify(["PUBLIC", None]) == "RESTRICTED"
    assert DataBoundaryGateway.classify(["PUBLIC", "SOMETHING_NEW"]) == "RESTRICTED"


def test_all_public_is_public():
    assert DataBoundaryGateway.classify(["PUBLIC", "PUBLIC"]) == "PUBLIC"


# ==============================================================================
# What actually crosses
# ==============================================================================

def test_a_restricted_passage_is_never_sent():
    gw, spy = gateway()

    _text, _i, _o, record = gw.send(
        "sys", ["public body", "confidential body"], ["PUBLIC", "RESTRICTED"],
        build_user=compose,
    )

    assert "confidential body" not in spy.user
    assert "public body" in spy.user
    assert record.passages_withheld_restricted == 1
    assert record.passages_sent == 1


def test_a_passage_with_no_known_classification_is_withheld():
    """Fail closed. Matching only the literal string "RESTRICTED" would send a passage whose
    classification could not be looked up, while the record simultaneously called the request
    restricted — the record would say the boundary held when it had not."""
    gw, spy = gateway()

    _t, _i, _o, record = gw.send("sys", ["known", "unknown"], ["PUBLIC", None],
                                 build_user=compose)

    assert "unknown" not in spy.user
    assert record.passages_withheld_restricted == 1


def test_an_entirely_restricted_request_is_refused_without_calling_the_model():
    gw, spy = gateway()

    with pytest.raises(BoundaryRefusal):
        gw.send("sys", ["a", "b"], ["RESTRICTED", "RESTRICTED"], build_user=compose)

    assert spy.calls == 0, "nothing may be attempted"


def test_mismatched_passages_and_tiers_are_refused():
    """Guessing which classification belongs to which passage is how restricted content gets
    sent under a public label."""
    gw, spy = gateway()

    with pytest.raises(BoundaryRefusal):
        gw.send("sys", ["a", "b", "c"], ["PUBLIC"], build_user=compose)

    assert spy.calls == 0


def test_the_caller_is_told_which_passages_survived():
    """Classification can drop passages, so what the model saw is not always what it was
    offered. A citation marker must point at a passage that was really sent."""
    gw, _spy = gateway()
    seen = []

    def capture(kept):
        seen.extend(i for i, _ in kept)
        return compose(kept)

    gw.send("sys", ["a", "b", "c"], ["PUBLIC", "RESTRICTED", "PUBLIC"], build_user=capture)

    assert seen == [0, 2], "indices must be the originals, not renumbered"


# ==============================================================================
# Redaction
# ==============================================================================

def test_a_phone_number_does_not_reach_the_model():
    gw, spy = gateway()

    _t, _i, _o, record = gw.send(
        "sys", ["Call the officer on +91 98765 43210 for the schedule."], ["PUBLIC"],
        build_user=compose,
    )

    assert "98765" not in spy.user
    assert "<PHONE_1>" in spy.user
    assert [(m.label, m.count) for m in record.masked] == [("PHONE", 1)]


def test_the_same_value_gets_the_same_placeholder_across_passages():
    """Otherwise the model is handed what looks like two different people and may reason about
    them as though they were."""
    gw, spy = gateway()

    gw.send("sys",
            ["Reach him on +91 98765 43210.", "Or on +91 98765 43210 after hours."],
            ["PUBLIC", "PUBLIC"], build_user=compose)

    assert spy.user.count("<PHONE_1>") == 2
    assert "<PHONE_2>" not in spy.user


def test_different_values_get_different_placeholders():
    gw, spy = gateway()

    gw.send("sys", ["Primary +91 98765 43210, alternate +91 91234 56789."], ["PUBLIC"],
            build_user=compose)

    assert "<PHONE_1>" in spy.user and "<PHONE_2>" in spy.user


def test_several_values_in_one_passage_are_all_replaced():
    """Replacements run right to left so that removing one does not invalidate the offsets of
    the ones before it — the classic bug in this shape of code."""
    gw, spy = gateway()

    gw.send("sys",
            ["Write to ops@a-pag.org or call +91 98765 43210, PAN ABCDE1234F."],
            ["PUBLIC"], build_user=compose)

    for leaked in ("ops@a-pag.org", "98765", "ABCDE1234F"):
        assert leaked not in spy.user, f"{leaked} reached the model"


def test_the_surrounding_sentence_survives_redaction():
    """A masked answer is still supposed to be an answer."""
    gw, spy = gateway()

    gw.send("sys", ["The nodal officer for FGD compliance is reachable on +91 98765 43210."],
            ["PUBLIC"], build_user=compose)

    assert "nodal officer for FGD compliance" in spy.user


def test_emission_figures_are_not_mistaken_for_identifiers():
    """The false positive that would matter most. A control that mangles the corpus gets turned
    off, and then it protects nothing."""
    gw, spy = gateway()

    body = ("Plant capacity 210 MW, stack height 220 m, emissions 123456789012 tonnes, "
            "Notification No. S.O. 3305 (E) dated 07.12.2015, Section 5 of the Act, 1986.")
    _t, _i, _o, record = gw.send("sys", [body], ["PUBLIC"], build_user=compose)

    assert record.masked == [], f"nothing should have matched: {record.masked}"
    assert "123456789012" in spy.user


def test_redaction_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config.settings, "GATEWAY_REDACT", False)
    gw, spy = gateway()

    _t, _i, _o, record = gw.send("sys", ["Call +91 98765 43210."], ["PUBLIC"],
                                 build_user=compose)

    assert "98765" in spy.user
    assert record.masked == []


# ==============================================================================
# The record
# ==============================================================================

def test_a_record_is_written_even_when_nothing_was_masked():
    """"Nothing sensitive was present" and "the scan never ran" are opposite facts, and a
    record kept only on detection cannot tell them apart."""
    gw, _spy = gateway()

    _t, _i, _o, record = gw.send("sys", ["A wholly unremarkable sentence."], ["PUBLIC"],
                                 build_user=compose)

    assert isinstance(record, BoundaryRecord)
    assert record.passages_considered == 1
    assert record.passages_sent == 1
    assert record.masked == []
    assert record.tier == "PUBLIC"
    assert record.model == "spy/model"


def test_the_record_counts_masking_and_withholding_separately():
    """One is a classification decision and the other is a content scan. Collapsing them would
    hide which control actually fired."""
    gw, _spy = gateway()

    _t, _i, _o, record = gw.send(
        "sys", ["Call +91 98765 43210.", "Confidential."], ["PUBLIC", "RESTRICTED"],
        build_user=compose,
    )

    assert record.passages_withheld_restricted == 1
    assert record.masked_total == 1
    assert record.anything_held_back is True


def test_masked_totals_count_distinct_values():
    gw, _spy = gateway()

    _t, _i, _o, record = gw.send(
        "sys", ["+91 98765 43210 and ops@a-pag.org and +91 91234 56789"], ["PUBLIC"],
        build_user=compose,
    )

    by_label = {m.label: m.count for m in record.masked}
    assert by_label == {"PHONE": 2, "EMAIL": 1}
    assert record.masked_total == 3


# ==============================================================================
# Recognisers — the checksums are what make these usable on this corpus
# ==============================================================================

@pytest.mark.parametrize("text,label", [
    ("ops@a-pag.org", "EMAIL"),
    ("rajesh.kumar@cpcb.nic.in", "EMAIL"),
    ("+91 98765 43210", "PHONE"),
    ("ABCDE1234F", "PAN"),
    ("27ABCDE1234F1Z5", "GSTIN"),
    ("SBIN0001234", "IFSC"),
])
def test_recognised(text, label):
    found = find_all(text)
    assert found and found[0].label == label, f"{text!r} -> {found}"


@pytest.mark.parametrize("text", [
    "123456789012",                      # a tonnage, not an Aadhaar (fails Verhoeff)
    "4567 8901 2345",                    # a docket number
    "210 MW commissioned in 1995",
    "S.O. 3305 (E) dated 07.12.2015",
    "Section 5 of the Act, 1986 (29 of 1986)",
    "1234 5678 9012 3456",               # fails Luhn
])
def test_not_recognised(text):
    assert find_all(text) == [], f"{text!r} matched: {find_all(text)}"


def test_a_gstin_is_not_reported_as_the_pan_inside_it():
    """GSTIN embeds a PAN. Reporting the inner match would both mislabel the value and leave
    the surrounding characters of the GSTIN sitting in the text."""
    found = find_all("GSTIN 27ABCDE1234F1Z5 filed")

    assert [f.label for f in found] == ["GSTIN"]
    assert found[0].text == "27ABCDE1234F1Z5"


def test_a_real_aadhaar_shape_with_a_valid_check_digit_is_caught():
    """Verhoeff-valid, so this is the case the checksum must let through rather than reject."""
    from src.modules.gateway.recognizers import _verhoeff

    candidates = [f"2345 6789 012{d}" for d in range(10)]
    valid = [c for c in candidates if _verhoeff(c)]
    assert valid, "no valid check digit found for the test prefix"

    found = find_all(valid[0])
    assert found and found[0].label == "AADHAAR"
