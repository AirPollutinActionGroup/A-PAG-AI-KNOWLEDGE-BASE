"""Deciding whether a detected language is worth believing.

Language detection needs sentences. Given a grid it answers anyway, and confidently: a
130,000-character emissions spreadsheet whose text is `em  country  units  X2000  X2001 ...`
was detected as **Croatian**, skipped as unsupported, and took 460 chunks out of the search
index with no signal beyond a status nobody was watching.

The measure must be script-agnostic, and that is the part that is easy to get wrong. A first
version counted runs of three or more letters, which works in English and scores Devanagari at
0.034 — matras are combining marks and break the run. That would have let a genuine Hindi
document past the gate and embedded it as noise, which is exactly what the gate exists to stop.
So this counts whitespace-delimited tokens and never looks inside them.
"""

import pytest

from src.modules.document_pipeline.embedding_job_handler import prose_ratio

ENGLISH = ("The Ministry of Power has requested an extension in timelines by thirty six months "
           "for category A, B and C thermal power plants.")
HINDI = ("केंद्रीय प्रदूषण नियंत्रण बोर्ड ने तापीय विद्युत संयंत्रों के लिए उत्सर्जन मानकों के "
         "अनुपालन हेतु निर्देश जारी किए हैं। यह अधिसूचना पर्यावरण संरक्षण अधिनियम के तहत जारी की गई है।")
GRID = ("em\tcountry\tunits\tX2000\tX2001\tX2002\n"
        "1\t2\tkt\t1.23\t4.56\t7.89\n2\t3\tkt\t9.1\t2.3\t4.5\n4\t5\tkt\t1.1\t2.2\t3.3")

THRESHOLD = 0.15


def test_prose_scores_well_above_the_threshold():
    assert prose_ratio(ENGLISH) > 0.5


def test_a_non_latin_script_is_not_mistaken_for_a_grid():
    """Not Hindi support — this protects the skip-gate that already exists.

    Counting letter runs scores Devanagari 0.034, so a non-English document would have looked
    like a spreadsheet, been treated as "language unknown, embed it anyway", and gone into the
    index as noise. The existing design records it as SKIPPED instead, which keeps it a
    findable backlog. This test is what stops that flipping silently."""
    assert prose_ratio(HINDI) == pytest.approx(prose_ratio(ENGLISH), abs=0.15)


def test_a_grid_of_numbers_scores_below_the_threshold():
    assert prose_ratio(GRID) < THRESHOLD


def test_empty_text_is_zero_not_a_crash():
    assert prose_ratio("") == 0.0


def test_tokens_containing_digits_do_not_count_as_words():
    """`X2000` is a column header, not a word. Counting it would drag a spreadsheet's score up
    towards a document's."""
    assert prose_ratio("X2000 X2001 X2002 X2003") < THRESHOLD


def test_very_short_tokens_do_not_count():
    """Codes and units — `kt`, `em`, `t` — are what a grid is mostly made of."""
    assert prose_ratio("kt em t kt em t kt em t") < THRESHOLD


def test_the_real_spreadsheet_shape_is_well_clear_of_the_threshold():
    """The measured file scored 0.021 against 0.309 for the thinnest real document. This pins
    the margin rather than the exact number, so a future tweak that narrows it fails here
    rather than in production."""
    header = "em\tcountry\tunits\t" + "\t".join(f"X{y}" for y in range(2000, 2022))
    row = "\t".join(["1", "2", "kt"] + [f"{i}.{i}" for i in range(22)])
    text = header + "\n" + "\n".join(row for _ in range(200))

    assert prose_ratio(text) < THRESHOLD / 2
