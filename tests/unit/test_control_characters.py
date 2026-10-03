"""Control characters in extracted text.

Not a cosmetic concern. Postgres rejects NUL in a text value outright -- "A string literal cannot
contain NUL (0x00) characters" -- so one stray byte out of a malformed PDF fails the chunk
INSERT, exhausts the job's three retries, and leaves the document stuck behind an error that
names storage rather than the character responsible. A document in A-PAG's first real folder did
exactly that, and the message gave no hint where to look.

The tab/newline exception is the part worth pinning: those are real text here. A table's cell
boundaries survive as tabs into the passage a person eventually reads in a citation.
"""

from src.modules.document_pipeline.normalization.text_cleaner import TextCleaner


def clean(text: str) -> str:
    return TextCleaner().clean(text)


def test_nul_is_removed():
    """The one that actually broke a document."""
    assert "\x00" not in clean("Section 5\x00 of the Act")


def test_the_surrounding_text_survives():
    """Stripping the byte must not take the sentence with it."""
    out = clean("Directions under Section 5\x00 of the Environment (Protection) Act, 1986")
    assert "Section 5" in out
    assert "Environment (Protection) Act, 1986" in out


def test_other_c0_controls_are_removed():
    """A form feed or a bell inside a cited passage is extraction debris."""
    out = clean("page one\x0cpage two\x07bell\x1bescape")
    assert not any(c in out for c in "\x0c\x07\x1b")
    assert "page one" in out and "page two" in out


def test_delete_is_removed():
    assert "\x7f" not in clean("before\x7fafter")


def test_tab_and_newline_are_kept():
    """These are real text. A table's cell boundaries reach the reader as tabs, and paragraph
    structure is newlines — removing either would damage the passage a citation points at."""
    out = clean("Plant\tCapacity\tStatus\nUnchahar\t210 MW\tPending")

    assert "\t" in out and "\n" in out
    assert out.count("\t") == 4


def test_a_document_of_only_control_characters_comes_back_empty():
    """Rather than as whitespace that later looks like content. An empty result is what the
    quality gate is built to catch; a string of spaces is not."""
    assert clean("\x00\x01\x02\x0c") == ""


def test_cleaning_is_idempotent():
    text = "Section 5\x00 of the Act\x0c, 1986"
    assert clean(clean(text)) == clean(text)
