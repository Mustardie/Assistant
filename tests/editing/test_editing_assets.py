"""The asset library, matching, and placement.

Three things carry the weight.

**Bad silence is better than random annoying SFX.** Most of these tests assert
on a *refusal*: a placeholder with no candidate, a candidate that scored too
low, a good match at a bad moment, a sound whose sidecar said not to place it.
Each one has to end as a marker naming what it wanted -- never as a placed
sound, and never as nothing at all.

**An empty library is a valid input.** Nobody has a tagged sound library on day
one, and the pass has to produce a complete plan and a useful shopping list
with zero files on disk. That is asserted directly rather than left to
sampling.

**Assets never touch the rough cut.** V1 and A1 belong to Sessions 3-5;
everything here lands on tracks the plan adds, using ``clip.overwrite`` (which
does not ripple) rather than ``clip.insert`` (which does). Both halves of that
are pinned.

Nothing here needs FFmpeg, a GPU, a model server, Premiere or real media: the
"assets" are a few bytes each and every duration comes from a sidecar.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from editing.assets import execute as asset_execute
from editing.assets import indexer as asset_indexer
from editing.assets import library as asset_library
from editing.assets import match as asset_match
from editing.assets import report as asset_report
from editing.assets import sidecar as sidecar_module
from editing.assets.compile import AssetOptions, compile_assets
from editing.assets.place import PROTECTED_TRACKS, PlacementLimits
from editing.assets.schema import AssetLibrary, AssetPlacementPlan
from editing.errors import EditingError
from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.style import presets as style_presets
from editing.style.schema import LayerEvidence, LayerItem, LayeredEditPlan

SEQUENCE = "Nova Rough Cut"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def write_asset(root: Path, relative: str, meta=None, *, body=b"RIFF0000"):
    """A file in the library, optionally with a sidecar."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if meta is not None:
        side = path.with_name(path.stem + ".asset.json")
        side.write_text(
            meta if isinstance(meta, str) else json.dumps(meta),
            encoding="utf-8",
        )
    return path


@pytest.fixture
def asset_root(tmp_path) -> Path:
    return tmp_path / "assets"


@pytest.fixture
def stocked(asset_root) -> Path:
    """A small, well-tagged library covering most placeholder kinds."""
    write_asset(asset_root, "sfx/impact_boom_heavy.wav",
                {"intensity": "high", "duration": 1.4})
    write_asset(asset_root, "sfx/impact_thud.wav", {"duration": 0.9})
    write_asset(asset_root, "sfx/pop_cartoon_funny.wav",
                {"tags": ["funny", "pop", "cartoon"], "duration": 0.6})
    write_asset(asset_root, "sfx/whoosh_fast_01.wav", {"duration": 0.8})
    write_asset(asset_root, "sfx/whoosh_slow_02.wav", {"duration": 1.1})
    write_asset(asset_root, "music/tension_bed_loop.wav",
                {"loopable": True, "intensity": "low", "duration": 30.0,
                 "tags": ["tension", "bed", "drone"]})
    write_asset(asset_root, "music/main_theme_track.wav",
                {"duration": 200.0, "tags": ["theme", "music"]})
    write_asset(asset_root, "ambience/cave_room_tone_loop.wav",
                {"loopable": True, "duration": 45.0,
                 "tags": ["ambience", "cave", "room"]})
    write_asset(asset_root, "callout/arrow_red.png",
                {"tags": ["arrow", "pointer"]})
    write_asset(asset_root, "titles/title_plate_dark.png",
                {"tags": ["title", "plate", "background"]})
    return asset_root


def index(config, root: Path, **kw) -> AssetLibrary:
    """Index without probing: no FFmpeg, and every duration is a sidecar."""
    kw.setdefault("probe_durations", False)
    return asset_indexer.index_library(config, root=str(root), **kw)


def item(
    kind, start, *, end=None, priority=0.7, layer="audio", segments=(), **payload
) -> LayerItem:
    return LayerItem(
        item_id=f"li_{kind}_{start}",
        layer=layer,
        kind=kind,
        start=float(start),
        end=float(end if end is not None else start),
        priority=priority,
        style="fast_funny",
        reason=f"{kind} planned here",
        evidence=LayerEvidence(segment_ids=list(segments)),
        payload=dict(payload),
        premiere_ops=[{"op": "marker.add", "time": float(start)}],
    )


def layered(*items, duration=180.0, style="fast_funny") -> LayeredEditPlan:
    return LayeredEditPlan(
        sequence_name=SEQUENCE,
        style=style,
        items=list(items),
        cut_duration=duration,
        on_scratch=True,
        roughcut_executed=True,
    )


@pytest.fixture
def roughcut() -> RoughCutPlan:
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[ClipPlacement(
            placement_id="p_1", asset_id="a_test", source_file="/f/ep12.mp4",
            source_in=0.0, source_out=180.0, sequence_start=0.0,
        )],
        on_scratch=True,
    )
    plan.ops = [
        {"op": "project.import", "paths": ["/f/ep12.mp4"], "bin": "b"},
        {"op": "sequence.create", "name": SEQUENCE, "from_asset": "/f/ep12.mp4"},
        {"op": "sequence.activate", "name": SEQUENCE},
        {"op": "clip.append", "asset": "/f/ep12.mp4", "track": "V1",
         "in": 0.0, "out": 180.0},
    ]
    return plan


def compiled(layers, library, **kw):
    kw.setdefault("style", style_presets.get(layers.style or "fast_funny"))
    kw.setdefault("roughcut_executed", True)
    return compile_assets(layers, library, **kw)


class FakeEngine:
    def __init__(self, *, succeed=True):
        self.calls: list[dict] = []
        self.succeed = succeed

    def run(self, plan):
        self.calls.append(plan)
        if not self.succeed:
            return {"success": False, "error": "Premiere said no",
                    "code": "execution_failed"}
        return {"success": True,
                "results": [{"ok": True} for _ in plan.get("ops", [])]}


# ---------------------------------------------------------------------------
# Part 1/2 -- the library and the indexer
# ---------------------------------------------------------------------------

def test_init_creates_the_folders_and_the_documentation(asset_root):
    result = asset_library.initialise(asset_root)

    assert len(result["created"]) == len(asset_library.FOLDERS)
    for name in asset_library.FOLDERS:
        assert (asset_root / name).is_dir()
    assert (asset_root / "README.md").exists()
    assert (asset_root / "example.asset.json").exists()

    example = json.loads((asset_root / "example.asset.json").read_text("utf-8"))
    parsed = sidecar_module._clean(example)[0]
    assert parsed, "the generated example must itself be a valid sidecar"


