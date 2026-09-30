"""The Data Boundary Gateway — the one place anything leaves for an external model.

The architecture names this as the central control, and its value comes entirely from being
*the only route*. A redaction function called politely by whoever remembers is not a boundary;
a boundary is a function that cannot be bypassed because there is no other way out. So
`AnswerService` takes a gateway rather than a provider, and the provider is reachable only
through it.

Three things happen here, in this order, and the order matters:

**1. Classify.** A request takes the highest tier present across every passage. Seven Internal
passages and one Restricted passage is a Restricted request — no averaging, no majority rule.
Restricted content does not go to external inference at all, so those passages are dropped
before anything else looks at them.

**2. Redact.** Every surviving passage is scanned for sensitive values and they are replaced
with typed placeholders. Numbering is consistent across the whole request: the same phone
number appearing in three passages becomes `<PHONE_1>` in all three, so the model can still
reason about "the first number" and a reader can still follow the reference. Replacements are
applied right-to-left so earlier offsets stay valid as the string changes length.

**3. Record.** What was sent, what was held back, and what was masked — always, not only when
something fired. A record kept only on detection cannot distinguish "nothing was there" from
"the scan did not run".

The placeholders deliberately survive into the answer. A model handed `<PHONE_1>` will write
`<PHONE_1>`, and that is the desired outcome: the reader sees exactly where a real value was
removed, instead of a fluent sentence with a plausible invented number in it.
"""

import logging

from src.core.config import settings
from src.db.enums import Classification
from src.modules.gateway.models import BoundaryRecord, MaskedValue
from src.modules.gateway.recognizers import find_all
from src.modules.generation.provider import AnswerProvider

logger = logging.getLogger(__name__)

# Highest wins. Kept as an explicit order rather than inferred from the enum, so adding a tier
# is a deliberate decision about where it sits rather than an accident of declaration order.
_TIER_ORDER = {Classification.PUBLIC.value: 0, Classification.RESTRICTED.value: 1}


class BoundaryRefusal(RuntimeError):
    """The request may not cross. Distinct from a model outage: nothing was attempted."""


class DataBoundaryGateway:
    """Wraps an external provider so nothing reaches it unclassified and unredacted."""

    def __init__(self, provider: AnswerProvider):
        self._provider = provider

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    @staticmethod
    def classify(tiers: list[str | None]) -> str:
        """The highest tier present. An unknown or missing tier is treated as the highest, not
        the lowest: a passage whose classification nobody recorded is not evidence that it is
        public."""
        highest = Classification.PUBLIC.value
        for tier in tiers:
            if tier is None or tier not in _TIER_ORDER:
                return Classification.RESTRICTED.value
            if _TIER_ORDER[tier] > _TIER_ORDER[highest]:
                highest = tier
        return highest

    @staticmethod
    def redact(texts: list[str]) -> tuple[list[str], list[MaskedValue]]:
        """Replaces sensitive values with typed placeholders across a whole request.

        Numbering is shared across passages on purpose. The same number appearing in three
        passages must read as the same number, or the model is handed what looks like three
        different people and may reason accordingly.
        """
        # label -> {original value: placeholder}, so a repeat gets the number it had before.
        assigned: dict[str, dict[str, str]] = {}
        order: dict[str, list[str]] = {}

        redacted: list[str] = []
        for text in texts:
            findings = find_all(text)
            if not findings:
                redacted.append(text)
                continue

            out = text
            # Right to left: replacing left to right would shift every later offset.
            for finding in sorted(findings, key=lambda f: f.start, reverse=True):
                seen = assigned.setdefault(finding.label, {})
                if finding.text not in seen:
                    seen[finding.text] = f"<{finding.label}_{len(seen) + 1}>"
                    order.setdefault(finding.label, []).append(seen[finding.text])
                placeholder = seen[finding.text]
                out = out[: finding.start] + placeholder + out[finding.end :]
            redacted.append(out)

        masked = [
            MaskedValue(label=label, count=len(values), placeholders=order.get(label, []))
            for label, values in assigned.items()
        ]
        masked.sort(key=lambda m: (-m.count, m.label))
        return redacted, masked

    def send(
        self,
        system: str,
        passages: list[str],
        tiers: list[str | None],
        *,
        build_user: "callable",
    ) -> tuple[str, int, int, BoundaryRecord]:
        """Classifies, redacts, sends, and records. The only way to reach the provider.

        `build_user` is handed `[(original_index, redacted_text), ...]` and composes the user
        message from it — the caller owns the prompt format, the gateway owns what may appear in
        it. Passing a composed prompt in instead would mean redacting a string the gateway did
        not build, where an offset computed against one passage means nothing.

        The indices matter as much as the texts. Classification can drop passages, so what the
        model is shown is not always what it was offered, and a citation marker must point at
        the passage that was actually sent. Handing back only the surviving texts would leave
        the caller renumbering by guesswork.
        """
        if len(passages) != len(tiers):
            raise BoundaryRefusal(
                f"{len(passages)} passages against {len(tiers)} classifications — refusing to "
                "guess which is which."
            )

        record = BoundaryRecord(
            destination=getattr(self._provider, "ENDPOINT", self._provider.model_name),
            model=self._provider.model_name,
            passages_considered=len(passages),
        )

        # Step 1: classification. Restricted passages do not go to external inference at all.
        kept: list[str] = []
        kept_tiers: list[str | None] = []
        kept_indices: list[int] = []
        for i, (text, tier) in enumerate(zip(passages, tiers, strict=True)):
            # Anything not *explicitly* PUBLIC is withheld, which includes a tier that is None
            # or unrecognised. Matching only the literal string "RESTRICTED" would send a
            # passage whose classification could not be found, while `classify()` below
            # simultaneously recorded the request as restricted — the record would have said
            # the boundary held, and it would not have. Fail closed: an unrecorded
            # classification is not evidence that something is safe to send.
            if (
                not settings.GENERATION_INCLUDE_RESTRICTED
                and tier != Classification.PUBLIC.value
            ):
                record.passages_withheld_restricted += 1
                continue
            kept.append(text)
            kept_tiers.append(tier)
            kept_indices.append(i)

        record.tier = self.classify(kept_tiers) if kept else Classification.PUBLIC.value

        if not kept:
            raise BoundaryRefusal(
                "Every passage was restricted; nothing may be sent to an external model."
            )

        # Step 2: redaction.
        if settings.GATEWAY_REDACT:
            kept, record.masked = self.redact(kept)

        # Step 3: the record, written whether or not anything fired.
        if record.anything_held_back:
            logger.info(
                "Boundary: tier=%s sent=%d/%d withheld_restricted=%d masked=%s",
                record.tier, len(kept), record.passages_considered,
                record.passages_withheld_restricted,
                {m.label: m.count for m in record.masked},
            )
        record.passages_sent = len(kept)

        text, in_tokens, out_tokens = self._provider.complete(
            system, build_user(list(zip(kept_indices, kept, strict=True)))
        )
        return text, in_tokens, out_tokens, record
