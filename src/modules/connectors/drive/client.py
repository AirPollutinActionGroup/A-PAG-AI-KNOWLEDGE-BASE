"""A small REST client for the parts of the Drive API this connector uses.

Four calls: list the folders under a root, list the files in a set of folders, download an
ordinary file, export a Google-native one. `google-api-python-client` would bring its own HTTP
stack, a discovery-document cache and protobuf to wrap the same four URLs, in an image that
already carries httpx — so this is httpx, and `google-auth` is used only for the token.

Two things here are not stylistic:

**Paging is mandatory, not an optimisation.** `files.list` returns 100 items by default and
silently stops there. A folder of 300 documents would import 100 of them and report success, and
nothing in the result says a page was dropped. Every listing follows `nextPageToken` to the end.

**Shared drives need asking for.** `supportsAllDrives` and `includeItemsFromAllDrives` are off by
default, so a folder living on a Shared Drive — which is how most organisations actually share
things — returns an empty list rather than an error. An empty result and "no access" look
identical from here, which is the failure this flag prevents.
"""

import logging
from collections.abc import Iterator
from typing import Self

import httpx

from src.modules.connectors.drive.credentials import DriveCredentials
from src.modules.connectors.drive.export_map import FOLDER_MIME
from src.modules.connectors.drive.models import DriveFile

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/drive/v3"

# Everything the connector needs about a file, requested in one go. Drive returns only `id` and
# `name` unless asked, and a missing `modifiedTime` would silently disable change detection —
# every file would look unchanged forever.
FILE_FIELDS = "id,name,mimeType,modifiedTime,size,owners(emailAddress),parents,trashed"
LIST_FIELDS = f"nextPageToken,files({FILE_FIELDS})"

PAGE_SIZE = 200


class DriveApiError(RuntimeError):
    """Drive refused a request. Carries the status so callers can tell 404 from 403."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DriveClient:
    """Reads Drive. Never writes to it — the credentials are read-only scoped."""

    def __init__(
        self,
        credentials: DriveCredentials,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self._creds = credentials
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- requests

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._creds.token()}"}
        headers.update(kwargs.pop("headers", {}))
        try:
            response = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            # Network-level. Worth distinguishing in the message from "Google said no", because
            # the fix is completely different: one is the VM's egress, the other is sharing.
            raise DriveApiError(f"Could not reach the Drive API: {e}") from e

        if response.status_code >= 400:
            raise DriveApiError(
                f"Drive API {response.status_code} for {url}: {response.text[:400]}",
                status_code=response.status_code,
            )
        return response

    def _paged(self, params: dict) -> Iterator[dict]:
        """Walks `nextPageToken` to the end. See the module docstring for why this is not
        optional."""
        page_token = None
        pages = 0
        while True:
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            payload = self._request("GET", f"{API_ROOT}/files", params=query).json()
            yield from payload.get("files", [])
            pages += 1
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        logger.debug("Drive listing completed in %d page(s)", pages)

    @staticmethod
    def _shared_drive_params() -> dict:
        return {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}

    # ---------------------------------------------------------------- listings

    def list_children(self, folder_id: str, *, folders_only: bool = False) -> list[DriveFile]:
        """Direct children of one folder. Trashed items are excluded by the query."""
        q = f"'{folder_id}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        params = {
            "q": q,
            "fields": LIST_FIELDS,
            "pageSize": PAGE_SIZE,
            # A stable order so two runs over an unchanged folder behave identically, which is
            # what makes a dry run worth reading.
            "orderBy": "name",
            **self._shared_drive_params(),
        }
        return [DriveFile.from_api(item) for item in self._paged(params)]

    def walk_folder_tree(self, root_id: str, *, max_depth: int = 10) -> dict[str, int]:
        """Every folder at or under `root_id`, mapped to its depth below the root.

        Used to resolve a file's tier from the folder it sits in. Descending matters: people make
        subfolders, and a document filed in `Restricted/2026/` is no less restricted for it.

        `max_depth` is a guard, not a policy. Drive allows a folder to be its own ancestor through
        shortcuts and multi-parenting, and an unbounded walk would not terminate; a visited set
        handles the ordinary cycle, and the depth cap handles the pathological one.
        """
        depths: dict[str, int] = {root_id: 0}
        frontier = [(root_id, 0)]
        while frontier:
            folder_id, depth = frontier.pop()
            if depth >= max_depth:
                logger.warning(
                    "Stopping the folder walk at depth %d under %s — deeper subfolders are not "
                    "being watched.", max_depth, root_id,
                )
                continue
            for child in self.list_children(folder_id, folders_only=True):
                if child.file_id in depths:
                    continue
                depths[child.file_id] = depth + 1
                frontier.append((child.file_id, depth + 1))
        return depths

    def get_file(self, file_id: str) -> DriveFile:
        params = {"fields": FILE_FIELDS, **self._shared_drive_params()}
        payload = self._request("GET", f"{API_ROOT}/files/{file_id}", params=params).json()
        return DriveFile.from_api(payload)

    # ---------------------------------------------------------------- content

    def download(self, file_id: str) -> bytes:
        """The bytes of an ordinary file."""
        params = {"alt": "media", **self._shared_drive_params()}
        return self._request("GET", f"{API_ROOT}/files/{file_id}", params=params).content

    def export(self, file_id: str, target_mime: str) -> bytes:
        """A Google-native document converted to `target_mime`. See `export_map.py`."""
        params = {"mimeType": target_mime, **self._shared_drive_params()}
        return self._request("GET", f"{API_ROOT}/files/{file_id}/export", params=params).content

    # ------------------------------------------------- Phase B: incremental sync

    def get_start_page_token(self) -> str:
        """A bookmark in Drive's change feed, for the worker that replaces full listings."""
        params = self._shared_drive_params()
        return self._request("GET", f"{API_ROOT}/changes/startPageToken", params=params).json()[
            "startPageToken"
        ]

    def list_changes(self, page_token: str) -> tuple[list[dict], str | None, str | None]:
        """Changes since `page_token`.

        Returns (changes, next_page_token, new_start_page_token). The caller must persist the new
        start token **only after** the changes it was given are durably queued — advancing it
        first loses every change in that page permanently, with nothing to replay from.
        """
        params = {
            "pageToken": page_token,
            "fields": f"changes(fileId,removed,file({FILE_FIELDS})),nextPageToken,newStartPageToken",
            "pageSize": PAGE_SIZE,
            **self._shared_drive_params(),
        }
        payload = self._request("GET", f"{API_ROOT}/changes", params=params).json()
        return (
            payload.get("changes", []),
            payload.get("nextPageToken"),
            payload.get("newStartPageToken"),
        )
