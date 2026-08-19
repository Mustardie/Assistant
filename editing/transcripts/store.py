"""Where transcripts come from, and where they are kept.

Two storage roles, on purpose:

``transcripts/<asset_id>.json`` is the **durable** copy. A transcript the user
exported from Premiere by hand and imported is expensive to reproduce -- it
must not be thrown away because a file's mtime changed when the footage was
copied to another drive. Loading a durable copy whose fingerprint no longer
matches returns it *with* a staleness flag rather than silently discarding or
silently trusting it.

The ``Cache`` entry is the **derived** copy, keyed on the full fingerprint, and
exists so a repeated ``transcript pull`` costs nothing.

Resolution order for "get me this asset's transcript" is: durable store, then
Premiere's Speech to Text, then a sidecar file sitting next to the media. The
manual import path writes into the durable store, so anything the user imports
wins from then on.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from editing.cache import Cache
from editing.config import TRANSCRIPT_EXTENSIONS, EditingConfig
from editing.errors import TranscriptError
from editing.fingerprint import Fingerprint, fingerprint
from editing.schema import MediaAsset, Transcript
from editing.transcripts import normalize
from editing.transcripts.premiere_source import MANUAL_EXPORT_HINT, pull

logger = logging.getLogger("nova.editing.transcripts.store")


@dataclass
class TranscriptResolution:
    """How an asset's transcript was obtained -- or why it was not."""

    asset_id: str
    transcript: Optional[Transcript] = None
    source: str = "none"
    origin: str = ""
    stale: bool = False
    note: str = ""

    @property
    def found(self) -> bool:
        return self.transcript is not None and len(self.transcript) > 0

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "found": self.found,
            "source": self.source,
            "origin": self.origin,
            "stale": self.stale,
            "entries": len(self.transcript) if self.transcript else 0,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Durable store
# ---------------------------------------------------------------------------

def store_path(config: EditingConfig, asset_id: str) -> Path:
    return config.transcripts_dir / f"{asset_id}.json"


