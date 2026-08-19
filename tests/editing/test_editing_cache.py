"""Cache keys, hits, misses and invalidation.

The property under test is narrow but critical: a hit must mean *the stored
value is the value this run would have computed*. So every input that changes
the result -- file content, model, sampling config, schema version -- has a
test proving it changes the key, and every input that does not must not.
"""
from __future__ import annotations

import json

import pytest

from editing.cache import Cache, CacheStats, canonical_key
from editing.config import SCHEMA_VERSION, SamplingConfig
from editing.fingerprint import (
    asset_id_for, content_hash, fingerprint, normalise_path,
)


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------

def test_canonical_key_ignores_dict_ordering():
    assert canonical_key({"a": 1, "b": 2}) == canonical_key({"b": 2, "a": 1})


def test_canonical_key_distinguishes_values():
    assert canonical_key({"a": 1}) != canonical_key({"a": 2})


def test_key_includes_the_kind(cache):
    assert cache.key("visual", x=1) != cache.key("transcript", x=1)


def test_key_changes_with_the_model(cache):
    first = cache.key("visual", file={"h": "abc"}, model="Qwen3-VL-8B-Instruct")
    second = cache.key("visual", file={"h": "abc"}, model="some-other-model")
    assert first != second


def test_key_changes_with_the_sampling_config(cache):
    base = SamplingConfig().validated()
    denser = SamplingConfig(frames_per_window=8).validated()
    assert cache.key("visual", sampling=base.cache_key_part()) != cache.key(
        "visual", sampling=denser.cache_key_part()
    )


def test_key_changes_with_file_content(cache, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"first version")
    first = cache.key("visual", file=fingerprint(path).cache_key_part())
    path.write_bytes(b"a completely different second version")
    second = cache.key("visual", file=fingerprint(path).cache_key_part())
    assert first != second


def test_key_is_stable_for_an_unchanged_file(cache, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"unchanged")
    mark = fingerprint(path)
    assert cache.key("visual", file=mark.cache_key_part()) == cache.key(
        "visual", file=fingerprint(path).cache_key_part()
    )


def test_sampling_configs_that_clamp_alike_share_a_key():
    """Two nonsense configs that clamp to the same thing are the same config."""
    first = SamplingConfig(frames_per_window=-4).validated()
    second = SamplingConfig(frames_per_window=0).validated()
    assert first.cache_key_part() == second.cache_key_part()


# ---------------------------------------------------------------------------
# Hits and misses
# ---------------------------------------------------------------------------

def test_miss_then_hit(cache):
    key = cache.key("visual", window=1)
    assert cache.get("visual", key) is None
    assert cache.stats.misses == 1

    cache.put("visual", key, {"environment": "cave"})
    assert cache.get("visual", key) == {"environment": "cave"}
    assert cache.stats.hits == 1
    assert cache.stats.writes == 1


def test_hit_rate(cache):
    key = cache.key("visual", window=1)
    cache.get("visual", key)              # miss
    cache.put("visual", key, {"a": 1})
    cache.get("visual", key)              # hit
    cache.get("visual", key)              # hit
    assert cache.stats.lookups == 3
    assert cache.stats.hit_rate == pytest.approx(2 / 3)


def test_disabled_cache_never_hits(tmp_path):
    cache = Cache(root=tmp_path / "cache", enabled=False)
    key = cache.key("visual", window=1)
    cache.put("visual", key, {"a": 1})
    assert cache.get("visual", key) is None
    assert cache.stats.writes == 0
    assert not (tmp_path / "cache").exists()


def test_get_or_compute_runs_once(cache):
    calls = []

    def compute():
        calls.append(1)
        return {"value": len(calls)}

    key = cache.key("visual", window=1)
    assert cache.get_or_compute("visual", key, compute) == {"value": 1}
    assert cache.get_or_compute("visual", key, compute) == {"value": 1}
    assert len(calls) == 1


def test_get_or_compute_does_not_store_a_failure(cache):
    key = cache.key("visual", window=1)
    with pytest.raises(RuntimeError):
        cache.get_or_compute("visual", key, lambda: (_ for _ in ()).throw(
            RuntimeError("model down")
        ))
    # A transient failure must not be baked in and returned forever after.
    assert cache.get("visual", key) is None


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_a_corrupt_entry_degrades_to_a_miss(cache):
    key = cache.key("visual", window=1)
    cache.put("visual", key, {"a": 1})
    cache.path_for("visual", key).write_text("{ half written", encoding="utf-8")

    assert cache.get("visual", key) is None
    # The bad entry is removed, so the next run rewrites it cleanly.
    assert not cache.path_for("visual", key).exists()


