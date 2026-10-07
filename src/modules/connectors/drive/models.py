"""What the connector keeps about a Drive file, and what it decided to do with it.

Deliberately a small dataclass rather than Drive's raw JSON. The rest of the connector should not
know that `modifiedTime` is camelCase or that `owners` is a list, and a fake in the tests should
only have to produce something this shape — not a faithful imitation of Google's API.
"""

from dataclasses import dataclass, field
from enum import Enum


class DriveFileState(str, Enum):
    """What happened to a Drive file, stored on the `drive_files` row.

    `SKIPPED` is a real outcome and not an error: a Google Form or a 200MB video in the watched
    folder is something the connector is right to leave alone, and recording why beats rediscovering
    it on every sync. The whole point is that a gap is findable rather than silent — the same
    reasoning as `SKIPPED_UNSUPPORTED_LANGUAGE` in the embedding stage.
    """

    IMPORTED = "IMPORTED"
    SKIPPED = "SKIPPED"
    REMOVED = "REMOVED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class DriveFile:
    """One file as Drive describes it, reduced to what the connector actually uses."""

    file_id: str
    name: str
    mime_type: str
    modified_time: str
    # Drive reports no size for Google-native documents, because they have no bytes until they
    # are exported. None therefore means "unknown", not "empty", and the size gate has to treat
    # it as unknown rather than as zero.
    size: int | None = None
    owner_email: str | None = None
    parents: list[str] = field(default_factory=list)
    trashed: bool = False

    @classmethod
    def from_api(cls, payload: dict) -> "DriveFile":
        owners = payload.get("owners") or []
        raw_size = payload.get("size")
        return cls(
            file_id=payload["id"],
            name=payload.get("name", ""),
            mime_type=payload.get("mimeType", ""),
            modified_time=payload.get("modifiedTime", ""),
            size=int(raw_size) if raw_size is not None else None,
            owner_email=(owners[0].get("emailAddress") if owners else None),
            parents=list(payload.get("parents") or []),
            trashed=bool(payload.get("trashed", False)),
        )