def test_init_never_overwrites_anything(asset_root):
    asset_library.initialise(asset_root)
    (asset_root / "README.md").write_text("mine", encoding="utf-8")
    (asset_root / "sfx" / "keep.wav").write_bytes(b"x")

    again = asset_library.initialise(asset_root)

    assert again["created"] == []
    assert again["docs"] == []
    assert (asset_root / "README.md").read_text("utf-8") == "mine"
    assert (asset_root / "sfx" / "keep.wav").exists()


def test_indexing_an_empty_library_is_not_an_error(config, asset_root):
    asset_library.initialise(asset_root)
    library = index(config, asset_root)

    assert len(library) == 0
    assert library.stats()["total"] == 0
    assert any("No assets were found" in w for w in library.warnings)


def test_indexing_a_root_that_does_not_exist_says_so(config, tmp_path):
    library = index(config, tmp_path / "nope")
    assert len(library) == 0
    assert any("does not exist" in w for w in library.warnings)


def test_the_folder_decides_the_category(config, asset_root):
    write_asset(asset_root, "sfx/thing.wav")
    write_asset(asset_root, "music/thing.wav")
    write_asset(asset_root, "ambience/thing.wav")
    write_asset(asset_root, "callout/thing.png")
    write_asset(asset_root, "titles/thing.png")
    write_asset(asset_root, "loose.wav")

    by_path = {i.path: i for i in index(config, asset_root).items}
    got = {Path(p).parent.name: i.category for p, i in by_path.items()}

    assert got["sfx"] == "sfx"
    assert got["music"] == "music"
    assert got["ambience"] == "ambience"
    assert got["callout"] == "callout"
    assert got["titles"] == "title"
    assert got[asset_root.name] == "other"


def test_subfolders_become_tags(config, asset_root):
    write_asset(asset_root, "sfx/impacts/heavy/boom.wav")
    asset = index(config, asset_root).items[0]

    assert "impacts" in asset.tag_names
    assert "heavy" in asset.tag_names
    assert asset.category == "sfx"


def test_the_filename_becomes_tags(config, asset_root):
    write_asset(asset_root, "sfx/whoosh_fast_01.wav")
    asset = index(config, asset_root).items[0]

    assert {"whoosh", "fast"} <= asset.tag_names
    assert "01" not in asset.tag_names, "numbers say nothing"


def test_tags_remember_where_they_came_from(config, asset_root):
    write_asset(asset_root, "sfx/impacts/boom.wav", {"tags": ["cinematic"]})
    asset = index(config, asset_root).items[0]
    sources = {tag.name: tag.source for tag in asset.tags}

    assert sources["impacts"] == "folder"
    assert sources["boom"] == "filename"
    assert sources["cinematic"] == "sidecar"


def test_a_sidecar_tag_outranks_an_inferred_one(config, asset_root):
    write_asset(asset_root, "sfx/boom.wav", {"tags": ["boom"]})
    asset = index(config, asset_root).items[0]
    tag = next(t for t in asset.tags if t.name == "boom")
    assert tag.source == "sidecar"
    assert tag.confidence == 1.0


def test_the_filename_can_say_a_file_loops(config, asset_root):
    write_asset(asset_root, "ambience/wind_loop.wav")
    write_asset(asset_root, "ambience/wind_once.wav")
    by_name = {i.filename: i for i in index(config, asset_root).items}

    assert by_name["wind_loop.wav"].loopable
    assert not by_name["wind_once.wav"].loopable


