"""The Drive sync policy: what to import, at what tier, owned by whom, and what to retire.

**It does not process anything.** Every file it accepts is handed to `UploadService.receive()`,
the same entry point the upload form uses, and the pipeline takes it from there — scan, OCR,
quality gate, chunking, embedding, audit, and the Data Boundary Gateway. A document that came
from Drive is indistinguishable afterwards from one somebody uploaded by hand, which is the whole
design: a second way in must not become a second set of rules.

Four decisions live here and nowhere else.

**The folder sets the tier, by folder ID.** A file under the Public tree is PUBLIC and one under
the Restricted tree is RESTRICTED, and a file under neither is skipped rather than guessed at.
Matching on IDs rather than names is a security property, not tidiness: names can be renamed,
duplicated, or shadowed by a subfolder somebody calls "Public", and a rename would otherwise
reclassify everything beneath it silently. Drive also lets one folder sit under two parents, so
where the trees overlap the **stricter** tier wins — `is_stricter()` from `auth/access.py`, the
same rule dedup uses.

**Change is detected by Drive's `modifiedTime`, not by hashing.** Google re-exports an unchanged
Doc to slightly different bytes each time, so a content hash would re-import the entire corpus on
every run. An edited file arrives as a **new document** and the previous one is left alone, which
keeps a citation written last week pointing at the text that was actually cited.

**Removal is reversible.** A file binned, deleted, or moved out of the watched folders retires
its document through the existing soft delete: gone from search and from answers immediately,
bytes and audit trail kept, restorable. Never a purge. Drive's own bin is reversible for about a
month, and a sync should not be more destructive than the thing it is following.

**Ownership is provenance, not authorisation.** The Drive owner's email is matched against local
accounts to decide who owns the document, because RESTRICTED is owner-scoped. Where no account
matches, a configured fallback owns it — and for a RESTRICTED file that means only that account
and admins can see it, which is logged loudly because it is a surprising outcome that looks like
nothing at all.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.enums import Classification
from src.db.models import DriveFile as DriveFileORM
from src.db.models import User as UserORM
from src.modules.auth.access import is_stricter
from src.modules.connectors.drive.export_map import (
    FOLDER_MIME,
    export_target,
    unexportable_reason,
)
from src.modules.connectors.drive.models import DriveFile, DriveFileState
from src.modules.document_pipeline.formats import detect_format
from src.modules.document_pipeline.models import UploadRequest
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository
from src.modules.document_pipeline.upload_service import UploadService
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)


@dataclass
class SyncOutcome:
    """What one pass did, in the shape the command prints and the worker logs."""

    imported: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    # A subset of `duplicates`. The copy was dropped, but it was filed more restricted than the
    # document already held, so that document's tier was raised (see KNOWN_DEBTS #36). Called out
    # separately because it is the one outcome here that changes who can read something.
    escalated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    # Set when ownership fell back, so the report can say who will actually be able to read a
    # RESTRICTED document.
    owner_fallbacks: list[tuple[str, str]] = field(default_factory=list)

    @property
    def seen(self) -> int:
        return (
            len(self.imported) + len(self.duplicates) + len(self.skipped)
            + len(self.failed) + self.unchanged
        )


class DriveSyncError(RuntimeError):
    """Configuration is wrong in a way that makes the whole run meaningless."""


class DriveSyncService:
    def __init__(
        self,
        client,
        session_factory: Callable[[], Session],
        *,
        public_folder_id: str,
        restricted_folder_id: str,
        fallback_owner_email: str,
        max_bytes: int,
        bucket_manager: BucketManager | None = None,
    ):
        self._client = client
        self._session_factory = session_factory
        self._public_root = public_folder_id
        self._restricted_root = restricted_folder_id
        self._fallback_owner_email = (fallback_owner_email or "").strip().lower()
        self._max_bytes = max_bytes
        self._buckets = bucket_manager or BucketManager()
        self._owner_cache: dict[str, uuid.UUID | None] = {}

    # ------------------------------------------------------------------ folders

    def resolve_folder_tiers(self) -> dict[str, Classification]:
        """Every watched folder id mapped to the tier it confers.

        Both trees are walked in full so nested subfolders work — people organise, and a document
        in `Restricted/2026/` is no less restricted for it.
        """
        if not self._public_root and not self._restricted_root:
            raise DriveSyncError(
                "Neither GDRIVE_PUBLIC_FOLDER_ID nor GDRIVE_RESTRICTED_FOLDER_ID is set. "
                "Without a watched folder there is nothing to sync, and no way to decide a tier."
            )

        tiers: dict[str, Classification] = {}
        for root, tier in (
            (self._public_root, Classification.PUBLIC),
            (self._restricted_root, Classification.RESTRICTED),
        ):
            if not root:
                continue
            for folder_id in self._client.walk_folder_tree(root):
                current = tiers.get(folder_id)
                # Drive allows a folder to sit under more than one parent, so the two trees can
                # genuinely overlap. Fail closed when they do.
                if current is None or is_stricter(tier, current):
                    tiers[folder_id] = tier
        logger.info("Watching %d Drive folder(s)", len(tiers))
        return tiers

    def list_watched_files(self, tiers: dict[str, Classification]) -> dict[str, tuple]:
        """Every non-folder file under the watched trees, with the tier it resolved to.

        Keyed by Drive file id, because a file with two parents is one file and must not be
        imported twice. Where its parents disagree, the stricter tier wins for the same reason
        folders do.
        """
        found: dict[str, tuple[DriveFile, Classification, str]] = {}
        for folder_id, tier in tiers.items():
            for item in self._client.list_children(folder_id):
                if item.mime_type == FOLDER_MIME:
                    continue
                existing = found.get(item.file_id)
                if existing is None or is_stricter(tier, existing[1]):
                    found[item.file_id] = (item, tier, folder_id)
        return found

    # ------------------------------------------------------------------- owners

    def resolve_owner(self, session: Session, email: str | None) -> tuple[uuid.UUID, bool]:
        """(owner_id, used_fallback).

        A document with no owner is one nobody but an admin can ever see, because RESTRICTED is
        owner-scoped — so there is no "leave it blank" option here, and the fallback has to
        resolve to a real account or the run is pointless.
        """
        key = (email or "").strip().lower()
        if key and key not in self._owner_cache:
            self._owner_cache[key] = session.execute(
                select(UserORM.user_id).where(UserORM.email == key)
            ).scalar_one_or_none()

        matched = self._owner_cache.get(key) if key else None
        if matched is not None:
            return matched, False

        if "__fallback__" not in self._owner_cache:
            if not self._fallback_owner_email:
                raise DriveSyncError(
                    "GDRIVE_FALLBACK_OWNER_EMAIL is not set. Documents whose Drive owner has no "
                    "account here would have no owner, and a RESTRICTED document with no owner "
                    "is invisible to everyone except admins."
                )
            fallback = session.execute(
                select(UserORM.user_id).where(UserORM.email == self._fallback_owner_email)
            ).scalar_one_or_none()
            if fallback is None:
                raise DriveSyncError(
                    f"GDRIVE_FALLBACK_OWNER_EMAIL is {self._fallback_owner_email!r}, which is "
                    f"not a registered user. Register it first (POST /api/v1/auth/register)."
                )
            self._owner_cache["__fallback__"] = fallback
        return self._owner_cache["__fallback__"], True

    # -------------------------------------------------------------- bookkeeping

    @staticmethod
    def _record(
        session: Session,
        item: DriveFile,
        *,
        state: DriveFileState,
        tier: Classification | None = None,
        folder_id: str | None = None,
        document_id: uuid.UUID | None = None,
        skip_reason: str | None = None,
    ) -> None:
        row = session.get(DriveFileORM, item.file_id) or DriveFileORM(drive_file_id=item.file_id)
        row.document_id = document_id
        row.drive_modified_time = item.modified_time
        row.drive_name = item.name
        row.drive_mime_type = item.mime_type
        row.folder_id = folder_id
        row.classification = tier.value if tier else None
        row.drive_owner_email = item.owner_email
        row.state = state.value
        row.skip_reason = skip_reason
        session.merge(row)
        session.commit()

    # ------------------------------------------------------------------ content

    def fetch_bytes(self, item: DriveFile) -> tuple[bytes, str]:
        """The file's bytes and the filename to store it under.

        Google-native documents are exported; everything else is downloaded as-is. The extension
        comes from the export target rather than the Drive name, because a Google Doc called
        "FGD Note" has no extension at all and `detect_format()` reads bytes, not names.
        """
        target = export_target(item.mime_type)
        if target is None:
            return self._client.download(item.file_id), item.name
        export_mime, extension = target
        data = self._client.export(item.file_id, export_mime)
        name = item.name if item.name.lower().endswith(extension) else item.name + extension
        return data, name

    # --------------------------------------------------------------------- pass

    def sync_once(self, *, dry_run: bool = False, limit: int | None = None) -> SyncOutcome:
        outcome = SyncOutcome()
        tiers = self.resolve_folder_tiers()
        found = self.list_watched_files(tiers)
        logger.info("Found %d file(s) under the watched folders", len(found))

        items = sorted(found.values(), key=lambda row: row[0].name)
        if limit is not None:
            items = items[:limit]

        for item, tier, folder_id in items:
            try:
                self._sync_one(item, tier, folder_id, outcome, dry_run=dry_run)
            except DriveSyncError:
                # Configuration, not this file. Every remaining file would fail the same way.
                raise
            except Exception as e:
                # One surprising file must not end the run. A real folder contains something
                # unexpected, and discovering at file 190 that nothing since file 12 was imported
                # is the failure worth designing against.
                logger.warning("FAILED %s (%s): %s", item.name, item.file_id, e)
                outcome.failed.append((item.name, str(e)))
                if not dry_run:
                    with self._session_factory() as session:
                        self._record(
                            session, item, state=DriveFileState.FAILED,
                            tier=tier, folder_id=folder_id, skip_reason=str(e)[:500],
                        )

        self._retire_vanished(found, outcome, dry_run=dry_run, partial=limit is not None)
        return outcome

    def _sync_one(
        self,
        item: DriveFile,
        tier: Classification,
        folder_id: str,
        outcome: SyncOutcome,
        *,
        dry_run: bool,
    ) -> None:
        label = f"{item.name} [{tier.value}]"

        with self._session_factory() as session:
            existing = session.get(DriveFileORM, item.file_id)

            if (
                existing is not None
                and existing.state == DriveFileState.IMPORTED.value
                and existing.drive_modified_time == item.modified_time
                and existing.classification == tier.value
            ):
                # Unchanged, and still filed where it was. Nothing is downloaded: this is what
                # makes an idle sync cost one small listing request rather than the whole folder.
                outcome.unchanged += 1
                return

            reason = unexportable_reason(item.mime_type)
            if reason:
                outcome.skipped.append((item.name, reason))
                if not dry_run:
                    self._record(
                        session, item, state=DriveFileState.SKIPPED,
                        tier=tier, folder_id=folder_id, skip_reason=reason,
                    )
                return

            # Checked against Drive's own figure, before fetching anything. `receive()` takes
            # bytes, so without this an oversized file is pulled into memory purely to be
            # rejected by a validator that already knew the limit.
            if item.size is not None and item.size > self._max_bytes:
                reason = (
                    f"{item.size / 1024 / 1024:.1f}MB exceeds the "
                    f"{self._max_bytes / 1024 / 1024:.0f}MB limit"
                )
                outcome.skipped.append((item.name, reason))
                if not dry_run:
                    self._record(
                        session, item, state=DriveFileState.SKIPPED,
                        tier=tier, folder_id=folder_id, skip_reason=reason,
                    )
                return

            if dry_run:
                outcome.imported.append(label)
                return

            data, filename = self.fetch_bytes(item)
            if not data:
                reason = "the file is empty"
                outcome.skipped.append((item.name, reason))
                self._record(
                    session, item, state=DriveFileState.SKIPPED,
                    tier=tier, folder_id=folder_id, skip_reason=reason,
                )
                return

            # Content, not extension — the same rule the API boundary applies. A .docx that is
            # really a spreadsheet is stored as the spreadsheet it is.
            spec = detect_format(data)
            if spec is None:
                reason = "not a PDF, DOCX, XLSX or PPTX"
                outcome.skipped.append((item.name, reason))
                self._record(
                    session, item, state=DriveFileState.SKIPPED,
                    tier=tier, folder_id=folder_id, skip_reason=reason,
                )
                return

            owner_id, used_fallback = self.resolve_owner(session, item.owner_email)
            if used_fallback:
                outcome.owner_fallbacks.append((item.name, item.owner_email or "unknown"))
                log = logger.warning if tier == Classification.RESTRICTED else logger.info
                log(
                    "Drive owner %r has no account here; %s is owned by the fallback account. "
                    "%s", item.owner_email, item.name,
                    "Being RESTRICTED, only that account and admins will see it."
                    if tier == Classification.RESTRICTED else "",
                )

            service = UploadService(
                bucket_manager=self._buckets,
                repository=PostgreSQLDocumentRepository(session),
                db_session=session,
            )
            response = service.receive(
                filename=filename,
                data=data,
                request_meta=UploadRequest(classification=tier, owner_id=owner_id),
                mime_type=spec.mime_type,
            )

            if response.was_duplicate:
                outcome.duplicates.append(label)
                if response.canonical_tier_escalated:
                    outcome.escalated.append(label)
            else:
                outcome.imported.append(label)

            self._record(
                session, item, state=DriveFileState.IMPORTED,
                tier=tier, folder_id=folder_id, document_id=response.document_id,
            )

    # ----------------------------------------------------------------- removals

    def _retire_vanished(
        self,
        found: dict,
        outcome: SyncOutcome,
        *,
        dry_run: bool,
        partial: bool,
    ) -> None:
        """Soft-deletes documents whose Drive file is no longer in the watched folders.

        Binned, permanently deleted, or moved out all reach this the same way: the file is simply
        not in the listing any more. They are treated alike because all three mean somebody
        decided it should not be there, and the action taken is reversible either way.

        **Never runs on a partial pass.** With `--limit`, "not in the listing" means "not in the
        part we looked at", and acting on that would retire most of the corpus.
        """
        if partial:
            logger.info("Skipping removal detection: a limited run cannot tell gone from unseen.")
            return

        with self._session_factory() as session:
            rows = session.execute(
                select(DriveFileORM).where(DriveFileORM.state == DriveFileState.IMPORTED.value)
            ).scalars().all()

            repo = PostgreSQLDocumentRepository(session)
            for row in rows:
                if row.drive_file_id in found:
                    continue
                outcome.removed.append(row.drive_name or row.drive_file_id)
                if dry_run:
                    continue
                if row.document_id is not None:
                    repo.soft_delete(row.document_id)
                row.state = DriveFileState.REMOVED.value
                row.skip_reason = "no longer in a watched Drive folder"
                logger.info(
                    "Retired %s (document %s): gone from Drive or moved out of the watched "
                    "folders. Reversible — the bytes and audit trail are kept.",
                    row.drive_name, row.document_id,
                )
            if not dry_run:
                session.commit()