def test_an_entry_without_a_value_is_discarded(cache):
    key = cache.key("visual", window=1)
    target = cache.path_for("visual", key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"kind": "visual"}), encoding="utf-8")
    assert cache.get("visual", key) is None


def test_entries_are_sharded_into_subdirectories(cache):
    key = cache.key("visual", window=1)
    cache.put("visual", key, {"a": 1})
    path = cache.path_for("visual", key)
    assert path.parent.name == key[:2]
    assert path.parent.parent.name == "visual"


def test_unserialisable_values_do_not_raise(cache):
    """A cache write failure must never take down an analysis run."""
    key = cache.key("visual", window=1)
    cache.put("visual", key, {"handle": object()})   # default=str handles it
    assert cache.get("visual", key) is not None


def test_stored_entry_records_its_provenance(cache):
    key = cache.key("visual", window=1)
    cache.put("visual", key, {"a": 1}, meta={"path": "/f/clip.mp4"})
    entry = json.loads(cache.path_for("visual", key).read_text(encoding="utf-8"))
    assert entry["meta"]["path"] == "/f/clip.mp4"
    assert entry["schema_version"] == SCHEMA_VERSION
    assert entry["stored_at"]


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def test_clear_one_kind_leaves_the_others(cache):
    cache.put("visual", cache.key("visual", w=1), {"a": 1})
    cache.put("transcript", cache.key("transcript", a=1), {"b": 2})

    assert cache.clear("visual") == 1
    assert cache.get("visual", cache.key("visual", w=1)) is None
    assert cache.get("transcript", cache.key("transcript", a=1)) == {"b": 2}


def test_clear_everything(cache):
    cache.put("visual", cache.key("visual", w=1), {"a": 1})
    cache.put("motion", cache.key("motion", a=1), [[0.0, 0.1]])
    assert cache.clear() == 2


def test_info_counts_by_kind(cache):
    cache.put("visual", cache.key("visual", w=1), {"a": 1})
    cache.put("visual", cache.key("visual", w=2), {"a": 2})
    cache.put("probe", cache.key("probe", a=1), {"duration": 1.0})

    info = cache.info()
    assert info["total_entries"] == 3
    assert info["kinds"]["visual"]["entries"] == 2
    assert info["kinds"]["probe"]["entries"] == 1
    assert info["total_bytes"] > 0


def test_info_on_an_empty_cache(cache):
    info = cache.info()
    assert info["total_entries"] == 0
    assert info["kinds"] == {}


def test_cache_stats_to_dict():
    stats = CacheStats(hits=3, misses=1, writes=1)
    assert stats.to_dict() == {
        "hits": 3, "misses": 1, "writes": 1, "hit_rate": 0.75
    }


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def test_asset_id_is_stable_and_path_derived(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"content")
    first = asset_id_for(path)
    path.write_bytes(b"different content entirely")
    # A re-export to the same path is the same asset; only the cache key moves.
    assert asset_id_for(path) == first
    assert first.startswith("a_")


def test_asset_id_differs_between_paths(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")
    assert asset_id_for(tmp_path / "a.mp4") != asset_id_for(tmp_path / "b.mp4")


def test_normalise_path_is_absolute():
    from pathlib import Path
    assert Path(normalise_path("some/relative/path.mp4")).is_absolute()


def test_content_hash_detects_a_change(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"a" * 1000)
    first = content_hash(path)
    path.write_bytes(b"b" * 1000)
    assert content_hash(path) != first


def test_content_hash_includes_the_size(tmp_path):
    """Head and tail can coincide; the size must still separate them."""
    short = tmp_path / "short.mp4"
    long = tmp_path / "long.mp4"
    short.write_bytes(b"x" * 100)
    long.write_bytes(b"x" * 200)
    assert content_hash(short) != content_hash(long)


def test_fingerprint_of_a_missing_file_raises_with_a_hint(tmp_path):
    from editing.errors import FootageError

    with pytest.raises(FootageError) as caught:
        fingerprint(tmp_path / "gone.mp4")
    assert "drive" in caught.value.hint


def test_fingerprint_can_skip_hashing(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 10)
    assert fingerprint(path, hash_content=False).content_hash == ""
