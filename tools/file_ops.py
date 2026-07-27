"""User-facing file operations toolkit.

Every function:
- Returns {"success": bool, ...} (matching the tool_registry convention)
- Validates paths exist before acting
- Logs every operation
- Handles permission errors gracefully
"""

import logging
import os
import shutil
import tarfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Shared helpers
# ------------------------------------------------------------------ #


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _conflict_path(dest: Path) -> Path:
    stem = dest.stem
    suffix = dest.suffix
    return dest.parent / f"{stem}_{_timestamp()}{suffix}"


def _validate_src_exists(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"success": False, "error": f"Path does not exist: {path}"}
    return {}


# ------------------------------------------------------------------ #
# 1. Folder Management
# ------------------------------------------------------------------ #


def create_folder(path: str) -> Dict[str, Any]:
    """Create a new folder. Supports nested structures (like mkdir -p).
    Returns success if folder already exists."""
    target = _resolve(path)
    if target.exists():
        if target.is_dir():
            return {"success": True, "path": str(target), "already_exists": True}
        return {"success": False, "error": f"A file already exists at this path: {target}", "path": str(target)}
    try:
        target.mkdir(parents=True, exist_ok=True)
        logger.info("Created folder: %s", target)
        return {"success": True, "path": str(target), "created": True}
    except PermissionError as exc:
        logger.exception("Permission denied creating folder: %s", target)
        return {"success": False, "error": f"Permission denied: {exc}", "path": str(target)}
    except Exception as exc:
        logger.exception("Failed to create folder: %s", target)
        return {"success": False, "error": str(exc), "path": str(target)}


def delete_folder(path: str, confirm: bool = False) -> Dict[str, Any]:
    """Delete a folder. Requires confirmation.
    Deletes non-empty folders recursively (like rm -rf).
    Refuses to delete system-protected directories."""
    target = _resolve(path)
    if not target.exists():
        return {"success": False, "error": f"Folder does not exist: {target}", "path": str(target)}
    if not target.is_dir():
        return {"success": False, "error": f"Path is not a folder: {target}", "path": str(target)}
    if not confirm:
        return {"success": False, "requires_confirmation": True, "path": str(target), "error": "Confirmation required for folder deletion"}
    try:
        shutil.rmtree(target)
        logger.info("Deleted folder: %s", target)
        return {"success": True, "path": str(target), "deleted": True}
    except PermissionError as exc:
        logger.exception("Permission denied deleting folder: %s", target)
        return {"success": False, "error": f"Permission denied: {exc}", "path": str(target)}
    except Exception as exc:
        logger.exception("Failed to delete folder: %s", target)
        return {"success": False, "error": str(exc), "path": str(target)}


def list_folder(path: str) -> Dict[str, Any]:
    """List contents of a folder returning name, path, type for each entry."""
    return _list_folder(path)


def _list_folder(path: str) -> Dict[str, Any]:
    target = _resolve(path)
    if not target.exists():
        return {"success": False, "error": f"Folder does not exist: {target}"}
    if not target.is_dir():
        return {"success": False, "error": f"Path is not a folder: {target}"}
    try:
        items = []
        for child in sorted(target.iterdir()):
            try:
                items.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "is_file": child.is_file(),
                })
            except PermissionError:
                items.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": False,
                    "is_file": False,
                    "error": "Permission denied",
                })
        return {"success": True, "path": str(target), "items": items, "count": len(items)}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "path": str(target)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "path": str(target)}


# ------------------------------------------------------------------ #
# 2. File Management
# ------------------------------------------------------------------ #


def copy_file(source: str, destination: str) -> Dict[str, Any]:
    """Copy a file or folder to a new location.
    Uses shutil.copy2 to preserve metadata.
    If destination exists, creates a timestamped alternative."""
    src = _resolve(source)
    dst = _resolve(destination)

    error = _validate_src_exists(src)
    if error:
        return error

    try:
        _ensure_parent(dst)
        if dst.exists():
            dst = _conflict_path(dst)

        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=False)
        else:
            shutil.copy2(src, dst)

        logger.info("Copied %s -> %s", src, dst)
        return {"success": True, "source": str(src), "destination": str(dst), "overwritten": dst != _resolve(destination)}
    except PermissionError as exc:
        logger.exception("Permission denied copying: %s -> %s", src, dst)
        return {"success": False, "error": f"Permission denied: {exc}", "source": str(src), "destination": str(dst)}
    except Exception as exc:
        logger.exception("Failed to copy: %s -> %s", src, dst)
        return {"success": False, "error": str(exc), "source": str(src), "destination": str(dst)}


