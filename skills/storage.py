"""Skill persistence layer.

Every skill lives in its own folder under Downloads/Nova/Skills (never
inside the project folder):

    <skill name>/
        skill.json        <- identity + metadata (name, description, ...)
        timeline.json     <- raw events + semantic steps + screenshots manifest
        metadata.json     <- recording environment, variables, improvement history
        screenshots/      <- captured frames (compressed JPEG)
        vision/           <- per-frame UI analysis (OCR text + UI objects)
        playback_logs/    <- per-playback audit trail (expected/detected UI, ...)
        .nova/stats.json  <- hidden: usage + improvement statistics

All paths resolve relative to settings.skills_dir so the whole tree can be
redirected via SKILLS_DIR or mocked in tests.
"""

import json
import logging
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------- #

def skills_root() -> Path:
    root = Path(settings.skills_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def skill_dir(name: str) -> Path:
    return skills_root() / sanitize_name(name)


def _stats_dir() -> Path:
    path = skills_root() / ".nova"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip())
    return cleaned or "Unnamed Skill"


def ensure_layout(name: str) -> Path:
    folder = skill_dir(name)
    for sub in ("screenshots", "vision", "playback_logs"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    (folder / ".nova").mkdir(parents=True, exist_ok=True)
    return folder


# --------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------- #

def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        logger.warning("Could not read %s -- using default", path)
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------- #
# Per-skill files
# --------------------------------------------------------------------- #

def skill_file(name: str) -> Path:
    return skill_dir(name) / "skill.json"


def timeline_file(name: str) -> Path:
    return skill_dir(name) / "timeline.json"


def metadata_file(name: str) -> Path:
    return skill_dir(name) / "metadata.json"


def load_skill(name: str) -> dict | None:
    data = read_json(skill_file(name))
    if not isinstance(data, dict):
        return None
    try:
        from skills.schema import migrate
        return migrate(data, load_timeline(name), load_metadata(name))
    except ValueError:
        logger.warning("Skill '%s' has an invalid definition", name)
        return data


def save_skill(record: dict) -> None:
    name = record.get("name") or record.get("id") or "Unnamed Skill"
    ensure_layout(name)
    write_json(skill_file(name), record)


def load_timeline(name: str) -> dict:
    data = read_json(timeline_file(name), {})
    return data if isinstance(data, dict) else {}


def save_timeline(name: str, timeline: dict) -> None:
    ensure_layout(name)
    write_json(timeline_file(name), timeline)


def load_metadata(name: str) -> dict:
    data = read_json(metadata_file(name), {})
    return data if isinstance(data, dict) else {}


def save_metadata(name: str, metadata: dict) -> None:
    ensure_layout(name)
    write_json(metadata_file(name), metadata)


# --------------------------------------------------------------------- #
# Listing / editing
# --------------------------------------------------------------------- #

def list_skills() -> list[dict]:
    root = skills_root()
    skills = []
    if not root.exists():
        return skills
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        record = read_json(folder / "skill.json")
        if not isinstance(record, dict):
            continue
        record = dict(record)
        record.setdefault("folder", str(folder))
        record.setdefault("name", folder.name)
        skills.append(record)
    return skills


def skill_exists(name: str) -> bool:
    return skill_file(name).exists()


def new_skill_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


def rename_skill(old: str, new: str) -> dict | None:
    """Rename a skill folder. Returns the updated skill record or None."""
    if not skill_exists(old):
        return None
    record = load_skill(old)
    if record is None:
        return None
    ensure_layout(new)
    for sub in ("screenshots", "vision", "playback_logs", ".nova"):
        src = skill_dir(old) / sub
        dst = skill_dir(new) / sub
        if src.exists() and src.is_dir():
            for item in src.iterdir():
                if item.is_dir():
                    shutil.copytree(item, dst / item.name, dirs_exist_ok=True)
                else:
                    dst.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dst / item.name)
    for filename in ("skill.json", "timeline.json", "metadata.json"):
        src = skill_dir(old) / filename
        dst = skill_dir(new) / filename
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    record["name"] = new
    record["renamed_from"] = old
    save_skill(record)
    shutil.rmtree(skill_dir(old), ignore_errors=True)
    logger.info("Renamed skill '%s' -> '%s'", old, new)
    return record


def delete_skill(name: str) -> bool:
    folder = skill_dir(name)
    if not folder.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    logger.info("Deleted skill '%s'", name)
    return True


