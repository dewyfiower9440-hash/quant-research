"""Domain error hierarchy.

Raise these for expected failures. Wrap low-level errors at adapter boundaries
with context: ``raise ExternalServiceError("fetching user 42 failed") from e``.
"""


class AppError(Exception):
    """Base class for all expected application errors."""


class ValidationError(AppError):
    """Input or state violates a domain rule."""


class NotFoundError(AppError):
    """A requested entity does not exist."""


class ExternalServiceError(AppError):
    """A dependency (HTTP API, DB, filesystem) failed."""
