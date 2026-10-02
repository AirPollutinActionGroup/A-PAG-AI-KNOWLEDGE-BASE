"""Checking an answer against the passages it was built from, without a judge.

RAGAS faithfulness asks a second model to decompose an answer into claims and verify each one.
That is the general method and it needs an LLM. But in this corpus the facts *are* the numbers:
a deadline, a rupee rate, a megawatt figure, a section reference, a notification number. An
answer that invents one of those is wrong in the way that matters, and it can be caught by
looking, because the figure either appears in the supplied passages or it does not.

So this measures three things that need no judgement at all:

**Numeric fidelity** — every figure in the answer must appear in the passages the model was
given. This is the one that catches hallucination in a policy corpus. "Category B by 31 December
2025" is checkable; a model that writes 2026 instead has made exactly the error that would
embarrass someone quoting it in a submission.

**Citation coverage** — what share of the answer's sentences carry a marker. A sentence making
a factual claim with no citation is unverifiable by the reader, which is the same problem as a
wrong citation wearing a different face.

**Quote fidelity** — any span in quotation marks must appear verbatim in a passage. A model
that paraphrases inside quotation marks has fabricated a quotation.

What this is **not**: a replacement for a model judge. It cannot see a claim that is wrong
without being numerically wrong — "the Ministry opposed the extension" when the passage says it
requested one uses no figures at all. The two measures answer different questions, and the
honest position is that this one is cheap, deterministic and runs on every change, while the
other is better and costs money.
"""

import re
from dataclasses import dataclass, field

# Markers like [3] are the system's own apparatus, not claims the model made.
_MARKER = re.compile(r"\[\d+\]")

# Figures worth checking: years, amounts, percentages, section and notification numbers,
# measurements. Deliberately not every digit -- "one of the three categories" is prose.
_NUMBER = re.compile(
    r"""
    (?<![\w.])
    (?:
        \d{1,3}(?:,\d{2,3})+(?:\.\d+)?     # Indian grouping: 1,02,040
      | \d+\.\d+                            # 0.20
      | \d{4,}                              # a year, or any larger plain integer: 102040
                                              # written without separators still has to be
                                              # checked, or a model can state an unverified
                                              # figure simply by omitting the commas
      | \d{1,3}(?:st|nd|rd|th)?             # 31st, 20
    )
    (?![\w])
    """,
    re.VERBOSE,
)

# Spans the model presented as quotation.
#
# No length bound in the pattern, and that is the point. With `{25,200}` inside it, a short
# quoted span is skipped by the engine, which then pairs that span's *closing* mark with the
# *opening* mark of the next one and matches the prose in between:
#
#     "Bid Awarded" [2]. also shows the same project with status "Bid Awarded"
#      ^ too short, skipped        ^------- matched as a "quotation" -------^
#
# Those inter-quote fragments then scored as misquotations and dragged quote fidelity to 50%
# on a run where nothing had been misquoted. Pair every mark here; filter by length afterwards.
_QUOTED = re.compile(r"[“\"]([^”\"]*)[”\"]")

# Below this a quoted span is a fragment the model wrapped for emphasis -- `"and status as"` --
# rather than something it claims to be reproducing from the document.
_MIN_QUOTE_CHARS = 25

_SENTENCE = re.compile(r"(?<=[.!?])\s+")

# Numbers that carry no factual weight on their own. Checking them produces noise: a model
# writing "one" or "the first" is not making a verifiable numeric claim.
_TRIVIAL = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}


def _normalise(text: str) -> str:
    """Flattens the ways the same figure gets written, so a real match is not missed.

    `31st December 2024` and `31 December 2024`, `Rs. 0.20` and `0.20`, `1,02,040` and
    `102040` are the same fact. Without this the score measures formatting rather than truth.
    """
    t = text.lower()
    t = t.replace(",", "")
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    t = re.sub(r"\s+", " ", t)
    return t


@dataclass
class FidelityReport:
    """What could be checked, and what failed."""

    numbers_checked: int = 0
    numbers_supported: int = 0
    unsupported_numbers: list[str] = field(default_factory=list)

    sentences: int = 0
    sentences_cited: int = 0

    quotes_checked: int = 0
    quotes_verbatim: int = 0
    bad_quotes: list[str] = field(default_factory=list)

    @property
    def numeric_fidelity(self) -> float | None:
        """None, not 1.0, when the answer contained no checkable figure. A score of 1 would
        claim a verification that never happened."""
        if not self.numbers_checked:
            return None
        return self.numbers_supported / self.numbers_checked

    @property
    def citation_coverage(self) -> float | None:
        if not self.sentences:
            return None
        return self.sentences_cited / self.sentences

    @property
    def quote_fidelity(self) -> float | None:
        if not self.quotes_checked:
            return None
        return self.quotes_verbatim / self.quotes_checked


def check(answer: str, contexts: list[str]) -> FidelityReport:
    """Checks one answer against the passages it was given."""
    report = FidelityReport()
    if not answer:
        return report

    haystack = _normalise(" \n ".join(contexts))
    stripped = _MARKER.sub(" ", answer)

    # --- numbers -------------------------------------------------------------------
    seen = set()
    for match in _NUMBER.finditer(stripped):
        raw = match.group(0)
        key = _normalise(raw)
        if key in _TRIVIAL or key in seen:
            continue
        seen.add(key)
        report.numbers_checked += 1
        if key in haystack:
            report.numbers_supported += 1
        else:
            report.unsupported_numbers.append(raw)

    # --- citation coverage ---------------------------------------------------------
    for sentence in _SENTENCE.split(answer):
        s = sentence.strip()
        # Fragments and bare list bullets are not claims.
        if len(s) < 25:
            continue
        report.sentences += 1
        if _MARKER.search(s):
            report.sentences_cited += 1

    # --- quotations ----------------------------------------------------------------
    for match in _QUOTED.finditer(answer):
        quoted = _MARKER.sub(" ", match.group(1)).strip()
        if len(quoted) < _MIN_QUOTE_CHARS:
            continue
        report.quotes_checked += 1
        if _normalise(quoted) in haystack:
            report.quotes_verbatim += 1
        else:
            report.bad_quotes.append(quoted[:90])

    return report