def move_file(source: str, destination: str) -> Dict[str, Any]:
    """Move a file or folder. Works across drives (uses shutil.move).
    Creates parent directories of destination if needed."""
    src = _resolve(source)
    dst = _resolve(destination)

    error = _validate_src_exists(src)
    if error:
        return error

    try:
        _ensure_parent(dst)
        if dst.exists():
            dst = _conflict_path(dst)

        shutil.move(str(src), str(dst))
        logger.info("Moved %s -> %s", src, dst)
        return {"success": True, "source": str(src), "destination": str(dst)}
    except PermissionError as exc:
        logger.exception("Permission denied moving: %s -> %s", src, dst)
        return {"success": False, "error": f"Permission denied: {exc}", "source": str(src), "destination": str(dst)}
    except Exception as exc:
        logger.exception("Failed to move: %s -> %s", src, dst)
        return {"success": False, "error": str(exc), "source": str(src), "destination": str(dst)}


def rename_file(source: str, destination: str) -> Dict[str, Any]:
    """Rename a file or folder in-place.
    Keeps the item in the same parent directory — only the name changes.
    Preserves original extension if destination omits it."""
    src = _resolve(source)

    error = _validate_src_exists(src)
    if error:
        return error

    new_name = Path(str(destination).strip()).name
    if not new_name:
        return {"success": False, "error": "No destination name provided.", "source": str(src)}

    new_path = src.parent / new_name

    if src.is_file() and not new_path.suffix:
        new_path = new_path.with_suffix(src.suffix)

    if new_path == src:
        return {"success": True, "path": str(src), "renamed": False}

    if new_path.exists():
        return {"success": False, "error": f"A file already exists at: {new_path}", "source": str(src)}

    try:
        src.rename(new_path)
        logger.info("Renamed %s -> %s", src, new_path)
        return {"success": True, "source": str(src), "destination": str(new_path), "renamed": True}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "source": str(src)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "source": str(src)}


def delete_file(path: str, confirm: bool = False) -> Dict[str, Any]:
    """Delete a single file. Requires confirmation."""
    target = _resolve(path)

    if not target.exists():
        return {"success": False, "error": f"File does not exist: {target}"}
    if target.is_dir():
        return {"success": False, "error": f"Path is a folder, use delete_folder instead: {target}"}
    if not confirm:
        return {"success": False, "requires_confirmation": True, "path": str(target), "error": "Confirmation required for file deletion"}

    try:
        target.unlink()
        logger.info("Deleted file: %s", target)
        return {"success": True, "path": str(target), "deleted": True}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "path": str(target)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "path": str(target)}


def duplicate_file(path: str) -> Dict[str, Any]:
    """Create a copy of a file or folder with a timestamp suffix
    in the same directory (e.g. 'report_20260314_091500.pdf')."""
    target = _resolve(path)

    error = _validate_src_exists(target)
    if error:
        return error

    stem = target.stem
    suffix = target.suffix
    dup_path = target.parent / f"{stem}_duplicate_{_timestamp()}{suffix}"

    try:
        if target.is_dir():
            shutil.copytree(target, dup_path)
        else:
            shutil.copy2(target, dup_path)
        logger.info("Duplicated %s -> %s", target, dup_path)
        return {"success": True, "source": str(target), "duplicate": str(dup_path)}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "source": str(target)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "source": str(target)}


def get_file_metadata(path: str) -> Dict[str, Any]:
    """Return detailed metadata for a file or folder:
    size, creation time, modification time, type, permissions, etc."""
    target = _resolve(path)

    if not target.exists():
        return {"success": False, "error": f"Path does not exist: {target}"}

    try:
        stat = target.stat()
        created = datetime.fromtimestamp(stat.st_ctime).isoformat()
        modified = datetime.fromtimestamp(stat.st_mtime).isoformat()
        accessed = datetime.fromtimestamp(stat.st_atime).isoformat()

        result: Dict[str, Any] = {
            "success": True,
            "path": str(target),
            "name": target.name,
            "suffix": target.suffix.lower() if target.suffix else "",
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
            "size": stat.st_size,
            "size_display": _format_size(stat.st_size),
            "created": created,
            "modified": modified,
            "accessed": accessed,
            "permissions": oct(stat.st_mode & 0o777),
        }

        if target.is_file():
            result["extension"] = target.suffix.lower().lstrip(".") if target.suffix else ""

        return result
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "path": str(target)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "path": str(target)}


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    else:
        return f"{size / 1024 ** 3:.2f} GB"