def duplicate_skill(name: str, new_name: str) -> dict | None:
    if not skill_exists(name):
        return None
    record = load_skill(name)
    if record is None:
        return None
    ensure_layout(new_name)
    src = skill_dir(name)
    dst = skill_dir(new_name)
    for sub in ("screenshots", "vision", "playback_logs", ".nova"):
        src_sub = src / sub
        if src_sub.exists():
            shutil.copytree(src_sub, dst / sub, dirs_exist_ok=True)
    for filename in ("skill.json", "timeline.json", "metadata.json"):
        if (src / filename).exists():
            shutil.copy2(src / filename, dst / filename)
    record = dict(record)
    record["name"] = new_name
    record["id"] = new_skill_id()
    record["created"] = now_iso()
    record["last_used"] = None
    record["duplicated_from"] = name
    save_skill(record)
    logger.info("Duplicated skill '%s' -> '%s'", name, new_name)
    return record


def export_skill(name: str, destination: Path | str | None = None) -> Path | None:
    """Zip a skill folder. Returns the archive path, or None if the skill
    does not exist or the archive could not be created."""
    folder = skill_dir(name)
    if not folder.exists():
        return None
    dest = Path(destination).expanduser() if destination else folder.with_suffix(".zip")
    if dest.is_dir():
        dest = dest / f"{sanitize_name(name)}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(folder.rglob("*")):
            if file_path.is_file():
                relative = file_path.relative_to(folder)
                archive.write(file_path, f"{sanitize_name(name)}/{relative}")
    logger.info("Exported skill '%s' -> %s", name, dest)
    return dest


def import_skill(archive_path: Path | str) -> str | None:
    """Extract a skill zip into the skills root. Returns the imported skill
    name, or None on failure."""
    archive = Path(archive_path).expanduser()
    if not archive.exists():
        return None
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                return None
            top_level = Path(names[0]).parts[0]
            name = sanitize_name(top_level)
            folder = skill_dir(name)
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
            ensure_layout(name)
            root_resolved = folder.resolve()
            for entry in names:
                parts = Path(entry).parts
                if len(parts) < 2:
                    continue  # stray top-level file -- not part of the skill
                target = folder.joinpath(*parts[1:]).resolve()
                if target != root_resolved and root_resolved not in target.parents:
                    raise ValueError(f"Unsafe path in skill archive: {entry}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        record = load_skill(name)
        if record is None:
            raise ValueError("Imported archive does not contain a valid skill.json")
        from skills.schema import migrate
        save_skill(migrate(record, load_timeline(name), load_metadata(name)))
        logger.info("Imported skill from %s", archive)
        return name
    except Exception:
        logger.exception("Failed to import skill from %s", archive)
        return None


# --------------------------------------------------------------------- #
# Usage / improvement bookkeeping
# --------------------------------------------------------------------- #

def update_last_used(name: str, outcome: str = "success") -> None:
    record = load_skill(name)
    if record is None:
        return
    record["last_used"] = now_iso()
    record.setdefault("usage", {"count": 0, "outcomes": []})
    record["usage"]["count"] = record["usage"].get("count", 0) + 1
    record["usage"]["outcomes"].append(outcome)
    record["usage"]["outcomes"] = record["usage"]["outcomes"][-20:]
    save_skill(record)


def write_playback_log(name: str, entry: dict) -> None:
    """Append one playback audit log. Each entry records exactly what the
    playback engine saw, decided and did per step."""
    folder = ensure_layout(name)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    counter = 0
    while (folder / "playback_logs" / f"{stamp}_{counter}.json").exists():
        counter += 1
    write_json(folder / "playback_logs" / f"{stamp}_{counter}.json", entry)


def read_stats(name: str) -> dict:
    return read_json(_stats_dir() / f"{sanitize_name(name)}.json", {})


def write_stats(name: str, stats: dict) -> None:
    write_json(_stats_dir() / f"{sanitize_name(name)}.json", stats)


def request_stats() -> dict:
    """Global request-frequency stats used by the suggestion engine
    (Phase 5). This is engine bookkeeping, not user data -- kept under
    the skills root's hidden .nova folder."""
    return read_json(_stats_dir() / "requests.json", {"requests": {}})


def save_request_stats(stats: dict) -> None:
    write_json(_stats_dir() / "requests.json", stats)
