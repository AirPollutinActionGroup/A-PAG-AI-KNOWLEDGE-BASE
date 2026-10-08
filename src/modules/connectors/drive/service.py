"""The Drive sync policy: what to import, at what tier, owned by whom, and what to retire.

**It does not process anything.** Every file it accepts is handed to `UploadService.receive()`,
the same entry point the upload form uses, and the pipeline takes it from there -- scan, OCR,
quality gate, chunking, embedding, audit, and the Data Boundary Gateway. A document that came
from Drive is indistinguishable afterwards from one somebody uploaded by hand, which is the whole
design: a second way in must not become a second set of rules.

That has one consequence that shapes everything below. `receive()` returns as soon as the bytes
are in quarantine; whether the file was a duplicate, whether it extracted, whether it embedded, is
decided minutes later by workers this module never talks to. So **a run can only act on what an
earlier run imported**. Each pass therefore does two things: syncs Drive into the pipeline, then
reconciles what previous passes put there and has since settled.

The decisions, all of them here and nowhere else:

**The folder sets the tier, by folder ID.** A file under the Public tree is PUBLIC and one under
the Restricted tree is RESTRICTED, and a file under neither is skipped rather than guessed at.
IDs rather than names is a security property: a name can be renamed, duplicated, or shadowed by a
subfolder somebody calls "Public". Where Drive puts one file under both trees -- it allows several
parents -- the stricter tier wins, via `is_stricter()`, the same rule dedup uses.

**A move between folders reclassifies in place.** It changes who may read a document, not what
the document says, so nothing is re-imported: the existing document's tier changes and a
`DOCUMENT_RECLASSIFIED` row records that the folder did it. Re-importing instead would collide with
dedup, which by design never *lowers* a tier -- an ordinary file moved from Restricted to Public
would silently stay RESTRICTED, while a Google Doc (whose exports differ byte for byte) would land
as a second document. One rule for both is better than two accidents.

**Content change is Drive's checksum where there is one, the modified time where there is not.**
A rename or move can bump the modified time without changing a byte, and on a nightly sync that
would re-embed an unchanged document. Google-native files have no checksum, and their exports are
not byte-stable either (two exports of one unchanged Sheet hash differently), so for those the
modified time is the only honest signal.

**An edit is a new version, and the old one is retired only once the new one is live.** Retiring
at import time would leave the file with nothing searchable if the new version then failed
extraction. The retired version is marked `SUPERSEDED` and hidden, with `supersedes_id` linking
the two -- `SUPERSEDED` also takes it out of dedup, so reverting a file to earlier content imports
cleanly instead of matching its own retired copy.

**Removal is reversible, and putting the file back really does restore it.** Binned, deleted, or
moved out all arrive identically -- the file is no longer in the listing -- and all soft-delete.
Returning it un-edited restores the original document rather than re-importing it: a re-import
would be marked DUPLICATE against the hidden original, because dedup does not look at
`deleted_at`, and nothing would come back. Never `purge()`.

**Ownership is provenance, not authorisation.** The Drive owner's email is matched to a local
account because RESTRICTED is owner-scoped; where none matches, a configured fallback owns it, and
for a RESTRICTED file that means only that account and admins can read it. Reported, not just
logged, because it otherwise looks like nothing happened.
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType, Classification, DocumentStatus
from src.db.models import AuditLog
from src.db.models import Document as DocumentORM
from src.db.models import DriveFile as DriveFileORM
from src.db.models import DriveFileVersion as VersionORM
from src.db.models import User as UserORM
from src.modules.audit.service import AuditService
from src.modules.auth.access import is_stricter
from src.modules.connectors.drive.export_map import (
    FOLDER_MIME,
    export_target,
    unexportable_reason,
)
from src.modules.connectors.drive.models import DriveFile, DriveFileState, VersionState
from src.modules.document_pipeline.formats import detect_format
from src.modules.document_pipeline.models import UploadRequest
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository
from src.modules.document_pipeline.upload_service import UploadService
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)

# Statuses at which a document has stopped moving without becoming searchable. A version that
# lands on one of these never replaces the version before it.
_FAILED_STATUSES = {
    DocumentStatus.REJECTED.value,
    DocumentStatus.VALIDATION_FAILED.value,
    DocumentStatus.EXTRACTION_FAILED.value,
    DocumentStatus.NORMALIZATION_FAILED.value,
    DocumentStatus.CHUNKING_FAILED.value,
    DocumentStatus.EMBEDDING_FAILED.value,
    DocumentStatus.SKIPPED_UNSUPPORTED_LANGUAGE.value,
}

_ACTOR = "drive-sync"

# Arbitrary but fixed: the identity of "a Drive sync" for Postgres's advisory lock.
_LOCK_KEY = 4_730_211_905


@dataclass
class SyncOutcome:
    """What one pass did, in the shape the command prints and the worker logs."""

    files_found: int = 0
    imported: list[str] = field(default_factory=list)
    # Imports that are new versions of a file already in the knowledge base. A subset of
    # `imported`, reported separately because they will retire something once they settle.
    edited: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    # (name, old tier, new tier) -- a move between folders. Called out because a move from
    # Restricted to Public widens who can read a document, and that is worth a person's eyes.
    reclassified: list[tuple[str, str, str]] = field(default_factory=list)
    owner_fallbacks: list[tuple[str, str]] = field(default_factory=list)

    # Reconciliation: what earlier imports turned into, now that the workers have finished.
    superseded: list[str] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    # A subset of `duplicates`: the copy was filed more restricted than the document already
    # held, so that document's tier was raised (KNOWN_DEBTS #36).
    escalated: list[str] = field(default_factory=list)
    pipeline_failed: list[tuple[str, str]] = field(default_factory=list)
    still_processing: int = 0


class DriveSyncError(RuntimeError):
    """Configuration is wrong in a way that makes the whole run meaningless."""


class SyncAlreadyRunning(DriveSyncError):
    """Another process is syncing right now."""


@contextmanager
def sync_lock(session_factory: Callable[[], Session]) -> Iterator[None]:
    """One Drive sync at a time, across processes.

    The nightly worker and somebody running `drive_sync.py` by hand can genuinely overlap, and two
    passes listing the same folder would both see a new file as new and import it twice. An
    advisory lock in Postgres -- the one thing both processes share -- makes the second one stop
    rather than duplicate. The transaction-scoped form is used so the lock cannot outlive the
    connection that took it: ending the transaction releases it, even if this process dies.

    A no-op on SQLite, which the unit tests run against and which has no advisory locks.
    """
    with session_factory() as session:
        bind = session.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            yield
            return
        acquired = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_KEY}
        ).scalar()
        if not acquired:
            raise SyncAlreadyRunning(
                "Another Drive sync is running. It will finish on its own; run this again after."
            )
        try:
            yield
        finally:
            session.rollback()  # ends the transaction, which releases the lock


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

        Both trees are walked in full so nested subfolders work -- people organise, and a document
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
                # Drive allows a folder under more than one parent, so the two trees can genuinely
                # overlap. Fail closed when they do.
                if current is None or is_stricter(tier, current):
                    tiers[folder_id] = tier
        logger.info("Watching %d Drive folder(s)", len(tiers))
        return tiers

    def list_watched_files(self, tiers: dict[str, Classification]) -> dict[str, tuple]:
        """Every non-folder file under the watched trees, with the tier it resolved to.

        Keyed by Drive file id, because a file with two parents is one file and must not be
        imported twice. Where its parents disagree, the stricter tier wins.
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
        owner-scoped -- so there is no "leave it blank" option, and the fallback has to resolve to
        a real account or the run is pointless.
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
        fallback_id = self._owner_cache["__fallback__"]
        assert fallback_id is not None  # validated above before it was cached
        return fallback_id, True

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
    ) -> DriveFileORM:
        """Writes what this file now is. `document_id` is the most recently *imported* document;
        which document is currently *visible* is the versions table's business."""
        row = session.get(DriveFileORM, item.file_id) or DriveFileORM(drive_file_id=item.file_id)
        if document_id is not None:
            row.document_id = document_id
        row.drive_modified_time = item.modified_time
        row.drive_md5 = item.md5
        row.drive_name = item.name
        row.drive_mime_type = item.mime_type
        row.folder_id = folder_id
        row.classification = tier.value if tier else None
        row.drive_owner_email = item.owner_email
        row.state = state.value
        row.skip_reason = skip_reason
        row.last_synced_at = datetime.now(UTC)
        row = session.merge(row)
        session.commit()
        return row

    @staticmethod
    def _audit(session: Session, document_id, event: AuditEventType, details: dict) -> None:
        """Best-effort, like every other audit write in this codebase: a failure is logged at
        ERROR and never blocks the sync. Attributed to the connector, never to a person."""
        try:
            AuditService.log_event(
                db=session, document_id=document_id, event_type=event,
                details={"actor": _ACTOR, **details}, user_id=_ACTOR,
            )
        except Exception:
            session.rollback()
            logger.exception(
                "AUDIT WRITE FAILED: doc_id=%s event=%s -- compliance gap, investigate",
                document_id, event.value,
            )

    @staticmethod
    def _content_changed(row: DriveFileORM, item: DriveFile) -> bool:
        """Has the content changed since the last import? See the module docstring for why the
        checksum is preferred and the modified time is the fallback."""
        if item.md5 and row.drive_md5:
            return item.md5 != row.drive_md5
        return item.modified_time != row.drive_modified_time

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
        """One pass. A dry run writes nothing, so it needs no lock and is always allowed."""
        if dry_run:
            return self._sync_once(dry_run=True, limit=limit)
        with sync_lock(self._session_factory):
            return self._sync_once(dry_run=False, limit=limit)

    def _sync_once(self, *, dry_run: bool, limit: int | None) -> SyncOutcome:
        outcome = SyncOutcome()
        tiers = self.resolve_folder_tiers()
        found = self.list_watched_files(tiers)
        outcome.files_found = len(found)
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
        # Last, so a file restored above settles in this same run rather than the next one.
        self.reconcile(outcome, dry_run=dry_run)
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
            is_new_version = False
            if (
                existing is not None
                and existing.document_id is not None
                and existing.state in (DriveFileState.IMPORTED.value, DriveFileState.REMOVED.value)
            ):
                if self._content_changed(existing, item):
                    # Edited. A file that was removed and comes back edited is simply imported;
                    # its old version stays retired.
                    is_new_version = existing.state == DriveFileState.IMPORTED.value
                else:
                    self._unchanged_content(session, existing, item, tier, folder_id, label,
                                            outcome, dry_run=dry_run)
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
                if is_new_version:
                    outcome.edited.append(label)
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

            # Content, not extension -- the same rule the API boundary applies. A .docx that is
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
                    "Drive owner %r has no account here; %s is owned by the fallback account.%s",
                    item.owner_email, item.name,
                    " Being RESTRICTED, only that account and admins will see it."
                    if tier == Classification.RESTRICTED else "",
                )

            service = UploadService(
                bucket_manager=self._buckets,
                repository=PostgreSQLDocumentRepository(session),
                db_session=session,
            )
            # No duplicate check here, deliberately: `receive()` only quarantines, and whether
            # this turns out to be a duplicate is decided by the scan worker afterwards.
            # Reconciliation reports it once it is known.
            response = service.receive(
                filename=filename,
                data=data,
                request_meta=UploadRequest(classification=tier, owner_id=owner_id),
                mime_type=spec.mime_type,
            )

            outcome.imported.append(label)
            if is_new_version:
                outcome.edited.append(label)

            row = self._record(
                session, item, state=DriveFileState.IMPORTED,
                tier=tier, folder_id=folder_id, document_id=response.document_id,
            )
            session.add(VersionORM(
                drive_file_id=row.drive_file_id,
                document_id=response.document_id,
                drive_modified_time=item.modified_time,
                drive_md5=item.md5,
                state=VersionState.PENDING.value,
            ))
            session.commit()

    def _unchanged_content(
        self,
        session: Session,
        existing: DriveFileORM,
        item: DriveFile,
        tier: Classification,
        folder_id: str,
        label: str,
        outcome: SyncOutcome,
        *,
        dry_run: bool,
    ) -> None:
        """Same content as last time. Whatever else changed -- the file came back, or moved
        between folders -- is bookkeeping or a decision about access, and neither justifies
        downloading or re-embedding anything."""
        acted = False
        if existing.state == DriveFileState.REMOVED.value:
            outcome.restored.append(label)
            acted = True
            if not dry_run:
                self._restore(session, existing)
        previous_tier = str(existing.classification or "-")
        if previous_tier != tier.value:
            outcome.reclassified.append((item.name, previous_tier, tier.value))
            acted = True
            if not dry_run:
                self._reclassify(session, existing, tier)
        if not acted:
            outcome.unchanged += 1
        if not dry_run:
            self._record(
                session, item, state=DriveFileState.IMPORTED, tier=tier, folder_id=folder_id,
            )

    # ------------------------------------------------- restore and reclassify

    def _restore(self, session: Session, row: DriveFileORM) -> None:
        """Brings a removed file's last document back, rather than importing it again.

        A re-import would be quarantined, scanned, and marked DUPLICATE against the hidden
        original -- dedup matches on the hash and does not consult `deleted_at` -- and nothing
        would become visible. So the original row is restored, and goes back through
        reconciliation as `PENDING` so a version that was still processing when it vanished is
        settled the same way as any other.
        """
        version = session.execute(
            select(VersionORM)
            .where(
                VersionORM.drive_file_id == row.drive_file_id,
                VersionORM.state == VersionState.REMOVED.value,
            )
            .order_by(VersionORM.imported_at.desc())
        ).scalars().first()
        document_id = version.document_id if version else row.document_id
        if document_id is None:
            return

        if PostgreSQLDocumentRepository(session).restore(document_id):
            self._audit(session, document_id, AuditEventType.DOCUMENT_RESTORED, {
                "reason": "drive-file-returned",
                "drive_file_id": row.drive_file_id,
            })
        if version is not None:
            version.state = VersionState.PENDING.value
            version.settled_at = None
        session.commit()
        logger.info("Restored %s (document %s): back in a watched folder.",
                    row.drive_name, document_id)

    def _reclassify(self, session: Session, row: DriveFileORM, tier: Classification) -> None:
        """A move between folders: same content, new tier, no re-import.

        Applies to the visible version and to any still in the pipeline, since a pending version
        would otherwise settle at the tier it was imported with and quietly undo the move.
        """
        repo = PostgreSQLDocumentRepository(session)
        targets = session.execute(
            select(VersionORM.document_id).where(
                VersionORM.drive_file_id == row.drive_file_id,
                VersionORM.state.in_(
                    [VersionState.CURRENT.value, VersionState.PENDING.value]
                ),
            )
        ).scalars().all() or ([row.document_id] if row.document_id else [])

        for document_id in targets:
            doc = repo.get_by_id(document_id)
            if doc is None or doc.purged_at is not None:
                continue
            previous = doc.classification.value if doc.classification else None
            if previous == tier.value:
                continue
            doc.classification = tier
            repo.update_document(doc)
            self._audit(session, document_id, AuditEventType.DOCUMENT_RECLASSIFIED, {
                # Same shape as the endpoint's and dedup's rows, so every tier change reads alike.
                "old_tier": previous,
                "new_tier": tier.value,
                "reason": "drive-folder-move",
                "drive_file_id": row.drive_file_id,
            })
            log = logger.warning if tier == Classification.PUBLIC else logger.info
            log("Tier changed by folder move: %s %s -> %s", row.drive_name, previous, tier.value)

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
        not in the listing. Every version that could become visible is retired -- including one
        still processing, which would otherwise finish embedding and appear in search for a file
        that is gone.

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

                live = session.execute(
                    select(VersionORM).where(
                        VersionORM.drive_file_id == row.drive_file_id,
                        VersionORM.state.in_(
                            [VersionState.CURRENT.value, VersionState.PENDING.value]
                        ),
                    )
                ).scalars().all()
                document_ids = [v.document_id for v in live] or (
                    [row.document_id] if row.document_id else []
                )
                for document_id in document_ids:
                    if repo.soft_delete(document_id):
                        session.commit()
                        self._audit(session, document_id, AuditEventType.DOCUMENT_DELETED, {
                            "reason": "drive-file-removed",
                            "drive_file_id": row.drive_file_id,
                            "permanent": False,
                        })
                for version in live:
                    version.state = VersionState.REMOVED.value
                    version.settled_at = datetime.now(UTC)
                row.state = DriveFileState.REMOVED.value
                row.skip_reason = "no longer in a watched Drive folder"
                session.commit()
                logger.info(
                    "Retired %s: gone from Drive or moved out of the watched folders. "
                    "Reversible -- put it back and the next sync restores it.",
                    row.drive_name,
                )

    # ----------------------------------------------------------- reconciliation

    def reconcile(self, outcome: SyncOutcome, *, dry_run: bool = False) -> None:
        """Settles versions an earlier run imported, now that the pipeline has had them.

        For each file with something pending: a version whose document is LIVE becomes CURRENT
        and retires the one before it; one that came out DUPLICATE or failed is recorded as such,
        and the previous version stays visible. Nothing is retired while the newest version is
        still being processed -- for up to one sync interval both versions can appear in search,
        which is the price of never leaving a file with nothing visible.
        """
        with self._session_factory() as session:
            pending_files = session.execute(
                select(VersionORM.drive_file_id)
                .where(VersionORM.state == VersionState.PENDING.value)
                .distinct()
            ).scalars().all()

            for drive_file_id in pending_files:
                self._reconcile_file(session, drive_file_id, outcome, dry_run=dry_run)

    def _reconcile_file(
        self, session: Session, drive_file_id: str, outcome: SyncOutcome, *, dry_run: bool
    ) -> None:
        versions = session.execute(
            select(VersionORM)
            .where(VersionORM.drive_file_id == drive_file_id)
            .order_by(VersionORM.imported_at)
        ).scalars().all()
        row = session.get(DriveFileORM, drive_file_id)
        name = (row.drive_name if row else None) or drive_file_id
        now = datetime.now(UTC)

        docs = {
            d.document_id: d
            for d in session.execute(
                select(DocumentORM).where(
                    DocumentORM.document_id.in_([v.document_id for v in versions])
                )
            ).scalars()
        }

        # Settle each pending version on its own terms first.
        for version in versions:
            if version.state != VersionState.PENDING.value:
                continue
            doc = docs.get(version.document_id)
            status = doc.status if doc else None

            if status == DocumentStatus.DUPLICATE.value:
                canonical = self._canonical_for(session, doc)
                outcome.duplicates.append(
                    (name, canonical.filename if canonical else "an existing document")
                )
                if self._was_escalation(session, version.document_id):
                    outcome.escalated.append(name)
                if not dry_run:
                    version.state = VersionState.DUPLICATE.value
                    version.note = (
                        f"duplicate of {canonical.document_id}" if canonical else "duplicate"
                    )
                    version.settled_at = now
            elif status in _FAILED_STATUSES:
                outcome.pipeline_failed.append((name, status))
                if not dry_run:
                    version.state = VersionState.FAILED.value
                    version.note = status
                    version.settled_at = now

        newest = versions[-1]
        newest_doc = docs.get(newest.document_id)
        if newest.state == VersionState.PENDING.value and (
            newest_doc is None or newest_doc.status != DocumentStatus.LIVE.value
        ):
            outcome.still_processing += 1
            if not dry_run:
                session.commit()
            return

        if newest.state == VersionState.PENDING.value:
            # The newest version is live: it becomes the one people see, and every earlier
            # version still visible is retired in its favour.
            for older in versions[:-1]:
                if older.state not in (VersionState.CURRENT.value, VersionState.PENDING.value):
                    continue
                outcome.superseded.append(name)
                if not dry_run:
                    self._supersede(session, docs.get(older.document_id), newest_doc,
                                    drive_file_id)
                    older.state = VersionState.SUPERSEDED.value
                    older.settled_at = now
            if not dry_run:
                newest.state = VersionState.CURRENT.value
                newest.settled_at = now

        if not dry_run:
            session.commit()

    def _supersede(self, session, old_doc, new_doc, drive_file_id: str) -> None:
        """Retires `old_doc` in favour of `new_doc`, the way the codebase already models it.

        Status `SUPERSEDED` and `supersedes_id` are the existing versioning vocabulary. The status
        also matters mechanically: dedup and the active-hash unique index both exclude SUPERSEDED,
        so reverting a file to earlier content imports cleanly instead of matching its own retired
        copy. `deleted_at` is what actually removes it from search, which filters on that column
        and not on status.
        """
        if old_doc is None or old_doc.purged_at is not None:
            return
        repo = PostgreSQLDocumentRepository(session)

        old = repo.get_by_id(old_doc.document_id)
        if old is None:
            return
        old.status = DocumentStatus.SUPERSEDED
        repo.update_document(old)
        repo.soft_delete(old_doc.document_id)
        session.commit()

        new = repo.get_by_id(new_doc.document_id) if new_doc is not None else None
        if new is not None:
            new.supersedes_id = old_doc.document_id
            new.version = (old_doc.version or 1) + 1
            repo.update_document(new)

        self._audit(session, old_doc.document_id, AuditEventType.DOCUMENT_SUPERSEDED, {
            "reason": "drive-file-edited",
            "superseded_by": str(new_doc.document_id) if new_doc else None,
            "drive_file_id": drive_file_id,
        })

    @staticmethod
    def _canonical_for(session: Session, duplicate: DocumentORM | None) -> DocumentORM | None:
        """The document a duplicate was matched against: same bytes, not itself a duplicate."""
        if duplicate is None or not duplicate.sha256:
            return None
        return session.execute(
            select(DocumentORM).where(
                DocumentORM.sha256 == duplicate.sha256,
                DocumentORM.document_id != duplicate.document_id,
                DocumentORM.status.not_in([
                    DocumentStatus.DUPLICATE.value, DocumentStatus.REJECTED.value,
                ]),
            )
        ).scalars().first()

    @staticmethod
    def _was_escalation(session: Session, document_id: uuid.UUID) -> bool:
        """Whether the scan worker raised the canonical document's tier for this duplicate.

        Read from the audit row the scan stage wrote, because that is the only place the fact
        exists -- the scan worker and this module never talk to each other directly.
        """
        rows = session.execute(
            select(AuditLog.details).where(
                AuditLog.document_id == document_id,
                AuditLog.event_type == AuditEventType.DOCUMENT_REJECTED.value,
            )
        ).scalars().all()
        return any((details or {}).get("canonical_tier_escalated") for details in rows)
