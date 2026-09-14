"""Single source of truth for object-storage keys.

`UploadService` writes the quarantine object in the API process; `ScanJobHandler` reads it back in
the worker process. Deriving the key independently in each place means a silent retrieval failure
the moment the two disagree about the extension, so both go through here.
"""

import uuid

from src.modules.document_pipeline.formats import FORMATS, PDF_MIME, spec_for
from src.modules.document_pipeline.models import Document

_FALLBACK_EXTENSION = FORMATS[PDF_MIME].extension


def extension_for(mime_type: str) -> str:
    spec = spec_for(mime_type)
    return spec.extension if spec else _FALLBACK_EXTENSION


def build_quarantine_key(document_id: uuid.UUID, mime_type: str) -> str:
    return f"{document_id}{extension_for(mime_type)}"


def quarantine_key_for(doc: Document) -> str:
    """Resolves the key of a document's quarantine object.

    Prefers the `quarantine_path` persisted at upload ("<bucket>/<key>") over re-deriving, so a
    document uploaded under an older key format stays retrievable. Falls back to deriving the key
    once promotion has cleared `quarantine_path`.
    """
    if doc.quarantine_path:
        return doc.quarantine_path.rsplit("/", 1)[-1]
    return build_quarantine_key(doc.id, doc.mime_type)


def build_raw_key(sha256: str, mime_type: str) -> str:
    """Content-addressed key for the promoted object.

    The extension is kept so a later extraction stage can dispatch on format from `raw_path` alone.
    Identical bytes hash identically and therefore always carry the same extension.
    """
    return f"{sha256}{extension_for(mime_type)}"
