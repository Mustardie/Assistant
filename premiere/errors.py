"""Error types for the Premiere editing layer.

Every failure that crosses the Python/Premiere seam ends up as one of these.
They all carry a machine-readable ``code`` so the calling model can branch on
the failure kind instead of pattern-matching English, and a ``hint`` that says
what to do differently -- the agent loop in this repo retries on advice, so a
useless error message costs a whole retry cycle.
"""
from __future__ import annotations


class PremiereError(Exception):
    """Base for everything raised by the premiere package."""

    code = "premiere_error"

    def __init__(self, message: str, *, hint: str = "", detail=None, path: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail
        self.path = path

    def to_dict(self) -> dict:
        out = {"success": False, "code": self.code, "error": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.path:
            out["at"] = self.path
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    def __str__(self) -> str:
        base = self.message
        if self.path:
            base = f"{self.path}: {base}"
        if self.hint:
            base = f"{base} ({self.hint})"
        return base


class ValidationError(PremiereError):
    """The EditPlan was malformed. Nothing was sent to Premiere."""

    code = "validation_error"


class BridgeError(PremiereError):
    """Could not talk to the Premiere panel at all."""

    code = "bridge_error"


class HostError(PremiereError):
    """The panel was reached but ExtendScript raised inside Premiere."""

    code = "host_error"


class UnsupportedError(PremiereError):
    """Premiere's scripting API genuinely cannot do this.

    Raised (or returned) rather than silently no-op'ing, so the model learns
    the real capability boundary instead of believing a lie. ``alternative``
    names the supported approach that gets closest.
    """

    code = "unsupported"

    def __init__(self, message: str, *, alternative: str = "", **kw):
        hint = kw.pop("hint", "") or (
            f"Not exposed by the Premiere scripting API. Use instead: {alternative}"
            if alternative else "Not exposed by the Premiere scripting API."
        )
        super().__init__(message, hint=hint, **kw)
        self.alternative = alternative

    def to_dict(self) -> dict:
        out = super().to_dict()
        if self.alternative:
            out["alternative"] = self.alternative
        return out


class RevisionError(PremiereError):
    """The timeline changed under us since the plan was written."""

    code = "revision_mismatch"