def test_the_filename_can_say_a_file_is_heavy_or_soft(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav")
    write_asset(asset_root, "sfx/subtle_tick.wav")
    by_name = {i.filename: i for i in index(config, asset_root).items}

    assert by_name["impact_boom.wav"].intensity == "high"
    assert by_name["subtle_tick.wav"].intensity == "low"


@pytest.mark.parametrize("relative,media", [
    ("sfx/a.wav", "audio"), ("sfx/a.mp3", "audio"), ("sfx/a.flac", "audio"),
    ("callout/a.png", "image"), ("callout/a.webp", "image"),
    ("callout/a.mp4", "video"), ("titles/a.mogrt", "mogrt"),
])
def test_supported_types_are_recognised(config, asset_root, relative, media):
    write_asset(asset_root, relative)
    assert index(config, asset_root).items[0].media_type == media


def test_unsupported_files_are_skipped_with_a_reason(config, asset_root):
    write_asset(asset_root, "sfx/notes.aiff")
    library = index(config, asset_root)

    assert len(library) == 0
    assert library.skipped
    assert "not a supported asset type" in library.skipped[0]["reason"]


def test_build_and_cache_folders_are_never_descended_into(config, asset_root):
    write_asset(asset_root, "sfx/real.wav")
    write_asset(asset_root, "sfx/node_modules/fake.wav")
    write_asset(asset_root, "sfx/__pycache__/fake.wav")
    write_asset(asset_root, "sfx/.hidden/fake.wav")

    names = {i.filename for i in index(config, asset_root).items}
    assert names == {"real.wav"}


def test_a_file_that_vanished_is_marked_not_dropped(config, asset_root):
    path = write_asset(asset_root, "sfx/gone.wav")
    first = index(config, asset_root)
    assert len(first) == 1

    path.unlink()
    second = index(config, asset_root, previous=first)

    assert len(second) == 1
    assert second.items[0].missing is True
    assert not second.items[0].usable


def test_an_unchanged_file_keeps_its_measured_duration(config, asset_root):
    write_asset(asset_root, "sfx/a.wav")
    first = index(config, asset_root)
    first.items[0].duration = 1.25          # as if ffprobe had run

    second = index(config, asset_root, previous=first)
    assert second.items[0].duration == 1.25


def test_indexing_without_a_probe_leaves_durations_unknown(config, asset_root):
    write_asset(asset_root, "sfx/a.wav")
    library = index(config, asset_root)
    assert library.items[0].duration is None


# ---------------------------------------------------------------------------
# Part 3 -- sidecars
# ---------------------------------------------------------------------------

def test_a_sidecar_is_read_and_wins_over_inference(config, asset_root):
    write_asset(asset_root, "sfx/quiet_tick.wav", {
        "category": "music", "intensity": "high", "loopable": True,
        "bpm": 128, "duration": 12.5, "volume_adjust_db": -3.0,
        "preferred_styles": ["fast_funny"], "avoid_styles": ["minimal_clean"],
        "license_notes": "bought",
    })
    asset = index(config, asset_root).items[0]

    assert asset.has_sidecar
    assert asset.category == "music"       # beat the folder
    assert asset.intensity == "high"       # beat the filename
    assert asset.loopable is True
    assert asset.bpm == 128
    assert asset.duration == 12.5
    assert asset.volume_adjust_db == -3.0
    assert asset.preferred_styles == ["fast_funny"]
    assert asset.license_notes == "bought"


def test_the_sidecar_filename_pairs_with_the_whole_stem(tmp_path):
    assert sidecar_module.sidecar_path(tmp_path / "a.b.wav").name == "a.b.asset.json"


@pytest.mark.parametrize("body", [
    '{"tags": ["a",,]}',
    "not json at all",
    "[1, 2, 3]",
    "",
])
def test_an_invalid_sidecar_never_crashes_the_indexer(config, asset_root, body):
    write_asset(asset_root, "sfx/boom.wav", body)
    library = index(config, asset_root)

    assert len(library) == 1
    asset = library.items[0]
    assert asset.needs_review is True
    assert asset.safe_for_auto is False, "unreadable is not the same as safe"
    assert asset.review_reason
    assert any("sidecar" in w for w in library.warnings)


def test_one_bad_field_does_not_throw_away_the_good_ones(config, asset_root):
    write_asset(asset_root, "sfx/boom.wav", {
        "tags": ["impact", "boom"],
        "intensity": "ENORMOUS",
        "bpm": "quite fast",
        "loopable": "maybe",
        "looppable": True,
    })
    parsed = sidecar_module.load(asset_root / "sfx" / "boom.wav")

    assert parsed.ok is True
    assert not parsed.needs_review
    assert len(parsed.problems) == 4
    assert parsed.get("tags")

    asset = index(config, asset_root).items[0]
    assert asset.needs_review is False
    assert {"impact", "boom"} <= asset.tag_names
    assert asset.intensity == "high"       # fell back to the filename


def test_an_impossible_trim_is_dropped_rather_than_applied(asset_root):
    write_asset(asset_root, "sfx/boom.wav",
                {"start_offset": 5.0, "end_offset": 2.0})
    parsed = sidecar_module.load(asset_root / "sfx" / "boom.wav")

    assert "start_offset" not in parsed.data
    assert "end_offset" not in parsed.data
    assert any("not after" in problem for problem in parsed.problems)


def test_a_sidecar_can_hold_a_file_back_from_automatic_placement(
    config, asset_root
):
    write_asset(asset_root, "sfx/impact_boom.wav", {"safe_for_auto": False})
    asset = index(config, asset_root).items[0]

    assert asset.safe_for_auto is False
    assert asset.needs_review is False, "held back is not the same as broken"
    assert not asset.usable


def test_a_trim_shortens_the_effective_duration(config, asset_root):
    write_asset(asset_root, "sfx/boom.wav",
                {"duration": 10.0, "start_offset": 2.0, "end_offset": 6.0})
    asset = index(config, asset_root).items[0]
    assert asset.effective_duration == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Part 4 -- matching
# ---------------------------------------------------------------------------

def test_every_placeholder_kind_has_a_requirement():
    from editing.style.presets import LAYER_KINDS

    covered = set(asset_match.REQUIREMENTS) | set(asset_match.NOT_ASSET_BACKED)
    assert set(LAYER_KINDS) <= covered, (
        f"no policy for: {sorted(set(LAYER_KINDS) - covered)}"
    )


@pytest.mark.parametrize("kind,expected", [
    ("impact_sfx", "impact_boom_heavy.wav"),
    ("comedic_sfx", "pop_cartoon_funny.wav"),
    ("whoosh", "whoosh_fast_01.wav"),
    ("tension_bed", "tension_bed_loop.wav"),
    ("music_start", "main_theme_track.wav"),
    ("ambience", "cave_room_tone_loop.wav"),
    ("visual_callout", "arrow_red.png"),
    ("title_card", "title_plate_dark.png"),
])
def test_each_kind_picks_the_obvious_asset(config, stocked, kind, expected):
    library = index(config, stocked)
    best = asset_match.best_match(
        asset_match.rank_candidates(kind, library, slot_duration=10.0)
    )
    assert best is not None, f"{kind} found nothing"
    assert best.filename == expected


def test_a_name_that_says_what_it_is_matches_without_a_probe(config, asset_root):
    """The case that fails silently when tag weight is too low.

    With no ffprobe nothing has a duration and nothing earns the duration
    credit, so a file called ``whoosh_fast_01.wav`` has to clear the threshold
    on its name alone -- or a machine without FFmpeg places nothing at all.
    """
    write_asset(asset_root, "sfx/whoosh_fast_01.wav")
    library = index(config, asset_root)

    best = asset_match.best_match(
        asset_match.rank_candidates("whoosh", library)
    )
    assert best is not None
    assert best.filename == "whoosh_fast_01.wav"


def test_the_wrong_category_is_never_considered(config, stocked):
    library = index(config, stocked)
    matches = asset_match.rank_candidates("impact_sfx", library)
    assert all(
        library.by_id(m.asset_id).category in ("sfx", "transition")
        for m in matches
    )


def test_every_loser_keeps_the_reason_it_lost(config, stocked):
    library = index(config, stocked)
    matches = asset_match.rank_candidates("tension_bed", library)
    losers = [m for m in matches if not m.accepted]

    assert losers
    assert all(m.rejected for m in losers)


def test_a_non_looping_file_cannot_be_a_bed(config, asset_root):
    write_asset(asset_root, "music/tension_drone.wav", {"duration": 60.0})
    library = index(config, asset_root)
    matches = asset_match.rank_candidates("tension_bed", library)

    assert matches
    assert not matches[0].accepted
    assert "loopable" in matches[0].rejected


def test_safe_for_auto_gates_matching(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav",
                {"safe_for_auto": False, "duration": 1.0})
    library = index(config, asset_root)

    refused = asset_match.rank_candidates("impact_sfx", library)
    assert not refused[0].accepted
    assert "safe_for_auto" in refused[0].rejected

    allowed = asset_match.rank_candidates(
        "impact_sfx", library, allow_unsafe=True
    )
    assert allowed[0].accepted


def test_a_style_can_be_excluded_by_a_sidecar(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav",
                {"avoid_styles": ["minimal_clean"], "duration": 1.0})
    library = index(config, asset_root)

    assert asset_match.rank_candidates(
        "impact_sfx", library, style="minimal_clean"
    )[0].rejected
    assert asset_match.rank_candidates(
        "impact_sfx", library, style="fast_funny"
    )[0].accepted


def test_a_preferred_style_lifts_a_match(config, asset_root):
    write_asset(asset_root, "sfx/impact_one.wav", {"duration": 1.0})
    write_asset(asset_root, "sfx/impact_two.wav",
                {"duration": 1.0, "preferred_styles": ["fast_funny"]})
    library = index(config, asset_root)

    best = asset_match.best_match(
        asset_match.rank_candidates("impact_sfx", library, style="fast_funny")
    )
    assert best.filename == "impact_two.wav"


def test_a_one_shot_that_is_far_too_long_loses(config, asset_root):
    write_asset(asset_root, "sfx/impact_short.wav", {"duration": 1.0})
    write_asset(asset_root, "sfx/impact_long.wav", {"duration": 45.0})
    library = index(config, asset_root)

    matches = asset_match.rank_candidates("impact_sfx", library)
    assert matches[0].filename == "impact_short.wav"
    long_match = next(m for m in matches if m.filename == "impact_long.wav")
    assert long_match.score < matches[0].score


def test_a_repeated_asset_gives_way_to_an_unused_alternative(config, stocked):
    library = index(config, stocked)
    first = asset_match.best_match(
        asset_match.rank_candidates("whoosh", library)
    )
    second = asset_match.best_match(
        asset_match.rank_candidates("whoosh", library, used={first.asset_id: 1})
    )
    assert second.asset_id != first.asset_id


def test_the_only_asset_of_its_kind_may_repeat(config, asset_root):
    """Rotation is not rationing.

    A library with one arrow should use that arrow every time a callout is
    planned. Penalising the second use into rejection would mean only the first
    callout in a whole video ever got one.
    """
    write_asset(asset_root, "callout/arrow_red.png", {"tags": ["arrow"]})
    library = index(config, asset_root)
    asset_id = library.items[0].asset_id

    for uses in range(5):
        best = asset_match.best_match(asset_match.rank_candidates(
            "visual_callout", library, used={asset_id: uses}
        ))
        assert best is not None, f"refused after {uses} use(s)"
        assert best.asset_id == asset_id


def test_an_unknown_duration_is_neutral_not_a_penalty(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav")
    library = index(config, asset_root)
    match = asset_match.rank_candidates("impact_sfx", library)[0]

    assert match.accepted
    assert any("not judged" in why for why, _delta in match.reasons)


def test_coverage_reports_what_the_library_cannot_serve(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav", {"duration": 1.0})
    library = index(config, asset_root)
    got = asset_match.coverage(library)

    assert got["impact_sfx"]["candidates"] == 1
    assert got["ambience"]["candidates"] == 0
    assert got["title_card"]["candidates"] == 0


# ---------------------------------------------------------------------------
# Part 5/6 -- placement
# ---------------------------------------------------------------------------

def test_a_one_shot_is_placed_at_its_moment(config, stocked):
    library = index(config, stocked)
    plan = compiled(layered(item("impact_sfx", 40.0)), library)
    placement = plan.placements[0]

    assert placement.status == "placed"
    assert placement.track == "A2"
    overwrite = next(
        op for op in placement.premiere_ops if op["op"] == "clip.overwrite"
    )
    assert overwrite["time"] == pytest.approx(40.0)
    assert overwrite["out"] - overwrite["in"] == pytest.approx(1.4)
    assert any(op["op"] == "audio.gain" for op in placement.premiere_ops)


def test_a_bed_is_looped_to_cover_its_slot(config, stocked):
    library = index(config, stocked)
    plan = compiled(layered(item("tension_bed", 10.0, end=70.0)), library)
    placement = plan.placements[0]

    assert placement.status == "placed"
    assert placement.track == "A3"
    clips = [op for op in placement.premiere_ops if op["op"] == "clip.overwrite"]
    assert len(clips) == 2, "a 30s loop under a 60s slot is two copies"
    assert clips[0]["time"] == pytest.approx(10.0)
    assert clips[1]["time"] == pytest.approx(40.0)
    assert placement.payload["loops"] == 2


def test_a_bed_that_would_need_endless_looping_is_refused(config, asset_root):
    write_asset(asset_root, "music/tension_bed_loop.wav",
                {"loopable": True, "duration": 4.0, "tags": ["tension", "bed"]})
    library = index(config, asset_root)
    plan = compiled(layered(item("tension_bed", 0.0, end=600.0),
                            duration=600.0), library)

    assert plan.placements[0].status == "rejected"
    assert "loops" in plan.placements[0].reason


def test_a_track_shorter_than_its_section_runs_out_rather_than_being_refused(
    config, asset_root
):
    """Music that ends before the section does is ordinary editing."""
    write_asset(asset_root, "music/main_theme_track.wav",
                {"duration": 40.0, "tags": ["theme", "music"]})
    library = index(config, asset_root)
    plan = compiled(layered(item("music_start", 0.0), duration=180.0), library)
    placement = plan.placements[0]

    assert placement.status == "placed"
    assert placement.end == pytest.approx(40.0)
    assert "ends before the section" in placement.notes


def test_a_music_start_placeholder_gets_a_slot_from_the_cut(config, stocked):
    """Session 5's music_start is a point; a bed needs a range."""
    library = index(config, stocked)
    plan = compiled(
        layered(item("music_start", 0.0), duration=150.0), library
    )
    assert plan.placements[0].end > 0.0
    assert plan.placements[0].status == "placed"


def test_a_bed_over_dialogue_is_ducked_with_the_real_speech_ranges(
    config, stocked
):
    """The Session 5 unlock: there was no bed clip to duck, and now there is."""
    library = index(config, stocked)
    speech = item("duck_narration", 0.0, end=150.0)
    speech.payload["under"] = [{"start": 5.0, "end": 9.0},
                               {"start": 30.0, "end": 34.0}]
    plan = compiled(
        layered(item("tension_bed", 0.0, end=60.0), speech, duration=150.0),
        library,
    )
    bed = next(p for p in plan.placements if p.kind == "tension_bed")
    duck = next(op for op in bed.premiere_ops if op["op"] == "audio.duck")

    assert len(duck["under"]) == 2
    assert duck["duck_db"] < duck["base_db"]
    assert bed.payload["ducked_under"] == 2


def test_a_bed_with_no_dialogue_under_it_is_not_ducked(config, stocked):
    library = index(config, stocked)
    plan = compiled(layered(item("tension_bed", 0.0, end=60.0)), library)
    bed = plan.placements[0]

    assert not any(op["op"] == "audio.duck" for op in bed.premiere_ops)
    assert any(op["op"] == "audio.gain" for op in bed.premiere_ops)


def test_effects_are_never_placed_on_top_of_each_other(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(
            item("impact_sfx", 40.0, priority=0.9),
            item("comedic_sfx", 40.5, priority=0.8),
            item("whoosh", 41.0, priority=0.7),
        ),
        library,
    )
    placed = plan.placed()

    assert len(placed) == 1, "three one-shots inside a second is spam"
    held = [p for p in plan.placements if p.status == "unsafe"]
    assert len(held) == 2
    assert all("sfx_spam" in p.risks for p in held)


def test_the_strongest_moment_survives_the_spam_rule(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(
            item("impact_sfx", 40.0, priority=0.4),
            item("comedic_sfx", 40.5, priority=0.95),
        ),
        library,
    )
    assert plan.placed()[0].kind == "comedic_sfx"


def test_effects_are_capped_per_minute(config, stocked):
    library = index(config, stocked)
    limits = PlacementLimits(min_sfx_gap=0.5, max_sfx_per_minute=2)
    plan = compiled(
        layered(*[item("impact_sfx", 10.0 + n * 3.0) for n in range(6)]),
        library, limits=limits,
    )
    assert len(plan.placed()) == 2
    assert any("a minute" in p.reason for p in plan.of_status("unsafe"))


def test_only_so_many_sounds_may_play_at_once(config, stocked):
    library = index(config, stocked)
    limits = PlacementLimits(max_concurrent_audio=1, min_sfx_gap=0.0)
    plan = compiled(
        layered(
            item("tension_bed", 0.0, end=60.0, priority=0.9),
            item("impact_sfx", 20.0, priority=0.5),
        ),
        library, limits=limits,
    )
    assert len(plan.placed()) == 1
    stacked = plan.of_status("unsafe")[0]
    assert "stacked_audio" in stacked.risks


def test_two_clips_never_overlap_on_one_track(config, stocked):
    """Correctness, not taste.

    ``clip.overwrite`` destroys what is under it, so two beds overlapping on
    A3 would look fine in the plan and silently eat each other on the
    timeline.
    """
    library = index(config, stocked)
    plan = compiled(
        layered(
            item("tension_bed", 0.0, end=60.0, priority=0.9),
            item("tension_bed", 30.0, end=90.0, priority=0.5),
        ),
        library,
    )
    assert len(plan.placed()) == 1
    refused = plan.of_status("unsafe")[0]
    assert "would overwrite" in refused.reason

    by_track: dict = {}
    for placement in plan.placed():
        by_track.setdefault(placement.track, []).append(placement)
    for entries in by_track.values():
        entries.sort(key=lambda p: p.start)
        for earlier, later in zip(entries, entries[1:]):
            assert later.start >= earlier.end


def test_a_graphic_is_placed_in_a_safe_zone(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(item("visual_callout", 30.0, end=32.0, layer="emphasis")),
        library,
    )
    placement = plan.placements[0]

    assert placement.status == "placed"
    assert placement.track == "V3"
    assert placement.payload["zone"] != "center"
    op = placement.premiere_ops[0]
    assert op["op"] == "graphic.image"
    assert op["position"] != [0.5, 0.5]


def test_a_graphic_is_never_placed_over_an_open_menu(
    config, stocked, rich_hud_timeline
):
    library = index(config, stocked)
    layers = layered(
        item("visual_callout", 30.0, end=32.0, layer="emphasis",
             segments=[rich_hud_timeline.segments[0].segment_id])
    )
    plan = compiled(layers, library, timeline=rich_hud_timeline)
    placement = plan.placements[0]

    assert placement.status == "unsafe"
    assert "hud_risk" in placement.risks
    assert placement.is_marker


def test_a_callout_is_never_left_on_screen_for_long(config, stocked):
    library = index(config, stocked)
    limits = PlacementLimits(max_callout_seconds=2.0)
    plan = compiled(
        layered(item("visual_callout", 10.0, end=40.0, layer="emphasis")),
        library, limits=limits,
    )
    assert plan.placements[0].duration == pytest.approx(2.0)


def test_graphics_do_not_stack(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(
            item("visual_callout", 10.0, end=12.0, layer="emphasis",
                 priority=0.9),
            item("callout_label", 10.5, end=12.5, layer="caption",
                 priority=0.6),
        ),
        library,
    )
    assert len(plan.placed()) == 1
    assert "too_many_overlays" in plan.of_status("unsafe")[0].risks


def test_a_mogrt_is_recorded_rather_than_placed(config, asset_root):
    write_asset(asset_root, "titles/title_plate.mogrt", {"tags": ["title"]})
    library = index(config, asset_root)
    plan = compiled(layered(item("title_card", 0.0, end=3.0, layer="title")),
                    library)
    placement = plan.placements[0]

    assert placement.status == "marker_only"
    assert "unsupported_media" in placement.risks
    assert "mogrt" in placement.reason.lower()


def test_a_sidecar_level_beats_the_category_default(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav",
                {"duration": 1.0, "volume_adjust_db": -2.5})
    library = index(config, asset_root)
    plan = compiled(layered(item("impact_sfx", 10.0)), library)
    gain = next(
        op for op in plan.placements[0].premiere_ops if op["op"] == "audio.gain"
    )
    assert gain["db"] == pytest.approx(-2.5)


def test_a_sidecar_trim_reaches_the_placed_clip(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav",
                {"duration": 10.0, "start_offset": 3.0, "end_offset": 4.5})
    library = index(config, asset_root)
    plan = compiled(layered(item("impact_sfx", 10.0)), library)
    clip = next(
        op for op in plan.placements[0].premiere_ops
        if op["op"] == "clip.overwrite"
    )
    assert clip["in"] == pytest.approx(3.0)
    assert clip["out"] == pytest.approx(4.5)


# ---------------------------------------------------------------------------
# Part 7 -- the plan
# ---------------------------------------------------------------------------

def test_an_empty_library_still_produces_a_complete_plan(config, asset_root):
    asset_library.initialise(asset_root)
    library = index(config, asset_root)
    layers = layered(
        item("impact_sfx", 10.0), item("whoosh", 30.0),
        item("tension_bed", 40.0, end=80.0),
        item("visual_callout", 90.0, end=92.0, layer="emphasis"),
    )
    plan = compiled(layers, library)

    assert len(plan.placements) == 4
    assert plan.placed() == []
    assert len(plan.missing()) == 4
    assert all(p.is_marker for p in plan.placements)
    assert plan.ops, "a plan of markers is still a plan"
    asset_execute.dry_run(plan)
    assert plan.dry_run_passed is True


def test_nothing_of_that_kind_and_nothing_good_enough_are_different(
    config, asset_root
):
    """Two different problems with two different fixes."""
    write_asset(asset_root, "sfx/random_noise.wav", {"duration": 40.0})
    library = index(config, asset_root)
    plan = compiled(
        layered(item("impact_sfx", 10.0), item("ambience", 20.0, end=60.0)),
        library,
    )
    by_kind = {p.kind: p for p in plan.placements}

    assert by_kind["impact_sfx"].status == "rejected"
    assert "low_score" in by_kind["impact_sfx"].risks
    assert by_kind["ambience"].status == "missing"
    assert "no_asset" in by_kind["ambience"].risks


def test_every_placement_that_places_nothing_leaves_a_marker(config, asset_root):
    library = index(config, asset_root)
    plan = compiled(layered(item("impact_sfx", 10.0)), library)

    assert plan.placements[0].premiere_ops
    assert plan.placements[0].is_marker
    comment = plan.placements[0].premiere_ops[0]["comment"]
    assert "IMPACT SFX" in comment
    assert "missing" in comment


def test_a_markers_only_pass_matches_but_places_nothing(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(item("impact_sfx", 10.0)), library,
        options=AssetOptions(markers_only=True),
    )
    placement = plan.placements[0]

    assert placement.status == "marker_only"
    assert placement.asset_filename, "the match is still recorded"
    assert {op["op"] for op in plan.ops} <= {"sequence.activate", "marker.add"}


def test_kinds_that_are_notes_rather_than_sounds_are_left_alone(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(
            item("silence_hold", 10.0, end=14.0),
            item("beat_marker", 20.0),
            item("punch_in", 30.0),
        ),
        library,
    )
    assert plan.placements == []
    assert any("no placeholders" in w for w in plan.warnings)


def test_the_critic_can_veto_a_graphic(config, stocked):
    from editing.critic.schema import RevisionRecommendation, RevisionSet

    library = index(config, stocked)
    revisions = RevisionSet(sequence_name=SEQUENCE, revisions=[
        RevisionRecommendation(
            revision_id="rv_1", issue="hud_hidden", confidence=0.8,
            start=30.0, end=32.0,
        ),
    ])
    plan = compiled(
        layered(item("visual_callout", 30.0, end=32.0, layer="emphasis")),
        library, revisions=revisions,
    )
    assert plan.placements[0].status == "unsafe"
    assert "hud_risk" in plan.placements[0].risks


def test_the_plan_round_trips_through_json(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(item("impact_sfx", 10.0), item("whoosh", 40.0)), library
    )
    restored = AssetPlacementPlan.from_dict(
        json.loads(json.dumps(plan.to_dict()))
    )

    assert restored.ops == plan.ops
    assert len(restored) == len(plan)
    assert restored.tracks == plan.tracks
    assert [p.status for p in restored.placements] == [
        p.status for p in plan.placements
    ]


def test_the_plan_never_targets_the_rough_cuts_tracks(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(
            item("impact_sfx", 10.0),
            item("tension_bed", 40.0, end=80.0),
            item("visual_callout", 100.0, end=102.0, layer="emphasis"),
        ),
        library,
    )
    for op in plan.ops:
        track = str(op.get("track") or "").upper()
        if track:
            assert track not in PROTECTED_TRACKS


def test_a_protected_track_cannot_even_be_configured():
    with pytest.raises(ValueError):
        AssetOptions(tracks={"sfx": "A1"}).resolved_tracks()


def test_operations_run_in_an_order_that_works(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(item("impact_sfx", 10.0),
                item("visual_callout", 40.0, end=42.0, layer="emphasis")),
        library,
    )
    names = [op["op"] for op in plan.ops]

    assert names[0] == "sequence.activate"
    assert names.index("project.import") < names.index("clip.overwrite")
    assert names.index("track.add") < names.index("clip.overwrite")
    assert names.index("clip.overwrite") < names.index("audio.gain")
    assert names[-1] == "marker.add" or "marker.add" not in names


# ---------------------------------------------------------------------------
# Execution guards
# ---------------------------------------------------------------------------

@pytest.fixture
def runnable(config, stocked):
    library = index(config, stocked)
    plan = compiled(
        layered(item("impact_sfx", 10.0), item("whoosh", 40.0)), library
    )
    asset_execute.dry_run(plan)
    return plan


def test_the_dry_run_validates_offline(runnable):
    assert runnable.dry_run_passed is True
    assert runnable.explanation
    assert runnable.dry_run_error is None


def test_an_empty_plan_fails_the_dry_run_with_a_reason():
    plan = AssetPlacementPlan(sequence_name=SEQUENCE)
    asset_execute.dry_run(plan)
    assert plan.dry_run_passed is False
    assert plan.dry_run_error["code"] == "empty_plan"


def test_the_allowlist_is_the_whole_guarantee():
    emitted = {
        "sequence.activate", "project.import", "track.add", "clip.overwrite",
        "graphic.image", "audio.gain", "audio.fade", "audio.duck", "marker.add",
    }
    assert emitted == set(asset_execute.ALLOWED_OPS)
    assert "clip.insert" not in asset_execute.ALLOWED_OPS, (
        "insert ripples; asset placement must never move an existing clip"
    )


@pytest.mark.parametrize("op", [
    {"op": "clip.insert", "asset": "/a.wav", "track": "A2", "time": 1.0},
    {"op": "clip.trim", "clip": {"track": "A2", "index": 0}, "edge": "out",
     "by": 1.0},
    {"op": "clip.remove", "clip": {"track": "A2", "index": 0}},
    {"op": "sequence.create", "name": "Something Else"},
    {"op": "project.save"},
    {"op": "track.remove", "track": "A2"},
])
def test_anything_that_could_move_or_destroy_a_clip_is_refused(runnable, op):
    runnable.ops.append(op)
    asset_execute.dry_run(runnable)

    assert runnable.dry_run_passed is False
    assert runnable.dry_run_error["code"] == "forbidden_operation"
    assert op["op"] in runnable.dry_run_error["error"]


@pytest.mark.parametrize("track", ["V1", "A1"])
def test_placing_on_the_rough_cuts_own_track_is_refused(runnable, track):
    runnable.ops.append({
        "op": "clip.overwrite", "asset": "/a.wav", "track": track, "time": 1.0,
    })
    asset_execute.dry_run(runnable)

    assert runnable.dry_run_passed is False
    assert runnable.dry_run_error["code"] == "protected_track"
    assert track in runnable.dry_run_error["error"]


def test_placing_a_clip_with_no_track_is_refused(runnable):
    runnable.ops.append({"op": "clip.overwrite", "asset": "/a.wav", "time": 1.0})
    asset_execute.dry_run(runnable)
    assert runnable.dry_run_error["code"] == "protected_track"
    assert "without naming a track" in runnable.dry_run_error["error"]


def test_importing_from_outside_the_library_is_refused(runnable, tmp_path):
    stray = tmp_path / "elsewhere" / "mystery.wav"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"x")
    runnable.ops.append({"op": "project.import", "paths": [str(stray)]})
    asset_execute.dry_run(runnable)

    assert runnable.dry_run_passed is False
    assert runnable.dry_run_error["code"] == "import_outside_library"


def test_plan_only_validates_nothing_and_runs_nothing(runnable):
    engine = FakeEngine()
    report = asset_execute.run(runnable, mode="plan_only", engine=engine)
    assert report.executed is False
    assert report.dry_run_passed is False
    assert engine.calls == []


def test_a_dry_run_never_reaches_the_engine(runnable, roughcut):
    engine = FakeEngine()
    report = asset_execute.run(runnable, mode="dry_run", roughcut=roughcut,
                               engine=engine)
    assert report.executed is False
    assert report.dry_run_passed is True
    assert engine.calls == []


def test_execution_refuses_when_the_dry_run_fails(runnable, roughcut):
    runnable.ops.append({"op": "project.save"})
    engine = FakeEngine()
    report = asset_execute.run(runnable, mode="execute", roughcut=roughcut,
                               engine=engine)
    assert report.executed is False
    assert report.refused_reason
    assert engine.calls == []


def test_execution_validates_again_rather_than_trusting_a_stored_pass(
    runnable, roughcut
):
    assert runnable.dry_run_passed is True
    runnable.ops.append({"op": "clip.remove", "clip": {"track": "A2",
                                                       "index": 0}})
    engine = FakeEngine()
    report = asset_execute.run(runnable, mode="execute", roughcut=roughcut,
                               engine=engine)
    assert report.executed is False
    assert engine.calls == []


def test_execution_refuses_a_plan_that_does_not_activate_its_target(
    runnable, roughcut
):
    runnable.ops[0] = {"op": "sequence.activate", "name": "Someone's Real Edit"}
    engine = FakeEngine()
    report = asset_execute.run(runnable, mode="execute", roughcut=roughcut,
                               engine=engine)
    assert report.executed is False
    assert report.on_scratch is False
    assert engine.calls == []


def test_execution_refuses_when_the_sequence_was_never_built(config, stocked):
    library = index(config, stocked)
    layers = layered(item("impact_sfx", 10.0))
    layers.roughcut_executed = False
    plan = compile_assets(
        layers, library, style=style_presets.get("fast_funny"),
        roughcut_executed=False,
    )
    asset_execute.dry_run(plan)
    engine = FakeEngine()
    report = asset_execute.run(plan, mode="execute", engine=engine)

    assert report.executed is False
    assert "no record" in report.refused_reason
    assert engine.calls == []


def test_execution_refuses_when_an_asset_file_has_gone(runnable, roughcut,
                                                       stocked):
    """A plan built this morning can name a file that moved this afternoon."""
    for path in runnable.assets_used():
        Path(path).unlink()
    engine = FakeEngine()
    report = asset_execute.run(runnable, mode="execute", roughcut=roughcut,
                               engine=engine)

    assert report.executed is False
    assert report.error["code"] == "asset_missing"
    assert engine.calls == []


def test_a_plan_that_passes_every_guard_runs(runnable, roughcut):
    engine = FakeEngine()
    report = asset_execute.run(runnable, mode="execute", roughcut=roughcut,
                               engine=engine)

    assert report.executed is True
    assert report.on_scratch is True
    assert report.operations_succeeded == len(runnable.ops)
    assert len(engine.calls) == 1
    assert engine.calls[0].get("dry_run") is not True
    assert runnable.executed is True


def test_a_premiere_failure_is_reported_rather_than_raised(runnable, roughcut):
    report = asset_execute.run(runnable, mode="execute", roughcut=roughcut,
                               engine=FakeEngine(succeed=False))
    assert report.executed is False
    assert report.error["error"] == "Premiere said no"


def test_an_unknown_mode_is_a_usage_error(runnable):
    with pytest.raises(EditingError):
        asset_execute.run(runnable, mode="just-place-it")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_the_shopping_list_groups_by_what_to_go_and_find(config, asset_root):
    library = index(config, asset_root)
    plan = compiled(
        layered(item("whoosh", 10.0), item("whoosh", 40.0),
                item("whoosh", 70.0), item("impact_sfx", 100.0)),
        library,
    )
    text = asset_report.render_missing(plan)

    assert "whoosh  x3" in text
    assert "put in : assets/sfx/" in text
    assert text.count("wanted :") == 2, "grouped by kind, not by moment"


def test_the_report_leads_with_what_is_missing(config, asset_root):
    library = index(config, asset_root)
    plan = compiled(layered(item("whoosh", 10.0)), library)
    text = asset_report.render(plan, library=library)

    assert text.index("MISSING") < text.index("PLACED")
    assert "never" in text and "V1 or A1" in text


def test_validation_explains_every_kind_of_problem(config, asset_root):
    write_asset(asset_root, "sfx/broken.wav", "{not json")
    write_asset(asset_root, "sfx/held.wav", {"safe_for_auto": False})
    write_asset(asset_root, "sfx/notes.aiff")
    library = index(config, asset_root)
    text = asset_report.render_validation(library)

    assert "NEEDS REVIEW" in text
    assert "HELD BACK BY CHOICE" in text
    assert "SKIPPED" in text


def test_showing_an_asset_explains_where_its_tags_came_from(config, asset_root):
    write_asset(asset_root, "sfx/impacts/boom.wav", {"tags": ["cinematic"]})
    library = index(config, asset_root)
    text = asset_report.render_asset(library, library.items[0])

    assert "folder" in text and "filename" in text and "sidecar" in text
    assert "impact_sfx" in text, "it should say what this could be used for"


def test_the_library_report_names_the_gaps(config, asset_root):
    write_asset(asset_root, "sfx/impact_boom.wav", {"duration": 1.0})
    library = index(config, asset_root)
    text = asset_report.render_library(library)

    assert "impact_sfx" in text
    assert "ambience" in text


# ---------------------------------------------------------------------------
# Pipeline and CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def rich_hud_timeline():
    """A timeline whose first segment has an inventory open."""
    from editing.align import build_timeline
    from editing.schema import MediaAsset, TimeRange, UIState, VisualEvent

    asset = MediaAsset(asset_id="a_test", path="/f/ep12.mp4",
                       filename="ep12.mp4", duration=200.0)
    event = VisualEvent(
        event_id="e_0", source_file=asset.path, asset_id=asset.asset_id,
        start=0.0, end=60.0, confidence=0.9, environment="base",
        actions=["building"], importance="setup",
        suggested_range=TimeRange(0.0, 60.0), model="test",
    )
    event.ui = UIState(inventory_open=True)
    return build_timeline([asset], {asset.asset_id: [event]}, {})


@pytest.fixture
def staged(config, stocked, roughcut):
    """A rough cut, a layered edit and an indexed library, all on disk."""
    from dataclasses import replace
    from editing.config import SamplingConfig
    from editing.pipeline import build_pipeline
    from editing.roughcut.schema import ExecutionReport

    pipeline = build_pipeline(replace(config), SamplingConfig())
    pipeline.write_rough_cut(roughcut)
    pipeline.write_execution_report(ExecutionReport(
        mode="execute_on_scratch", executed=True, sequence_name=SEQUENCE,
        on_scratch=True, dry_run_passed=True,
    ))
    layers = layered(
        item("impact_sfx", 10.0), item("whoosh", 40.0),
        item("tension_bed", 60.0, end=120.0),
        item("visual_callout", 130.0, end=132.0, layer="emphasis"),
    )
    pipeline.write_layers(layers)
    pipeline.index_assets(root=str(stocked), probe_durations=False)
    return pipeline, stocked


def test_the_pipeline_runs_the_whole_pass(staged):
    pipeline, root = staged
    plan = pipeline.asset_plan(root=str(root))

    assert plan.dry_run_passed is True
    assert plan.placed()
    assert (pipeline.config.asset_library_dir / "structure.placement.json").exists()
    assert (pipeline.config.asset_library_dir / "structure.placement.txt").exists()
    assert pipeline.load_asset_plan().ops == plan.ops


def test_planning_works_before_the_library_is_ever_indexed(config, roughcut):
    from dataclasses import replace
    from editing.config import SamplingConfig
    from editing.pipeline import build_pipeline

    pipeline = build_pipeline(replace(config), SamplingConfig())
    pipeline.write_rough_cut(roughcut)
    pipeline.write_layers(layered(item("impact_sfx", 10.0)))

    plan = pipeline.asset_plan()
    assert plan.stats()["missing"] == 1
    assert plan.dry_run_passed is True


def test_the_layered_plan_survives_an_asset_pass(staged):
    pipeline, root = staged
    before = (pipeline.config.layers_dir / "structure.json").read_text("utf-8")
    pipeline.asset_plan(root=str(root))
    after = (pipeline.config.layers_dir / "structure.json").read_text("utf-8")
    assert before == after


def run_cli(argv, capsys):
    from editing.cli import main

    code = main(argv)
    return code, capsys.readouterr()


def test_the_cli_initialises_a_library(tmp_path, capsys):
    root = tmp_path / "lib"
    code, captured = run_cli([
        "assets", "init", "--root", str(root),
        "--output-dir", str(tmp_path / "out"), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    assert json.loads(captured.out)["success"] is True
    assert (root / "sfx").is_dir()


def test_the_cli_indexes_and_lists(staged, capsys):
    pipeline, root = staged
    code, captured = run_cli([
        "assets", "list", "--root", str(root),
        "--output-dir", str(pipeline.config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["count"] == 10


def test_the_cli_refuses_to_execute_without_yes(staged, capsys):
    pipeline, root = staged
    pipeline.asset_plan(root=str(root))
    code, captured = run_cli([
        "assets", "execute", "--output-dir", str(pipeline.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 1
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert "--yes" in payload["hint"]
    assert not (
        pipeline.config.asset_library_dir / "structure.placement-execution.json"
    ).exists()


def test_the_cli_dry_run_places_nothing(staged, capsys):
    pipeline, root = staged
    pipeline.asset_plan(root=str(root))
    code, captured = run_cli([
        "assets", "dry-run", "--output-dir", str(pipeline.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    assert json.loads(captured.out)["report"]["executed"] is False


def test_the_cli_shows_missing_and_deferred(staged, capsys):
    pipeline, root = staged
    pipeline.asset_plan(root=str(root))
    for command in ("show-missing", "show-deferred"):
        code, captured = run_cli([
            "assets", command,
            "--output-dir", str(pipeline.config.output_dir), "--no-premiere",
            "--json", "-q",
        ], capsys)
        assert code == 0
        assert json.loads(captured.out)["success"] is True


def test_the_cli_explains_one_match(staged, capsys):
    pipeline, root = staged
    code, captured = run_cli([
        "assets", "match", "whoosh", "--root", str(root),
        "--output-dir", str(pipeline.config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["kind"] == "whoosh"
    assert payload["matches"]
    assert payload["matches"][0]["reasons"]
