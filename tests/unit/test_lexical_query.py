"""Trimming a question down to the words BM25 can use.

BM25 scores every term with OR semantics, so a passage matching many common words outscores one
matching the single rare word that identifies the answer. Measured on the real index, same
document, same query engine:

    "Yermarus bid status unit 2"                 -> FGD Installation Status - NCR.xlsx  (right)
    "What was the status of the bid for unit 2
     of the Yermarus Thermal Power Station"      -> TPP.docx                            (wrong)

Twelve function words buried one proper noun. That question was one of four the evaluation set
failed outright; after this it passes, and lexical hit@5 rose from 83% to 89% while the semantic
arm's scores stayed bit-identical — which is the evidence the change stayed on the side it was
meant to.

The two preservation tests are the ones that keep this safe. A trim that eats "Category A" or
"S.O. 3305 (E)" would wreck precisely the questions this corpus exists to answer.
"""

import pytest

from src.modules.retrieval.lexical_query import lexical_query


def terms(question: str) -> list[str]:
    return lexical_query(question).split()


# ==============================================================================
# The failure this exists for
# ==============================================================================

def test_function_words_are_dropped_and_the_proper_noun_survives():
    q = "What was the status of the bid for unit 2 of the Yermarus Thermal Power Station"

    out = terms(q)

    assert "Yermarus" in out
    for noise in ("What", "was", "the", "of", "for"):
        assert noise not in out, f"{noise!r} should have been dropped"


def test_a_question_keeps_its_subject_matter():
    out = terms("what are the FGD installation timelines for Category A plants")

    assert out == ["FGD", "installation", "timelines", "Category", "A", "plants"]


# ==============================================================================
# What must never be trimmed
# ==============================================================================

def test_a_single_capital_letter_is_a_label_not_an_article():
    """"Category A" is a plant classification in this corpus and the whole point of many
    questions. Dropping the A as an article turned it into "Category", which matches every
    category in the index instead of one."""
    assert "A" in terms("which plants are in Category A")
    assert terms("Category B and Category C deadlines") == [
        "Category", "B", "Category", "C", "deadlines"
    ]


def test_a_notification_number_keeps_its_dots():
    """`S.O. 3305 (E)` is how a gazette notification is written. Stripping the trailing dot
    changes the term BM25 matches on."""
    out = lexical_query("What does notification S.O. 3305 (E) dated 07.12.2015 cover?")

    assert "S.O." in out
    assert "3305" in out
    assert "(E)" in out
    assert "07.12.2015" in out


def test_a_lowercase_a_is_still_dropped():
    """The single-letter exemption is for capitals only, or it would keep every article."""
    assert "a" not in terms("what is a thermal power plant emission standard")


# ==============================================================================
# Not making a short question worse
# ==============================================================================

@pytest.mark.parametrize("question", ["why?", "how?", "what about it"])
def test_a_question_too_short_to_trim_is_returned_unchanged(question):
    """Below two surviving terms the trim has taken too much to be trusted. A question that is
    already nothing but function words is better searched as written than as an empty string."""
    assert lexical_query(question) == question


def test_an_already_terse_question_is_left_almost_alone():
    assert terms("penalties for non-compliance") == ["penalties", "non-compliance"]


def test_empty_input_does_not_raise():
    assert lexical_query("") == ""


# ==============================================================================
# The list itself
# ==============================================================================

def test_domain_terms_are_not_stopwords():
    """A stopword list that learns the corpus stops working when the corpus grows. "emission"
    and "compliance" are common here and still discriminating."""
    out = terms("what are the emission compliance thermal norms")

    for word in ("emission", "compliance", "thermal", "norms"):
        assert word in out
