"""Service-account credentials for the Drive connector.

A **service account**, not per-user OAuth and not an API key. An API key only reaches files
shared publicly, which the knowledge-base folder must never be. Per-user OAuth would mean every
colleague granting consent and the deployment storing a refresh token per person — a credential
store, a consent screen and a revocation path, for a feature whose whole point is one shared
folder. The service account sees exactly what has been shared with it and nothing else, which is
the property worth having: it cannot browse anyone's Drive.

The scope is **read-only and stays read-only**. The connector never writes to Drive, so deleting
a document in the knowledge base can never delete somebody's file, and a bug here cannot damage
the source of truth. Widening this is a decision about blast radius, not a convenience.

`google-auth` is used for this and only this: signing the service-account JWT and exchanging it
for an access token, then refreshing it before expiry. That is the part worth not hand-rolling.
Everything else is plain REST over httpx — see `client.py`.
"""

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Read-only, deliberately. See the module docstring.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class DriveAuthError(RuntimeError):
    """The key is missing, unreadable, or rejected by Google."""


class DriveCredentials:
    """Holds the service-account key and hands out a currently-valid access token.

    Tokens last an hour and a folder sync can outlive one, so `token()` refreshes on demand
    rather than once at construction. The lock is there because the Phase B worker may later add
    a second caller: refreshing twice concurrently is wasteful rather than harmful, but the state
    inside the credentials object is not documented as thread-safe.
    """

    def __init__(self, key_file: str):
        self._key_file = key_file
        self._creds = None
        self._lock = threading.Lock()

    def _load(self):
        if not self._key_file:
            raise DriveAuthError(
                "GDRIVE_CREDENTIALS_FILE is not set. Point it at the service-account JSON key."
            )
        path = Path(self._key_file)
        if not path.is_file():
            raise DriveAuthError(
                f"No service-account key at {path}. Copy the JSON key there, and keep it out of "
                f"the repository: it grants access to everything shared with that account."
            )
        try:
            from google.oauth2.service_account import Credentials
        except ImportError as e:  # pragma: no cover - the dependency is declared
            raise DriveAuthError(
                "google-auth is not installed. Run `uv sync` (it is declared in pyproject.toml)."
            ) from e

        try:
            return Credentials.from_service_account_file(str(path), scopes=SCOPES)
        except Exception as e:
            # Deliberately does not echo the file's contents into the message. The usual cause is
            # a truncated or wrong-format download, and a key fragment in a log is a leaked key.
            raise DriveAuthError(f"Could not read the service-account key at {path}: {e}") from e

    def token(self) -> str:
        """A valid access token, refreshed if the current one has expired."""
        from google.auth.transport.requests import Request

        with self._lock:
            if self._creds is None:
                self._creds = self._load()
                logger.info(
                    "Drive credentials loaded for %s",
                    getattr(self._creds, "service_account_email", "unknown service account"),
                )
            if not self._creds.valid:
                try:
                    self._creds.refresh(Request())
                except Exception as e:
                    raise DriveAuthError(
                        f"Google refused the service-account key: {e}. Check that the Drive API "
                        f"is enabled on the project and that the key has not been disabled."
                    ) from e
            return self._creds.token

    @property
    def account_email(self) -> str | None:
        """Who the folder has to be shared with.

        Reported by the command, because "nothing was found" is almost always this account not
        having been given access rather than an empty folder, and the two look identical from
        here.
        """
        if self._creds is None:
            try:
                self._creds = self._load()
            except DriveAuthError:
                return None
        return getattr(self._creds, "service_account_email", None)
