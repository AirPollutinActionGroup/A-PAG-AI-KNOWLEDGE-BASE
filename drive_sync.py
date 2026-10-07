"""Import a shared Google Drive folder into the knowledge base.

Staff drop files into "A-PAG Knowledge Base" in Drive; the subfolder they choose — `Public` or
`Restricted` — is the document's sensitivity tier. This walks those folders and hands each file
to `UploadService.receive()`, the same entry point the upload form uses, so a document that
arrived from Drive is indistinguishable afterwards from one uploaded by hand. Nothing skips
validation, scanning, OCR, the quality gate or the Data Boundary Gateway.

    python drive_sync.py --dry-run
    python drive_sync.py
    python drive_sync.py --limit 5

There is no `--classification` flag, deliberately. The folder decides, so nobody has to remember
to tick a box, and a file filed somewhere other than those two folders is skipped rather than
given a tier nobody chose.

Re-running is safe and cheap. Unchanged files are recognised by Drive's `modifiedTime` and are
never downloaded again; an interrupted run is resumed by running it a second time.

Run it the way the workers are configured, or not at all: with `STORAGE_BACKEND=local` on the
host while the workers read MinIO, every file lands where they cannot see it and fails at SCAN
several minutes later in a different log. The preflight check below refuses that.
"""

import argparse
import logging
import sys

from bulk_ingest import preflight_storage
from src.core.config import settings
from src.db.engine import SessionLocal
from src.modules.connectors.drive.client import DriveClient
from src.modules.connectors.drive.credentials import DriveAuthError, DriveCredentials
from src.modules.connectors.drive.service import (
    DriveSyncError,
    DriveSyncService,
    SyncOutcome,
)
from src.storage.bucket_manager import BucketManager

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger("drive_sync")
logger.setLevel(logging.INFO)
logging.getLogger("src.modules.connectors").setLevel(logging.INFO)


def report(outcome: SyncOutcome, dry_run: bool) -> None:
    print()
    print("=" * 72)
    print(f"{'Found' if dry_run else 'Synced'}: {outcome.seen} file(s) under the watched folders")
    print("-" * 72)
    print(f"  {'would import' if dry_run else 'queued for processing'} : "
          f"{len(outcome.imported)}")
    print(f"  unchanged since last sync : {outcome.unchanged}")
    print(f"  already in the corpus     : {len(outcome.duplicates)}")
    print(f"  skipped                   : {len(outcome.skipped)}")
    print(f"  failed                    : {len(outcome.failed)}")
    print(f"  {'would retire' if dry_run else 'retired'} (gone from Drive) : "
          f"{len(outcome.removed)}")

    if outcome.escalated:
        print()
        print(f"TIER RAISED by {len(outcome.escalated)} duplicate(s):")
        for name in outcome.escalated[:20]:
            print(f"  {name}")
        print("  These files are already in the corpus at a lower tier. The copy was dropped and")
        print("  the existing document was raised to RESTRICTED. Reverse one with")
        print("  POST /api/v1/documents/{id}/classify if a file was filed in the wrong folder.")

    if outcome.owner_fallbacks:
        print()
        print(f"OWNER FELL BACK for {len(outcome.owner_fallbacks)} file(s):")
        for name, email in outcome.owner_fallbacks[:20]:
            print(f"  {name}\n      Drive owner {email} has no account here")
        print("  RESTRICTED documents among these are visible only to the fallback account and")
        print("  to admins. Register those people, or move the files to Public.")

    for label, rows in (("SKIPPED", outcome.skipped), ("FAILED", outcome.failed)):
        if rows:
            print(f"\n{label}:")
            for name, why in rows[:20]:
                print(f"  {name}\n      {why}")
            if len(rows) > 20:
                print(f"  ... and {len(rows) - 20} more")

    if outcome.removed:
        print("\nRETIRED (no longer in a watched Drive folder):")
        for name in outcome.removed[:20]:
            print(f"  {name}")
        if len(outcome.removed) > 20:
            print(f"  ... and {len(outcome.removed) - 20} more")
        print("  Hidden from search and answers. The files and audit trail are kept, so this is")
        print("  reversible: put the file back in the folder, or ask an admin to restore it.")

    if dry_run:
        print("\nDry run — nothing was imported, retired or changed. Re-run without --dry-run.")
        return

    print("\nThe workers process these in the background. Documents become searchable as they")
    print("reach LIVE; nothing further is required here.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be imported and retired, touching nothing.")
    parser.add_argument("--limit", type=int,
                        help="Stop after this many files. Disables retiring vanished documents, "
                             "since a partial listing cannot tell 'gone' from 'not looked at'.")
    args = parser.parse_args()

    if not settings.GDRIVE_ENABLED:
        raise SystemExit(
            "GDRIVE_ENABLED is false. Set it, along with GDRIVE_CREDENTIALS_FILE, the two folder "
            "IDs and GDRIVE_FALLBACK_OWNER_EMAIL, in .env. See DEPLOY.md."
        )

    credentials = DriveCredentials(settings.GDRIVE_CREDENTIALS_FILE)
    try:
        account = credentials.account_email
    except DriveAuthError as e:
        raise SystemExit(str(e)) from e

    logger.info("Reading Drive as %s", account or "an unknown service account")
    logger.info(
        "Public folder %s | Restricted folder %s",
        settings.GDRIVE_PUBLIC_FOLDER_ID or "(unset)",
        settings.GDRIVE_RESTRICTED_FOLDER_ID or "(unset)",
    )

    buckets = BucketManager()
    preflight_storage(buckets)

    try:
        with DriveClient(credentials) as client:
            service = DriveSyncService(
                client=client,
                session_factory=SessionLocal,
                public_folder_id=settings.GDRIVE_PUBLIC_FOLDER_ID,
                restricted_folder_id=settings.GDRIVE_RESTRICTED_FOLDER_ID,
                fallback_owner_email=settings.GDRIVE_FALLBACK_OWNER_EMAIL,
                max_bytes=settings.GDRIVE_MAX_FILE_BYTES,
                bucket_manager=buckets,
            )
            outcome = service.sync_once(dry_run=args.dry_run, limit=args.limit)
    except (DriveAuthError, DriveSyncError) as e:
        raise SystemExit(str(e)) from e

    if outcome.seen == 0:
        # Overwhelmingly the common first-run problem, and an empty folder looks identical to no
        # access from here, so say both.
        print(
            f"\nNothing found. Either the folders are empty, or they have not been shared with "
            f"{account or 'the service account'} — share the top-level folder with that address "
            f"as Viewer."
        )

    report(outcome, args.dry_run)
    return 1 if outcome.failed else 0


if __name__ == "__main__":
    sys.exit(main())
