"""Custom error hierarchy for worker and pipeline processing."""


class PipelineProcessingError(Exception):
    """Base exception for pipeline processing errors."""


class TransientProcessingError(PipelineProcessingError):
    """Transient failure that should be retried with backoff (e.g. storage/clamav/database glitch)."""


class PermanentProcessingError(PipelineProcessingError):
    """Permanent failure that should NOT be retried (e.g. corrupt/malicious PDF, missing document)."""
