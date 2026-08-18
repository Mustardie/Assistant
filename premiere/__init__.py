"""Premiere Pro editing layer.

    VIDEO + STORY -> AI EDITOR -> EditPlan -> validation -> engine -> timeline

The creative side produces a structured EditPlan; this package validates it and
executes it inside Premiere through a CEP panel. The editing model never emits
ExtendScript -- the only thing that crosses the bridge is data describing
operations from ``premiere.catalog``.

Layout:
    catalog.py       every operation, as data (the editing interface)
    schema.py        types, selectors, time parsing, validation primitives
    validator.py     EditPlan validation (the gate before Premiere)
    engine.py        expansion, batching, history, partial-failure handling
    easing.py        curve maths and keyframe baking
    assets.py        generated text/shape overlays and subtitle files
    styles.py        reusable named text styles
    capabilities.py  measured capability discovery
    bridge.py        HTTP transport to the CEP panel
    history.py       operation log, revisions, checkpoints
    tool.py          the five agent-facing tools
    install.py       installs the CEP extension

The panel itself lives in ``extensions/PremiereBridge/``.
"""
from premiere.errors import (
    BridgeError, HostError, PremiereError, RevisionError, UnsupportedError,
    ValidationError,
)

__all__ = [
    "PremiereError", "ValidationError", "BridgeError", "HostError",
    "UnsupportedError", "RevisionError",
]
