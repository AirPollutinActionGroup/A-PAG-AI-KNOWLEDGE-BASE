"""Checking an answer against its own passages, without a judge.

RAGAS faithfulness is the general version of this and needs a second model. This is the part
that can be checked by looking, and in a policy corpus that is most of what matters: the facts
here are deadlines, rupee rates, megawatt figures and notification numbers, and an answer that
invents one is wrong in the way that would embarrass someone quoting it in a submission.

The normalisation tests are the ones that keep the score honest. `31st December 2024` and
`31 December 2024` are the same fact, and a checker that called the first one unsupported would
be measuring formatting rather than truth — and would report hallucination where there is none,
which is the failure that gets a check switched off.
"""

from src.modules.generation.fidelity import check

# Parenthesised because an implicit concatenation sitting bare in a list is one missing comma
# away from being several items instead of one, and ruff rejects it for that reason.
CONTEXT = [
    (
        "Category A plants must comply by 31 December 2024, Category B by 31 December 2025 "
        "and Category C by 31 December 2026. Environmental compensation is Rs. 0.20 per unit "
        "for 0-180 days, 0.30 for 181-365 days and 0.40 beyond. Bids have been awarded in 233 "
        "units (1,02,040 MW) of a total 537 units (2,04,160 MW). The Ministry requested an "
        "extension in timelines by 36 months beyond the stipulated dates."
    ),
]


# ==============================================================================
# Numbers — the measure that catches hallucination in this corpus
# ==============================================================================

def test_an_answer_whose_figures_are_all_present_scores_one():
    answer = ("Category A must comply by 31 December 2024 [1], and compensation is "
              "Rs. 0.20 per unit for the first 180 days [1].")

    r = check(answer, CONTEXT)

    assert r.numeric_fidelity == 1.0
    assert r.unsupported_numbers == []


def test_an_invented_figure_is_caught_and_named():
    """The failure this exists for. A model writing 2027 where the passage says 2024 has made
    exactly the error that would be quoted into a government submission."""
    answer = "Category A must comply by 31 December 2027 [1] at Rs. 0.55 per unit [1]."

    r = check(answer, CONTEXT)

    assert r.numeric_fidelity < 0.5
    assert "2027" in r.unsupported_numbers
    assert "0.55" in r.unsupported_numbers


def test_a_fabricated_notification_number_is_caught():
    answer = "This follows notification S.O. 9999 (E) [1]."

    r = check(answer, CONTEXT)

    assert "9999" in r.unsupported_numbers


# ==============================================================================
# Normalisation — the same fact written differently is still the same fact
# ==============================================================================

def test_ordinal_dates_match_plain_ones():
    assert check("Due 31st December 2024 [1].", CONTEXT).numeric_fidelity == 1.0


def test_indian_digit_grouping_matches():
    """`1,02,040` in the answer against `1,02,040` in the passage, and either against the
    ungrouped form. Treating these as different would report fabrication on a correct answer."""
    assert check("Bids cover 1,02,040 MW [1].", CONTEXT).numeric_fidelity == 1.0
    assert check("Bids cover 102040 MW [1].", CONTEXT).numeric_fidelity == 1.0


def test_currency_formatting_does_not_matter():
    assert check("The rate is Rs 0.20 per unit [1].", CONTEXT).numeric_fidelity == 1.0


def test_citation_markers_are_not_treated_as_claims():
    """`[3]` is the system's own apparatus. Counting it as an unsupported figure would make
    every well-cited answer look fabricated."""
    r = check("The deadline is 31 December 2024 [3][7].", CONTEXT)

    assert r.unsupported_numbers == []


def test_small_counting_numbers_are_ignored():
    """"one of the three categories" is prose, not a verifiable figure."""
    r = check("There are 3 categories and 2 deadlines in play [1].", CONTEXT)

    assert r.numbers_checked == 0


def test_an_answer_with_no_figures_scores_none_not_one():
    """None, because nothing was verified. Returning 1.0 would claim a check that never ran."""
    r = check("The Ministry requested an extension [1].", CONTEXT)

    assert r.numeric_fidelity is None


# ==============================================================================
# Citation coverage
# ==============================================================================

def test_uncited_sentences_lower_coverage():
    """A factual sentence with no marker is unverifiable by the reader, which is the same
    problem as a wrong citation wearing a different face."""
    answer = ("Category A must comply by 31 December 2024 [1]. "
              "The Ministry is expected to grant a further extension shortly.")

    r = check(answer, CONTEXT)

    assert r.sentences == 2
    assert r.sentences_cited == 1
    assert r.citation_coverage == 0.5


def test_short_fragments_are_not_counted_as_sentences():
    r = check("Yes. Category A must comply by 31 December 2024 [1].", CONTEXT)

    assert r.sentences == 1


# ==============================================================================
# Quotations
# ==============================================================================

def test_a_verbatim_quotation_passes():
    answer = 'The passage says "requested an extension in timelines by 36 months" [1].'

    assert check(answer, CONTEXT).quote_fidelity == 1.0


def test_a_paraphrase_inside_quotation_marks_is_caught():
    """Presenting a paraphrase as a quotation is fabrication of a specific kind: the reader is
    told these were the document's own words."""
    answer = 'The passage says "demanded an immediate halt to all plant operations" [1].'

    assert check(answer, CONTEXT).quote_fidelity == 0.0


def test_short_quoted_fragments_are_not_checked():
    """A model wrapping `"and status as"` in quote marks for emphasis is not claiming to
    reproduce the document. Scoring those made quote fidelity read as 50% on a real run when
    nothing had been misquoted."""
    r = check('The row shows "and status as" for that unit [1].', CONTEXT)

    assert r.quotes_checked == 0


def test_an_empty_answer_reports_nothing_rather_than_failing():
    r = check("", CONTEXT)

    assert r.numeric_fidelity is None
    assert r.citation_coverage is None


def test_two_short_quotes_in_one_sentence_do_not_pair_across_the_prose():
    """A bug this checker shipped with. With a length bound inside the pattern, a short quoted
    span is skipped by the regex engine, which then pairs its closing mark with the next
    span's opening mark and matches the prose between them:

        "Bid Awarded" [2]. also shows the same project with status "Bid Awarded"
         ^ too short, skipped        ^------- matched as a "quotation" -------^

    Those fragments scored as misquotations and dragged quote fidelity to 50% on a run where
    nothing had been misquoted.
    """
    answer = ('The status is "Bid Awarded" [2]. The sheet also shows the same project and '
              'unit with status "Bid Awarded" [3].')

    r = check(answer, ["The row shows Bid Awarded for unit 1."])

    assert r.quotes_checked == 0, f"paired across the prose: {r.bad_quotes}"
    assert r.bad_quotes == []
