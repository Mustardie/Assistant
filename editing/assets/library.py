"""Where the asset library lives, and how it is created.

One folder per category under one root, because the folder is the primary
signal: a file in ``sfx/impacts/`` is an impact sound whatever it is called,
and asking users to write a sidecar for every file before anything works would
mean nothing ever works.

``init`` is deliberately generous. It creates the folders, writes a README that
explains what goes in each one, and drops a commented example sidecar next to
it — so the answer to "what do I put here and how do I describe it?" is in the
folder rather than in this docstring. It never overwrites anything a user has
written.

The root defaults to ``<model_dir>/assets``, which puts sounds and graphics
beside the model weights rather than in the run outputs. Run outputs are
disposable; an asset library is not.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from editing.config import EditingConfig

#: Category folders created by ``assets init``, and what each one is for. The
#: order is the order they are reported in.
FOLDERS = {
    "music": (
        "Full tracks and beds. Anything long enough to sit under a section."
    ),
    "sfx": (
        "Short one-shots: impacts, pops, whooshes, stings. Under about three "
        "seconds."
    ),
    "ambience": (
        "Loopable room tone and atmosphere: wind, cave drips, rain, crowd."
    ),
    "callout": (
        "Arrows, circles, labels and other point-at-this graphics. PNG with "
        "transparency."
    ),
    "titles": (
        "Title and chapter card backgrounds, lower-third plates, end cards."
    ),
    "transitions": (
        "Whoosh-and-wipe overlays and their sounds, when they belong together."
    ),
}

#: Folder name -> the schema category it implies. ``titles`` and
#: ``transitions`` are plural on disk because that reads better as a folder,
#: and singular in the schema because that reads better as a category.
FOLDER_CATEGORY = {
    "music": "music",
    "sfx": "sfx",
    "ambience": "ambience",
    "callout": "callout",
    "callouts": "callout",
    "titles": "title",
    "title": "title",
    "transitions": "transition",
    "transition": "transition",
}

#: Directory names never descended into. Pointing the indexer at a project root
#: by accident should cost a second, not an hour.
SKIP_DIRECTORIES = frozenset({
    ".git", ".svn", ".hg", "__pycache__", "node_modules", "venv", ".venv",
    "env", "build", "dist", "installer", "site-packages", ".idea", ".vscode",
    "Adobe Premiere Pro Auto-Save", "Adobe Premiere Pro Audio Previews",
    "Adobe Premiere Pro Video Previews", "Adobe Premiere Pro Preview Files",
    "Media Cache", "Media Cache Files", ".cache", "temp", "tmp",
})

#: How deep the scan goes below a category folder. Deep enough for
#: ``sfx/impacts/heavy/``, shallow enough that a stray symlink into a drive
#: root cannot run away.
MAX_DEPTH = 4

README_NAME = "README.md"
EXAMPLE_SIDECAR = "example.asset.json"
INDEX_NAME = "library.json"


def default_root(config: EditingConfig) -> Path:
    """Where the library lives unless told otherwise.

    Beside the model weights rather than in ``data/editing``: run outputs are
    disposable and get cleared, and a sound library is neither.
    """
    return Path(config.model_dir) / "assets"


def resolve_root(config: EditingConfig, root: Optional[str] = None) -> Path:
    return Path(root).expanduser() if root else default_root(config)


def index_path(config: EditingConfig, root: Optional[str] = None) -> Path:
    """Where the index JSON is written.

    In the run output, not in the library: the index is derived data, and a
    user who deletes ``data/editing`` should lose the index and keep the
    sounds.
    """
    return config.asset_library_dir / INDEX_NAME


def initialise(root: Path, *, write_docs: bool = True) -> dict:
    """Create the folder structure. Never overwrites anything.

    Returns what it did, per path, so ``assets init`` can report "created 6,
    left 2 alone" rather than claiming to have made folders that were already
    there.
    """
    root = Path(root)
    result: dict = {"root": str(root), "created": [], "existing": [],
                    "docs": []}

    for name in FOLDERS:
        folder = root / name
        if folder.exists():
            result["existing"].append(str(folder))
        else:
            folder.mkdir(parents=True, exist_ok=True)
            result["created"].append(str(folder))

    if not write_docs:
        return result

    readme = root / README_NAME
    if not readme.exists():
        readme.write_text(_readme_text(), encoding="utf-8")
        result["docs"].append(str(readme))

    example = root / EXAMPLE_SIDECAR
    if not example.exists():
        example.write_text(
            json.dumps(_example_sidecar(), indent=2) + "\n", encoding="utf-8"
        )
        result["docs"].append(str(example))
    return result


def _example_sidecar() -> dict:
    """A filled-in sidecar, with every field this system reads.

    Written as a real, valid document rather than a schema listing: a user
    copies it next to a file, renames it and edits values, which is a much
    shorter path than reading a field table.
    """
    return {
        "_comment": (
            "Copy this next to a file and rename it to match: "
            "impact_boom.wav -> impact_boom.asset.json. Every field is "
            "optional; anything absent is inferred from the folder and the "
            "filename. Delete the keys you do not need."
        ),
        "category": "sfx",
        "tags": ["impact", "boom", "heavy"],
        "intensity": "high",
        "mood": ["dramatic", "dark"],
        "bpm": None,
        "loopable": False,
        "safe_for_auto": True,
        "preferred_styles": ["cinematic_minecraft"],
        "avoid_styles": ["minimal_clean"],
        "license_notes": "Bought from <somewhere>, licence covers YouTube use.",
        "start_offset": 0.0,
        "end_offset": None,
        "volume_adjust_db": -3.0,
        "notes": "Long tail -- leave room after it.",
    }


def _readme_text() -> str:
    lines = [
        "# Asset library",
        "",
        "Local files only. Nothing here is downloaded, and nothing in this",
        "folder is ever modified -- trimming, gain and fades are applied to the",
        "*placed clip* in Premiere, never to your source file.",
        "",
        "## Folders",
        "",
    ]
    for name, description in FOLDERS.items():
        lines.append(f"- **`{name}/`** — {description}")
    lines.extend([
        "",
        "Subfolders are fine and are read as tags: a file in",
        "`sfx/impacts/heavy/` picks up `impacts` and `heavy` automatically.",
        "",
        "## Supported files",
        "",
        "| Kind | Extensions |",
        "|---|---|",
        "| audio | `.wav` `.mp3` `.m4a` `.aac` `.flac` `.ogg` |",
        "| image | `.png` `.jpg` `.jpeg` `.webp` |",
        "| video | `.mp4` `.mov` `.webm` |",
        "| Motion Graphics | `.mogrt` (indexed, but placed as a marker only) |",
        "",
        "## Sidecar metadata",
        "",
        "Optional. Put `<filename>.asset.json` next to a file to describe it:",
        "",
        "```",
        "impact_boom.wav",
        "impact_boom.asset.json",
        "```",
        "",
        "See `example.asset.json` in this folder for every field. All of them",
        "are optional. A sidecar that will not parse does not break indexing —",
        "the asset is marked `needs_review` and left out of automatic",
        "placement until you fix it.",
        "",
        "## Naming",
        "",
        "Filenames are read for tags, so descriptive names do most of the work",
        "on their own:",
        "",
        "- `whoosh_fast_01.wav` → tags `whoosh`, `fast`",
        "- `tension_bed_loop.wav` → tags `tension`, `bed`, `loop` (and marked",
        "  loopable, because the name says so)",
        "- `arrow_red.png` → tags `arrow`, `red`",
        "",
        "## Safety",
        "",
        "`safe_for_auto: false` in a sidecar takes a file out of automatic",
        "placement entirely — it stays indexed and searchable, and the system",
        "will leave a marker naming it instead of using it. That is the switch",
        "to reach for when a sound is right but you want to place it yourself.",
        "",
    ])
    return "\n".join(lines)


def category_for(path: Path, root: Path) -> tuple:
    """The category a file's location implies, and the folder tags with it.

    Returns ``(category, [folder tags])``. The first path component under the
    root decides the category; everything deeper becomes a tag, which is what
    makes ``sfx/impacts/heavy/boom.wav`` self-describing without a sidecar.
    """
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return "other", []

    parts = [part for part in relative.parts[:-1] if part]
    if not parts:
        return "other", []

    category = FOLDER_CATEGORY.get(parts[0].strip().lower(), "other")
    tags = [part.strip().lower() for part in parts[1:]]
    return category, tags


def should_skip_directory(name: str) -> bool:
    return name in SKIP_DIRECTORIES or name.startswith(".")
