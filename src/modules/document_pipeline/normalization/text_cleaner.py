"""Text cleaning — the artifacts that come out of real document extraction.

Every rule here fixes something that demonstrably happens when text is pulled out of a document,
and nothing here rewrites content. In particular it does not strip punctuation, lowercase, or
remove stopwords: this text is destined for retrieval and for a person to read in a citation, and
all of those would damage it.
"""

import re
import unicodedata

# Characters with no visual content that survive extraction and corrupt matching later:
# zero-width space/non-joiner/joiner, BOM, and the soft hyphen PDFs use for optional breaks.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b‌‍﻿­"), None)

# C0 control characters, minus the three that are real text: tab, newline, carriage return.
# These are not cosmetic. Postgres rejects NUL in a text value outright -- "A string literal
# cannot contain NUL (0x00) characters" -- so a single stray byte from a malformed PDF fails the
# INSERT, exhausts the job's retries, and leaves the document stuck with an error naming storage
# rather than the character that caused it. One document in the first real folder did exactly
# that. The rest go with it: a form feed or a bell in the middle of a cited passage is
# extraction debris either way.
_CONTROL = dict.fromkeys(
    [c for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)] + [0x7F], None
)

# A word broken across a line by hyphenation ("manage-\nment"). Only rejoined when the next line
# continues in lowercase — "Delhi-\nNCR" and "Section 4-\nA" must survive intact.
_LINE_BREAK_HYPHEN = re.compile(r"(\w)-\n([a-z])")

_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_REPEATED_SPACE = re.compile(r"[ \t]{2,}")
# Three or more newlines collapse to two — one blank line is a paragraph break, more is noise.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


class TextCleaner:
    """Normalizes extracted text without altering what it says."""

    def clean(self, text: str) -> str:
        if not text:
            return ""

        # NFKC folds compatibility forms — ligatures ("ﬁ" -> "fi"), full-width characters, and
        # non-breaking spaces — so the same word matches itself regardless of how it was encoded.
        text = unicodedata.normalize("NFKC", text)
        text = text.translate(_INVISIBLE)
        text = text.translate(_CONTROL)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _LINE_BREAK_HYPHEN.sub(r"\1\2", text)
        text = _REPEATED_SPACE.sub(" ", text)
        text = _TRAILING_SPACE.sub("", text)
        text = _EXCESS_BLANK_LINES.sub("\n\n", text)
        return text.strip()
