"""Capability discovery.

The single most useful thing this layer can tell a creative model is what is
actually true about the Premiere in front of it. Effect names differ between
installs, Lumetri parameter names shift between versions, the QE DOM is
undocumented and can vanish, and several APIs (captions, MOGRT, time
remapping) exist on some builds and not others.

So instead of hardcoding a belief about any of that, ``probe`` asks the host
and caches the answer. Everything reported here is measured, not assumed --
including the keyframe interpolation modes, which are verified by writing them
to a real parameter and reading back what stuck.

A probe result is cached for the session because it involves real work in
Premiere (it applies and removes a test effect), but ``refresh=True`` forces a
re-measure after the user installs a plugin.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from premiere import catalog
from premiere.errors import PremiereError

logger = logging.getLogger("nova.premiere.capabilities")

_CACHE: Optional[dict] = None
_CACHE_AT = 0.0
_TTL = 600.0


def probe(engine, *, refresh: bool = False) -> dict:
    """Measure what this Premiere install genuinely supports."""
    global _CACHE, _CACHE_AT
    if _CACHE is not None and not refresh and (time.time() - _CACHE_AT) < _TTL:
        return _CACHE

    report: dict = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "measured on the running Premiere host",
    }

    try:
        host = engine.bridge.call("caps.probe", {}) or {}
    except PremiereError as exc:
        # A failed probe is itself information -- report it rather than
        # returning a fabricated capability list.
        return {
            "available": False,
            "error": exc.message,
            "hint": getattr(exc, "hint", ""),
            "note": "Could not reach Premiere, so nothing below was measured.",
        }

    report["available"] = True
    report.update({
        "premiere_version": host.get("version"),
        "app": host.get("app"),
        "project": host.get("project"),
        "sequence": host.get("sequence"),
        "qe_dom": host.get("qe", {}),
        "apis": host.get("apis", {}),
        "interpolation_modes": host.get("interpolation", {}),
        "counts": host.get("counts", {}),
    })

    report["unsupported"] = catalog.unsupported() + _HOST_LIMITS
    report["operations"] = len(catalog.OPS)
    report["categories"] = catalog.categories()

    report["text_engine"] = _text_engine_report(host)
    report["summary"] = _summarise(report)

    _CACHE, _CACHE_AT = report, time.time()
    return report


#: Limits that are properties of Premiere's scripting API itself rather than of
#: any one operation. Stated plainly here so a model can read the boundary in
#: one place instead of discovering it by failing.
_HOST_LIMITS = [
    {
        "capability": "keyframe bezier handles (graph editor velocity)",
        "reason": "The scripting API sets a keyframe's interpolation MODE but "
                  "exposes no read/write access to the per-keyframe velocity "
                  "handles the graph editor draws.",
        "alternative": "keyframe.bake / animate with a custom easing -- samples "
                       "the exact curve into keyframes",
    },
    {
        "capability": "native Essential Graphics text layer creation",
        "reason": "There is no scripting entry point that constructs an EGP text "
                  "layer from nothing.",
        "alternative": "text.create with engine='render' (image overlay), or "
                       "engine='mogrt' with a registered .mogrt template whose "
                       "parameters are then script-driven",
    },
    {
        "capability": "effect preset (.prfpset) application",
        "reason": "No scripting entry point applies a saved effect preset to a "
                  "track item.",
        "alternative": "color.grade with explicit values, or a .cube LUT via "
                       "color.lut",
    },
    {
        "capability": "Lumetri curves and colour wheels",
        "reason": "Exposed as opaque composite parameters with no scriptable "
                  "control-point list.",
        "alternative": "color.grade tonal controls, or bake the look into a .cube "
                       "LUT and apply it with color.lut",
    },
    {
        "capability": "app.undo()",
        "reason": "Premiere's scripting API has no undo entry point.",
        "alternative": "history.undo -- implemented as a project-state restore "
                       "from an automatic pre-batch checkpoint",
    },
]


def _text_engine_report(host: dict) -> dict:
    from premiere.styles import default_mogrt
    template = default_mogrt()
    apis = host.get("apis", {})
    return {
        "render": {
            "available": True,
            "live_text": False,
            "note": "Rasterised PNG overlay. Full typography control; not "
                    "editable inside Premiere after placement.",
        },
        "mogrt": {
            "available": bool(apis.get("importMGT")) and bool(template),
            "template": template,
            "live_text": True,
            "note": "Requires a registered .mogrt template "
                    "(premiere.styles.set_default_mogrt)."
                    if not template else "Template registered.",
        },
        "active": "mogrt" if (apis.get("importMGT") and template) else "render",
    }


def _summarise(report: dict) -> str:
    apis = report.get("apis", {}) or {}
    qe = report.get("qe_dom", {}) or {}
    available = [name for name, ok in apis.items() if ok]
    missing = [name for name, ok in apis.items() if not ok]
    parts = [
        f"Premiere {report.get('premiere_version') or 'unknown'}",
        f"QE DOM: {'available' if qe.get('available') else 'unavailable'}",
        f"{len(available)} host APIs present",
    ]
    if missing:
        parts.append(f"missing: {', '.join(sorted(missing))}")
    return "; ".join(parts)


def invalidate() -> None:
    global _CACHE
    _CACHE = None
