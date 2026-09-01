"""Typed application errors.

Engine code raises these. The API layer has a single handler that turns any
:class:`AppError` into the structured JSON shape the frontend understands:

    {"error": {"code": "...", "message": "...", "detail": {...}}}

`message` is shown to the user, so write it the way the UI copy rules require:
say what to do, not only what failed. `detail` carries anything a developer
needs and is never required to be readable.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error the engine raises deliberately."""

    code: str = "app_error"
    http_status: int = 500
    default_message: str = "Something went wrong. Check the log file for details."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.detail = detail or {}
        if code:
            self.code = code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the structured error shape used by the API."""
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


class NotFoundError(AppError):
    """A project, sheet, file or record does not exist."""

    code = "not_found"
    http_status = 404
    default_message = "That item no longer exists. Refresh and try again."


class ValidationError(AppError):
    """The request or the user input is not usable."""

    code = "validation_error"
    http_status = 422
    default_message = "Some of the values are not valid. Check the highlighted fields."


class UnreadableFileError(AppError):
    """A drawing file is corrupt, locked or password-protected.

    Never fails the whole run. The file goes to the quarantine list and the
    user is told which files could not be opened and why.
    """

    code = "unreadable_file"
    http_status = 422
    default_message = "This file could not be opened. It may be password-protected or damaged."


class AlignmentFailedError(AppError):
    """The two sheets could not be aligned well enough to compare.

    A bad alignment is refused rather than reported as a wrong result.
    """

    code = "alignment_failed"
    http_status = 422
    default_message = (
        "These two sheets could not be aligned reliably. "
        "Compare them side by side, or set the alignment points manually."
    )


class SchemaVersionError(AppError):
    """The project file was written by a different version of the application."""

    code = "schema_version"
    http_status = 409
    default_message = "This project file was made by a different version of the application."


class ConfigurationError(AppError):
    """The environment is missing something the application needs to start."""

    code = "configuration_error"
    http_status = 500
    default_message = "The application is not set up correctly on this machine."