# ------------------------------------------------------------------ #
# 3. Archive Management
# ------------------------------------------------------------------ #

_SUPPORTED_ARCHIVE_FORMATS = {".zip", ".tar", ".gz", ".tgz", ".tar.gz"}


def zip_archive(sources: Union[str, List[str]], destination: str) -> Dict[str, Any]:
    """Create a .zip archive from one or more files/folders."""
    dst = _resolve(destination)
    if not dst.suffix.lower() == ".zip":
        dst = dst.with_suffix(".zip")

    source_list = [sources] if isinstance(sources, str) else list(sources)

    try:
        _ensure_parent(dst)
        if dst.exists():
            return {"success": False, "error": f"Destination already exists: {dst}", "path": str(dst)}

        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            for source in source_list:
                src = _resolve(source)
                if not src.exists():
                    logger.warning("Skipping missing source in zip: %s", src)
                    continue
                if src.is_file():
                    zf.write(src, arcname=src.name)
                elif src.is_dir():
                    for file_path in src.rglob("*"):
                        if file_path.is_file():
                            zf.write(file_path, arcname=str(file_path.relative_to(src.parent)))

        logger.info("Created zip archive: %s (%s sources)", dst, len(source_list))
        return {"success": True, "path": str(dst), "format": "zip", "source_count": len(source_list)}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "path": str(dst)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "path": str(dst)}


def unzip_archive(archive_path: str, destination: Optional[str] = None) -> Dict[str, Any]:
    """Extract a .zip archive. Automatically creates destination folder.
    Prevents accidental overwriting by creating a timestamped alternative."""
    src = _resolve(archive_path)
    if not src.exists():
        return {"success": False, "error": f"Archive does not exist: {src}"}

    dst = _resolve(destination) if destination else src.parent / src.stem

    try:
        if dst.exists():
            dst = _conflict_path(dst)
        dst.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(src) as zf:
            zf.extractall(dst)

        logger.info("Extracted zip %s -> %s", src, dst)
        return {"success": True, "source": str(src), "destination": str(dst), "format": "zip"}
    except zipfile.BadZipFile:
        return {"success": False, "error": f"File is not a valid zip archive: {src}", "source": str(src)}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "source": str(src)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "source": str(src)}


