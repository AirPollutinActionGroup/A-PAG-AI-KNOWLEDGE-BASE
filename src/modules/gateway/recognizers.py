"""What counts as sensitive, and how it is recognised.

Every recogniser here is a **deterministic pattern**, not a model. That is a deliberate choice
and it has a cost: a person's name in running prose is not detected, because a name has no shape.
What it buys is that every detection is reproducible and testable — the same passage always
redacts the same way, a rule that fires wrongly can be pointed at, and the whole set runs in
microseconds with nothing added to the image. For a control whose job is to be *demonstrable*,
"it matched this pattern" is worth more than a probability.

Most of these are India-specific, because a generic PII library is built around US identifiers
and would miss Aadhaar, PAN and GSTIN entirely while flagging nothing that matters here.

**Checksums are used where the format has one.** Aadhaar and credit cards carry a Verhoeff or
Luhn check digit, and a 12-digit number that fails Verhoeff is a tonnage figure or a docket
number, not an Aadhaar. Without that test this would redact half the numbers in an emissions
table — a control that mangles the corpus gets switched off, which protects nothing.

Ordering matters: recognisers run in the order listed and earlier matches win the span. GSTIN
contains a PAN inside it, so GSTIN must be tried first or every GSTIN would be reported as a PAN.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Recognizer:
    """One kind of sensitive value: what it is called, how to find it, how to confirm it."""

    label: str
    pattern: re.Pattern[str]
    # Second-stage test on the matched text. Patterns alone are too loose for anything that is
    # "a run of digits"; this is where a checksum or a range check rejects the false positives.
    validate: Callable[[str], bool] | None = None
    description: str = ""


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _luhn(text: str) -> bool:
    """The check digit every payment card carries."""
    digits = [int(d) for d in _digits(text)]
    if len(digits) < 12:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff(text: str) -> bool:
    """UIDAI's check digit for Aadhaar. Without it, every 12-digit figure in an emissions table
    looks like an identity number."""
    digits = _digits(text)
    if len(digits) != 12:
        return False
    # An Aadhaar never begins 0 or 1 — UIDAI reserves those.
    if digits[0] in "01":
        return False
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(d)]]
    return c == 0


def _plausible_landline(text: str) -> bool:
    """An Indian landline: STD code starting 0, then the subscriber number, ten digits in all.

    Added because a real document got through. `Tel:011-23063746` -- an Under Secretary's direct
    line in a Ministry of Power memorandum -- was sent to Sarvam untouched, while the UI said
    "no phone numbers, emails or identifiers were found". The mobile pattern only matches
    numbers starting 6-9, and government correspondence is full of landlines.

    The digit-count test is what keeps this off the corpus. `0.20` per unit, `0-180 days` and
    `05.09.2022` all begin with a zero and none of them reach eleven digits.
    """
    digits = _digits(text)
    # `+91-11-23063746` carries the country code where the trunk zero would be.
    if digits.startswith("91") and len(digits) == 12:
        digits = "0" + digits[2:]
    # Trunk zero plus ten digits. The count is what keeps this off the corpus: `0.20` per
    # unit, `0-180 days` and `05.09.2022` all begin with a zero and none reach eleven digits.
    return len(digits) == 11 and digits[0] == "0" and digits[1] != "0"


def _plausible_phone(text: str) -> bool:
    """Indian mobile numbers are ten digits starting 6-9. The leading-digit test is what stops a
    ten-digit tonnage or a year range being read as a phone number."""
    digits = _digits(text)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return len(digits) == 10 and digits[0] in "6789"


# Ordering is load-bearing: GSTIN embeds a PAN, so it must be tried first.
RECOGNIZERS: tuple[Recognizer, ...] = (
    Recognizer(
        label="GSTIN",
        # 2-digit state code, PAN, entity digit, 'Z', checksum character.
        pattern=re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b"),
        description="Goods and Services Tax identification number",
    ),
    Recognizer(
        label="PAN",
        pattern=re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
        description="Permanent Account Number",
    ),
    Recognizer(
        label="AADHAAR",
        pattern=re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
        validate=_verhoeff,
        description="Aadhaar number (Verhoeff-checked)",
    ),
    Recognizer(
        label="CREDIT_CARD",
        pattern=re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
        validate=_luhn,
        description="Payment card number (Luhn-checked)",
    ),
    Recognizer(
        label="PHONE",
        # Landlines. Tried before the mobile pattern because an STD code starting 0 would
        # otherwise have its leading zero eaten by the mobile rule's optional `\b0`, leaving a
        # partial match that masks most of the number and leaves the rest in the text.
        # Two separator groups, because `011 2306 3746` and `011-23063746` are the same number
        # written two ways and both turn up in government correspondence. The pattern is
        # deliberately loose; `_plausible_landline` does the filtering, on digit count.
        pattern=re.compile(r"(?:\+91[\s-]?)?0?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,5}\b"),
        validate=_plausible_landline,
        description="Indian landline number",
    ),
    Recognizer(
        label="IFSC",
        pattern=re.compile(r"\b[A-Z]{4}0[0-9A-Z]{6}\b"),
        description="Bank branch code",
    ),
    Recognizer(
        label="EMAIL",
        # The domain must allow several labels. A single `[\w-]+\.[A-Za-z]{2,}` stops at the
        # first label/TLD pair, so `rajesh@cpcb.nic.in` masked as `rajesh@cpcb.nic` and left
        # `.in` sitting in the text — government addresses here are routinely three deep.
        pattern=re.compile(r"\b[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"),
        description="Email address",
    ),
    Recognizer(
        label="PHONE",
        # Requires a separator or a country code before a bare 10-digit run, so a plain number
        # in a table is not swept up; `_plausible_phone` then checks the leading digit.
        pattern=re.compile(
            r"(?:\+91[\s-]?|\b0)?[6-9]\d{4}[\s-]?\d{5}\b"
            r"|\+91[\s-]?\d{10}\b"
        ),
        validate=_plausible_phone,
        description="Indian mobile number",
    ),
)


@dataclass(frozen=True)
class Finding:
    """One detected value and where it sat."""

    label: str
    start: int
    end: int
    text: str


def find_all(text: str) -> list[Finding]:
    """Every detection in `text`, left to right, non-overlapping.

    Overlaps are resolved by recogniser order rather than by length: a GSTIN contains a PAN, and
    reporting the inner PAN would both mislabel the value and leave the surrounding characters
    of the GSTIN in the text.
    """
    if not text:
        return []

    findings: list[Finding] = []
    claimed: list[tuple[int, int]] = []

    for rec in RECOGNIZERS:
        for m in rec.pattern.finditer(text):
            start, end = m.start(), m.end()
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            value = m.group(0)
            if rec.validate is not None and not rec.validate(value):
                continue
            claimed.append((start, end))
            findings.append(Finding(label=rec.label, start=start, end=end, text=value))

    findings.sort(key=lambda f: f.start)
    return findings