def save(
    config: EditingConfig,
    transcript: Transcript,
    *,
    mark: Optional[Fingerprint] = None,
    cache: Optional[Cache] = None,
) -> Path:
    """Write the durable copy (and the derived cache entry when given one)."""
    config.transcripts_dir.mkdir(parents=True, exist_ok=True)
    target = store_path(config, transcript.asset_id)
    document = transcript.to_dict()
    document["fingerprint"] = mark.to_dict() if mark else {}
    document["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if cache is not None and mark is not None:
        cache.put(
            "transcript",
            cache.key("transcript", file=mark.cache_key_part(), source=transcript.source),
            transcript.to_dict(),
            meta={"asset_id": transcript.asset_id, "source": transcript.source},
        )
    return target


def load(
    config: EditingConfig,
    asset_id: str,
    *,
    mark: Optional[Fingerprint] = None,
) -> tuple[Optional[Transcript], bool]:
    """Load the durable copy. Returns (transcript, stale).

    ``stale`` is True when the transcript was made for different file content
    than ``mark`` describes. The transcript is still returned -- deciding
    whether a re-transcribe is worth it belongs to the caller, and a stale
    transcript is usually still far better than none.
    """
    target = store_path(config, asset_id)
    if not target.exists():
        return None, False
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable transcript %s: %s", target, exc)
        return None, False

    transcript = Transcript.from_dict(document)
    stale = False
    stored = document.get("fingerprint") or {}
    if mark is not None and stored.get("content_hash"):
        stale = stored.get("content_hash") != mark.content_hash
    return transcript, stale


# ---------------------------------------------------------------------------
# Sidecar discovery
# ---------------------------------------------------------------------------

def find_sidecar(
    media_path: str | Path,
    *,
    extra_dirs: Optional[list[Path]] = None,
) -> Optional[Path]:
    """Find a transcript file exported alongside the footage.

    Looks for ``clip.srt``, ``clip.vtt``, ``clip.json``... beside the media and
    in any extra directories given, preferring the richer formats: SRT and VTT
    carry per-line timing, plain text often does not.
    """
    media = Path(media_path).expanduser()
    directories = [media.parent] + list(extra_dirs or [])
    stem = media.stem

    for extension in TRANSCRIPT_EXTENSIONS:
        for directory in directories:
            for candidate in (
                directory / f"{stem}{extension}",
                directory / f"{stem}.transcript{extension}",
            ):
                if candidate.exists() and candidate.is_file():
                    return candidate
    return None


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------

def import_file(
    config: EditingConfig,
    asset: MediaAsset,
    path: str | Path,
    *,
    cache: Optional[Cache] = None,
    mark: Optional[Fingerprint] = None,
) -> Transcript:
    """Parse a transcript file, normalise it against the asset, and store it."""
    entries, source = normalize.parse_file(path)
    entries = normalize.normalize_entries(
        entries, max_duration=asset.duration or None
    )
    if not entries:
        raise TranscriptError(
            f"No transcript entries survived normalisation for {asset.filename}",
            hint="Every line fell outside the media duration, or had no text. "
                 "Check the transcript belongs to this clip.",
        )

    transcript = Transcript(
        asset_id=asset.asset_id,
        source=source,
        source_path=str(Path(path).expanduser()),
        entries=entries,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        note=f"Imported from {Path(path).name}",
    )
    save(config, transcript, mark=mark or _mark_for(asset), cache=cache)
    return transcript


def _mark_for(asset: MediaAsset) -> Optional[Fingerprint]:
    """Best-effort fingerprint; a missing file must not block an import."""
    try:
        return fingerprint(asset.path)
    except Exception:  # noqa: BLE001 - offline media is a normal state here
        return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(
    config: EditingConfig,
    asset: MediaAsset,
    *,
    cache: Optional[Cache] = None,
    bridge=None,
    use_premiere: Optional[bool] = None,
    use_sidecar: bool = True,
    refresh: bool = False,
    mark: Optional[Fingerprint] = None,
) -> TranscriptResolution:
    """Get this asset's transcript from the best source available.

    Never raises for "no transcript exists" -- that is an ordinary outcome
    recorded in the returned ``note``, so a folder can be analysed with some
    clips transcribed and some not.
    """
    consult_premiere = config.use_premiere if use_premiere is None else use_premiere
    mark = mark or _mark_for(asset)

    if not refresh:
        stored, stale = load(config, asset.asset_id, mark=mark)
        if stored is not None and len(stored):
            return TranscriptResolution(
                asset_id=asset.asset_id,
                transcript=stored,
                source=stored.source,
                origin="stored",
                stale=stale,
                note="Footage has changed since this transcript was made; "
                     "re-import or re-transcribe if the timing looks off."
                if stale else "",
            )

    notes: list[str] = []

    if consult_premiere:
        result = pull(asset, bridge=bridge)
        if result.found and result.transcript is not None:
            transcript = result.transcript
            transcript.entries = normalize.normalize_entries(
                transcript.entries, max_duration=asset.duration or None
            )
            save(config, transcript, mark=mark, cache=cache)
            return TranscriptResolution(
                asset_id=asset.asset_id,
                transcript=transcript,
                source="premiere",
                origin=f"premiere:{result.method}",
                note=result.note,
            )
        notes.append(result.note or "Premiere had no transcript for this clip.")

    if use_sidecar:
        sidecar = find_sidecar(asset.path, extra_dirs=[config.transcripts_dir])
        if sidecar is not None:
            try:
                transcript = import_file(config, asset, sidecar, cache=cache, mark=mark)
                return TranscriptResolution(
                    asset_id=asset.asset_id,
                    transcript=transcript,
                    source=transcript.source,
                    origin=f"sidecar:{sidecar.name}",
                    note=f"Found {sidecar.name} next to the footage.",
                )
            except TranscriptError as exc:
                notes.append(f"Sidecar {sidecar.name} could not be used: {exc}")
        else:
            notes.append("No transcript file found next to the footage.")

    return TranscriptResolution(
        asset_id=asset.asset_id,
        source="none",
        note=" ".join(notes) + " " + MANUAL_EXPORT_HINT,
    )
