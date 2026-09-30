"""What the gateway did, in a form the caller can be shown.

This is not diagnostics. The architecture's requirement is that a person reading an answer can
see what was withheld from the model that produced it — an answer built from partially redacted
evidence is not the same answer as one built from all of it, and presenting them identically is
the failure this record exists to prevent.
"""

from pydantic import BaseModel, Field


class MaskedValue(BaseModel):
    """One kind of sensitive value that was found, and how often."""

    label: str
    count: int
    # The placeholders that replaced it, e.g. ["<PHONE_1>", "<PHONE_2>"]. Shown so a reader who
    # sees `<PHONE_1>` in the answer can tell it stands for something real that was removed on
    # the way out, rather than for something the model invented.
    placeholders: list[str] = Field(default_factory=list)


class BoundaryRecord(BaseModel):
    """The audit of one crossing: what was sent, what was held back, and on whose authority.

    Written for every outbound request whether or not anything was masked. A record only kept
    when something happened cannot distinguish "nothing sensitive was present" from "the check
    did not run", and those are very different things to discover later.
    """

    # The highest classification across every passage considered. A request takes the highest
    # tier present — seven Internal passages and one Restricted passage is a Restricted request.
    # There is no averaging and no majority rule.
    tier: str = "PUBLIC"

    destination: str = ""
    model: str = ""

    passages_considered: int = 0
    passages_sent: int = 0
    # Withheld because they are RESTRICTED, not because of anything found in them. Counted
    # separately from masking: one is a classification decision, the other is a content scan,
    # and collapsing them would hide which control actually fired.
    passages_withheld_restricted: int = 0

    masked: list[MaskedValue] = Field(default_factory=list)

    @property
    def masked_total(self) -> int:
        return sum(m.count for m in self.masked)

    @property
    def anything_held_back(self) -> bool:
        return bool(self.masked) or self.passages_withheld_restricted > 0