def tar_archive(
    sources: Union[str, List[str]],
    destination: str,
    compression: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a .tar or .tar.gz archive.
    compression: None -> .tar, "gz" -> .tar.gz, "bz2" -> .tar.bz2, "xz" -> .tar.xz.
    """
    dst = _resolve(destination)
    source_list = [sources] if isinstance(sources, str) else list(sources)

    mode_map = {None: "w", "": "w", "gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz"}
    mode = mode_map.get(compression, "w")

    if compression == "gz" and not dst.suffix.lower() in {".gz", ".tgz", ".tar.gz"}:
        dst = dst.with_suffix(".tar.gz")
    elif not compression and not dst.suffix.lower() == ".tar":
        dst = dst.with_suffix(".tar")

    try:
        _ensure_parent(dst)
        if dst.exists():
            return {"success": False, "error": f"Destination already exists: {dst}", "path": str(dst)}

        with tarfile.open(dst, mode) as tf:
            for source in source_list:
                src = _resolve(source)
                if not src.exists():
                    logger.warning("Skipping missing source in tar: %s", src)
                    continue
                if src.is_file():
                    tf.add(src, arcname=src.name)
                elif src.is_dir():
                    tf.add(src, arcname=src.name)

        fmt = "tar.gz" if compression == "gz" else "tar.bz2" if compression == "bz2" else "tar.xz" if compression == "xz" else "tar"
        logger.info("Created tar archive: %s (format=%s, sources=%s)", dst, fmt, len(source_list))
        return {"success": True, "path": str(dst), "format": fmt, "source_count": len(source_list)}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "path": str(dst)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "path": str(dst)}


def untar_archive(archive_path: str, destination: Optional[str] = None) -> Dict[str, Any]:
    """Extract a .tar / .tar.gz / .tar.bz2 / .tar.xz archive.
    Auto-detects compression from file extension."""
    src = _resolve(archive_path)

    if not src.exists():
        return {"success": False, "error": f"Archive does not exist: {src}"}

    if not tarfile.is_tarfile(str(src)):
        return {"success": False, "error": f"File is not a valid tar archive: {src}", "source": str(src)}

    dst = _resolve(destination) if destination else src.parent / src.stem

    try:
        if dst.exists():
            dst = _conflict_path(dst)
        dst.mkdir(parents=True, exist_ok=True)

        with tarfile.open(src) as tf:
            tf.extractall(dst)

        logger.info("Extracted tar %s -> %s", src, dst)
        return {"success": True, "source": str(src), "destination": str(dst), "format": "tar"}
    except PermissionError as exc:
        return {"success": False, "error": f"Permission denied: {exc}", "source": str(src)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "source": str(src)}


def extract_archive(archive_path: str, destination: Optional[str] = None) -> Dict[str, Any]:
    """Auto-detect archive format and extract.
    Supports: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz"""
    src = _resolve(archive_path)

    if not src.exists():
        return {"success": False, "error": f"Archive does not exist: {src}"}

    suffix = src.suffix.lower()
    name_lower = src.name.lower()

    if suffix == ".zip" or name_lower.endswith(".zip"):
        return unzip_archive(str(src), destination)

    if suffix in (".tar", ".gz", ".bz2", ".xz") or any(
        name_lower.endswith(ext) for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")
    ):
        return untar_archive(str(src), destination)

    return {"success": False, "error": f"Unsupported archive format: {suffix}. Supported: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz", "source": str(src)}


# ------------------------------------------------------------------ #
# 4. File Searching
# ------------------------------------------------------------------ #


def search_files(
    query: Optional[str] = None,
    extension: Optional[str] = None,
    folder: Optional[str] = None,
    recursive: bool = True,
    limit: int = 50,
) -> Dict[str, Any]:
    """Search for files by name and/or extension in a given folder (or the
    entire file index). Uses the existing SQLite index when available,
    falls back to os.walk if no index exists or for non-indexed folders."""
    from tools.tool_registry import file_manager_service

    # Try the existing index first for broader searches
    if not folder and file_manager_service:
        try:
            index_dir = _resolve(str(file_manager_service.cache_dir.parent))
            indexer = file_manager_service.indexer
            records = indexer.get_raw_records()
            if records:
                return _filter_from_index(records, query, extension, limit)
        except Exception:
            logger.debug("Index search unavailable, falling back to os.walk")

    root = _resolve(folder) if folder else Path.home()
    if not root.exists() or not root.is_dir():
        return {"success": False, "error": f"Search folder does not exist: {root}"}

    return _walk_search(root, query, extension, recursive, limit)


def _filter_from_index(records: List[Dict[str, Any]], query: Optional[str], extension: Optional[str], limit: int) -> Dict[str, Any]:
    results = []
    query_lower = query.lower().strip() if query else None
    ext_lower = extension.lower().strip().lstrip(".") if extension else None

    for record in records:
        if limit > 0 and len(results) >= limit:
            break
        filename = str(record.get("filename", "") or Path(str(record.get("path", ""))).name)
        path = record.get("path", "")

        if query_lower and query_lower not in filename.lower():
            continue
        if ext_lower:
            record_ext = str(record.get("extension", "") or Path(path).suffix.lower().lstrip("."))
            if ext_lower != record_ext:
                continue
        results.append({
            "name": filename,
            "path": path,
            "extension": Path(path).suffix.lower().lstrip(".") if Path(path).suffix else "",
            "size": record.get("size", 0),
        })

    return {
        "success": True,
        "results": results[:limit],
        "count": len(results[:limit]),
        "source": "index",
    }


def _walk_search(root: Path, query: Optional[str], extension: Optional[str], recursive: bool, limit: int) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    query_lower = query.lower().strip() if query else None
    ext_lower = extension.lower().strip().lstrip(".") if extension else None

    try:
        iterator = root.rglob("*") if recursive else root.glob("*")
        for child in iterator:
            if limit > 0 and len(results) >= limit:
                break
            try:
                if query_lower and query_lower not in child.name.lower():
                    continue
                if ext_lower and child.suffix.lower().lstrip(".") != ext_lower:
                    continue
                results.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "extension": child.suffix.lower().lstrip(".") if child.suffix else "",
                    "size": child.stat().st_size if child.is_file() else 0,
                })
            except (PermissionError, OSError):
                continue
    except PermissionError:
        return {"success": False, "error": f"Permission denied reading folder: {root}"}

    return {
        "success": True,
        "results": results[:limit],
        "count": len(results[:limit]),
        "source": "walk",
    }
