"""Local asset libraries, and putting real sounds and graphics on the timeline.

    local folders -> indexed library (+ optional sidecar metadata)
    layered edit plan + library + style -> one placement per placeholder
        -> matching, then the mixing safety rules
        -> ordered Premiere operations
        -> offline dry run
        -> (only on an explicit --yes) applied to the same scratch sequence

    schema.py    AssetItem, AssetTag, AssetLibrary, AssetMatch, AssetPlacement
    library.py   the folder layout, and creating it
    indexer.py   scanning, fingerprinting, probing, inference
    sidecar.py   optional ``<filename>.asset.json`` metadata
    match.py     placeholder -> ranked candidates, with every rejection kept
    place.py     a chosen asset -> operations, under the mixing rules
    compile.py   the whole pass, and the five possible outcomes
    execute.py   three modes, the allowlist, the track and import guards
    report.py    the shopping list, the refusals, and what was placed

**Local files only.** Nothing here downloads anything, and nothing modifies a
source file: trimming, gain, fades and ducking are all expressed as operations
on a *placed clip*.

Two rules shape the whole package.

**Bad silence is better than random annoying SFX.** Every rule is written to
make refusing cheap: a placeholder with no candidate, no good candidate, or no
safe moment becomes a marker naming what it wanted. On most libraries most of a
plan is markers, and that is the design working -- the marker list doubles as a
shopping list.

**Assets never touch V1 or A1.** Everything lands on tracks the plan adds, and
placement uses ``clip.overwrite`` (which does not ripple) rather than
``clip.insert`` (which does). So the rough cut underneath is untouched, nothing
computed by Sessions 3-5 moves, and the whole pass is undone by deleting the
added tracks and the markers.
"""
from editing.assets.compile import AssetOptions, compile_assets
from editing.assets.execute import (
    ALLOWED_OPS, MODES, check_allowed, check_imports, check_tracks, run,
    summarise, targets_scratch_sequence,
)
from editing.assets.execute import dry_run as dry_run_assets
from editing.assets.indexer import (
    index_library, media_type_for, tags_from_filename,
)
from editing.assets.library import (
    FOLDERS, category_for, default_root, index_path, initialise, resolve_root,
)
from editing.assets.match import (
    REQUIREMENTS, Requirement, best_match, coverage, rank_candidates,
    requirement_for,
)
from editing.assets.place import DEFAULT_TRACKS, PROTECTED_TRACKS, PlacementLimits
from editing.assets.report import (
    render, render_asset, render_deferred, render_library, render_missing,
    render_validation,
)
from editing.assets.schema import (
    CATEGORIES, MEDIA_TYPES, PLACEMENT_STATUSES, SUPPORTED_EXTENSIONS,
    AssetItem, AssetLibrary, AssetMatch, AssetPlacement, AssetPlacementPlan,
    AssetTag, asset_id_for,
)
from editing.assets.sidecar import Sidecar, load as load_sidecar, sidecar_path

__all__ = [
    # schema
    "AssetItem", "AssetTag", "AssetLibrary", "AssetMatch", "AssetPlacement",
    "AssetPlacementPlan", "CATEGORIES", "MEDIA_TYPES", "PLACEMENT_STATUSES",
    "SUPPORTED_EXTENSIONS", "asset_id_for",
    # library
    "FOLDERS", "initialise", "default_root", "resolve_root", "index_path",
    "category_for",
    # indexing
    "index_library", "media_type_for", "tags_from_filename",
    # sidecar
    "Sidecar", "load_sidecar", "sidecar_path",
    # matching
    "Requirement", "REQUIREMENTS", "rank_candidates", "best_match",
    "requirement_for", "coverage",
    # placement
    "PlacementLimits", "DEFAULT_TRACKS", "PROTECTED_TRACKS",
    "compile_assets", "AssetOptions",
    # execution
    "dry_run_assets", "run", "targets_scratch_sequence", "check_allowed",
    "check_tracks", "check_imports", "summarise", "MODES", "ALLOWED_OPS",
    # reports
    "render", "render_missing", "render_deferred", "render_library",
    "render_validation", "render_asset",
]
