"""Failure types for the editing structure layer.

These mirror the shape of ``premiere.errors``: a machine-readable ``code``, a
``hint`` that says what to do differently, and a ``detail`` payload. The CLI
prints them as JSON, so an automated caller can branch on ``code`` instead of
parsing English.
"""
from __future__ import annotations


class EditingError(Exception):
    """Base for everything raised by the editing package."""

    code = "editing_error"

    def __init__(self, message: str, *, hint: str = "", detail=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"success": False, "code": self.code, "error": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    def __str__(self) -> str:
        return f"{self.message} ({self.hint})" if self.hint else self.message


class FootageError(EditingError):
    """A footage folder or media file could not be read."""

    code = "footage_error"


class TranscriptError(EditingError):
    """A transcript could not be obtained, parsed or normalised."""

    code = "transcript_error"


class VisualError(EditingError):
    """Frame extraction or visual analysis failed."""

    code = "visual_error"


class ModelError(EditingError):
    """The vision model was unreachable or returned something unusable."""

    code = "model_error"


class ToolMissingError(EditingError):
    """An external binary (ffmpeg/ffprobe) is required and not installed."""

    code = "tool_missing"

    def __init__(self, message: str, **kw):
        kw.setdefault(
            "hint",
            "Install FFmpeg and make sure ffmpeg/ffprobe are on PATH, or set "
            "EDITING_FFMPEG / EDITING_FFPROBE to their full paths.",
        )
        super().__init__(message, **kw)
