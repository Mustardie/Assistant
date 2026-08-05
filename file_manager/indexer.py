import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config.paths import get_nova_index_dir

logger = logging.getLogger(__name__)


class FileIndexManager:
    """
    Owns the SQLite file/folder index and the background scanner. All
    query-understanding and ranking logic has moved out to
    intent.py / entities.py / ranking.py / disambiguation.py /
    installations.py, orchestrated by search_engine.SearchEngine. This
    class's job is strictly: crawl the filesystem, store what it finds,
    and hand raw records to whoever asks (get_raw_records).
    """

    def __init__(self, db_path: Optional[Path] = None, config_path: Optional[Path] = None):
        index_dir = get_nova_index_dir()

        self.db_path = Path(db_path or (index_dir / ".assistant_file_index.sqlite3")).resolve()
        self.config_path = Path(config_path or (index_dir / ".assistant_file_index.json")).resolve()
        self.metadata_cache_path = self.config_path.with_suffix(".metadata.json")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._paused_event = threading.Event()
        self._paused_event.clear()
        self._thread: Optional[threading.Thread] = None
        self._search_engine = None  # lazy, avoids a hard import cycle
        self._status: Dict[str, Any] = {
            "indexed_files": 0,
            "indexed_folders": 0,
            "last_scan": None,
            "current_scan_progress": "Idle",
            "current_drive": None,
            "current_path": None,
            "files_per_second": 0.0,
            "elapsed_seconds": 0.0,
            "eta_seconds": None,
            "index_size": 0,
            "pending_updates": 0,
            "running": False,
            "paused": False,
        }
        self._folder_columns: List[str] = []
        self._folder_columns_set: set[str] = set()
        self._exclude_patterns: List[str] = []
        self._current_indexed_files = 0
        self._current_indexed_folders = 0
        self._progress_start_time = 0.0
        self._last_status_update = 0.0
        self._files_since_log = 0
        self._metadata_dirty = False
        self._load_config()
        self._init_db()
        self._cache_folder_schema()

    def _load_config(self) -> None:
        defaults = {
            "roots": [str(Path.home() / "Desktop"), str(Path.home() / "Documents"), str(Path.home() / "Downloads"), str(Path.home() / "Pictures"), str(Path.home() / "Videos"), str(Path.home() / "Music")],
            "exclude": [
                "C:/Windows",
                "C:/Program Files",
                "C:/Program Files (x86)",
                "C:/ProgramData",
                "$Recycle.Bin",
                "System Volume Information",
                "AppData/Local/Temp",
                "C:/Users/*/AppData",
                "C:/Users/*/Documents/My Music",
                "C:/Users/*/Documents/My Pictures",
                "C:/Users/*/Documents/My Videos",
                "node_modules",
                ".venv",
                "venv",
                ".git",
                "__pycache__",
                "site-packages",
                "dist-packages",
                ".tox",
                "Library/Python",
                "Python311",
                "Python312",
                "Python313",
            ],
            "full_drive_indexing": True,
            "ask_about_additional_drives": True,
        }
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    defaults.update(loaded)
            except Exception:
                logger.exception("Failed to load file index config")
        self.config = defaults
        self._prepare_exclude_patterns()

    def _save_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config, indent=2, sort_keys=True), encoding="utf-8")

    def _normalize_path(self, path: str) -> str:
        try:
            return os.path.normcase(os.path.normpath(str(path)))
        except Exception:
            return str(path)

    def _normalize_exclude_pattern(self, pattern: str) -> str:
        try:
            normalized = str(pattern).replace("/", os.sep)
            return os.path.normcase(os.path.normpath(normalized))
        except Exception:
            return str(pattern).lower()

    def _prepare_exclude_patterns(self) -> None:
        # Turn each exclude entry into a case-insensitive regex. A literal
        # '*' becomes a wildcard that spans a single path component, so
        # entries like "C:/Users/*/AppData" actually match real usernames
        # instead of being compared as literal '*' text.
        self._exclude_patterns = []
        for pattern in self.config.get("exclude", []):
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            escaped = re.escape(self._normalize_exclude_pattern(pattern))
            escaped = escaped.replace(r"\*", r"[^\\/]*")
            try:
                self._exclude_patterns.append(re.compile(escaped, re.IGNORECASE))
            except re.error:
                self._exclude_patterns.append(re.compile(re.escape(pattern), re.IGNORECASE))

    def _cache_folder_schema(self) -> None:
        try:
            self._folder_columns = self._get_table_columns("folders")
            self._folder_columns_set = set(self._folder_columns)
        except Exception:
            self._folder_columns = []
            self._folder_columns_set = set()

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    extension TEXT,
                    size INTEGER,
                    created REAL,
                    modified REAL,
                    hash TEXT,
                    extracted_text TEXT,
                    ocr_text TEXT,
                    caption TEXT,
                    keywords TEXT,
                    embedding TEXT,
                    indexed_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS folders (
                    path TEXT PRIMARY KEY,
                    name TEXT,
                    parent_path TEXT,
                    drive TEXT,
                    aliases TEXT,
                    modified REAL,
                    scanned_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_filename ON files(filename)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_modified ON files(modified)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_name ON folders(name)")
            conn.commit()

    # ------------------------------------------------------------------ #
    # Background indexing
    # ------------------------------------------------------------------ #

    def start_background_indexing(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._background_loop, daemon=True)
        self._thread.start()

    def pause_indexing(self) -> Dict[str, Any]:
        self._paused_event.set()
        self._status["paused"] = True
        return {"success": True, "paused": True}

    def resume_indexing(self) -> Dict[str, Any]:
        self._paused_event.clear()
        self._status["paused"] = False
        return {"success": True, "paused": False}

    def stop_indexing(self) -> Dict[str, Any]:
        self._stop_event.set()
        return {"success": True}

    def _background_loop(self) -> None:
        self._status["running"] = True
        try:
            self.index_roots(self._get_index_roots())
        except Exception as exc:
            logger.exception("Background indexing failed: %s", exc)
        finally:
            self._status["running"] = False
            self._status["current_scan_progress"] = "Idle"

    def _get_index_roots(self) -> List[Path]:
        roots: List[Path] = []
        for root in self.config.get("roots", []):
            path = Path(root).expanduser().resolve()
            if path.exists():
                roots.append(path)
        if self.config.get("full_drive_indexing", False):
            for drive in self._discover_drives():
                if str(drive) not in {str(path) for path in roots}:
                    roots.append(drive)
        return roots

    def _discover_drives(self) -> List[Path]:
        drives: List[Path] = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            path = Path(f"{letter}:/")
            if path.exists():
                drives.append(path)
        return drives

    def index_roots(self, roots: Sequence[Path]) -> Dict[str, Any]:
        self._status["current_scan_progress"] = "Starting index build"
        self._progress_start_time = time.time()
        self._last_status_update = 0.0
        self._files_since_log = 0
        self._metadata_dirty = False
        self._current_indexed_files = self._count_files()
        self._current_indexed_folders = self._count_folders()
        self._pending_file_rows = []
        self._pending_folder_rows = []
        self._inserted_since_commit = 0

        indexed = 0
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            cursor = conn.cursor()
            for root in roots:
                if self._stop_event.is_set():
                    break
                while self._paused_event.is_set():
                    self._paused_event.wait(1)
                root_path = self._normalize_path(str(root))
                self._status["current_drive"] = str(root)
                self._status["current_scan_progress"] = f"Scanning {root}"
                indexed += self._index_directory(root_path, cursor, conn, str(root))
                self._flush_inserts(cursor, conn, force=True)
            self._flush_inserts(cursor, conn, force=True)
            if self._metadata_dirty:
                self._write_metadata_cache()
                self._metadata_dirty = False
        finally:
            conn.close()

        self._status["last_scan"] = datetime.now(timezone.utc).isoformat()
        self._status["indexed_files"] = self._count_files()
        self._status["indexed_folders"] = self._count_folders()
        self._status["index_size"] = self._db_size()
        self._status["pending_updates"] = 0
        self._status["current_scan_progress"] = "Idle"
        self._status["current_path"] = None
        self._status["current_drive"] = None
        return {"success": True, "indexed": indexed}

    def _update_status(self, current_path: Optional[str] = None, current_drive: Optional[str] = None, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_status_update < 1.0:
            return
        self._last_status_update = now
        with self._lock:
            if current_path is not None:
                self._status["current_path"] = current_path
            if current_drive is not None:
                self._status["current_drive"] = current_drive
            elapsed = now - self._progress_start_time if self._progress_start_time else 0.0
            self._status["elapsed_seconds"] = elapsed
            self._status["files_per_second"] = self._current_indexed_files / elapsed if elapsed > 0 else 0.0
            self._status["indexed_files"] = self._current_indexed_files
            self._status["indexed_folders"] = self._current_indexed_folders
            self._status["index_size"] = self._db_size()
            self._status["pending_updates"] = len(getattr(self, "_pending_file_rows", [])) + len(getattr(self, "_pending_folder_rows", []))
            self._status["eta_seconds"] = None

    def _flush_inserts(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, force: bool = False) -> None:
        if self._pending_folder_rows:
            cursor.executemany(
                "INSERT OR REPLACE INTO folders(path, name, parent_path, drive, aliases, modified, scanned_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                self._pending_folder_rows,
            )
            self._inserted_since_commit += len(self._pending_folder_rows)
            self._pending_folder_rows = []
        if self._pending_file_rows:
            cursor.executemany(
                "INSERT OR REPLACE INTO files(path, filename, extension, size, created, modified, hash, extracted_text, ocr_text, caption, keywords, embedding, indexed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._pending_file_rows,
            )
            self._inserted_since_commit += len(self._pending_file_rows)
            self._pending_file_rows = []
        if force or self._inserted_since_commit >= 1000:
            conn.commit()
            self._inserted_since_commit = 0

    def _is_reparse_point(self, path: str) -> bool:
        try:
            if os.name != "nt":
                return False
            if os.path.islink(path):
                return True
            stat_info = os.stat(path, follow_symlinks=False)
            return getattr(stat_info, "st_reparse_tag", 0) != 0
        except Exception:
            return False

    def _index_directory(self, directory: str, cursor: sqlite3.Cursor, conn: sqlite3.Connection, root_drive: str) -> int:
        if self._stop_event.is_set() or not os.path.isdir(directory):
            return 0
        if self._is_reparse_point(directory):
            return 0
        count = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if self._stop_event.is_set():
                        break
                    while self._paused_event.is_set():
                        self._paused_event.wait(1)
                    entry_path = self._normalize_path(entry.path)
                    self._update_status(current_path=entry_path, current_drive=root_drive)
                    if self._should_exclude(entry_path):
                        continue
                    if entry.is_symlink():
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            self._record_folder(entry_path)
                            self._current_indexed_folders += 1
                            count += self._index_directory(entry_path, cursor, conn, root_drive)
                        elif entry.is_file(follow_symlinks=False) and self._is_supported(entry_path):
                            self._index_file(entry_path)
                            self._current_indexed_files += 1
                            count += 1
                            self._files_since_log += 1
                            if self._files_since_log >= 50000:
                                elapsed = max(time.time() - self._progress_start_time, 1e-6)
                                logger.info(
                                    "Indexed: %d files, Folders: %d, Speed: %.2f files/sec, Current directory: %s",
                                    self._current_indexed_files,
                                    self._current_indexed_folders,
                                    self._current_indexed_files / elapsed,
                                    directory,
                                )
                                self._files_since_log = 0
                            self._metadata_dirty = True
                            if len(self._pending_file_rows) + len(self._pending_folder_rows) >= 1000:
                                self._flush_inserts(cursor, conn, force=True)
                        self._update_status(current_path=entry_path, current_drive=root_drive)
                    except (PermissionError, OSError, sqlite3.OperationalError):
                        continue
        except (PermissionError, OSError, sqlite3.OperationalError):
            return count
        return count

    def _should_exclude(self, path: str) -> bool:
        normalized = self._normalize_path(path)
        for exclude_re in self._exclude_patterns:
            if exclude_re.search(normalized):
                return True
        return False

    def _get_table_columns(self, table_name: str) -> List[str]:
        if table_name == "folders" and self._folder_columns:
            return self._folder_columns
        with closing(sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)) as conn:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")]

    def _record_folder(self, path: str) -> None:
        try:
            stat_info = os.stat(path)
        except Exception:
            return
        self._pending_folder_rows.append(
            (
                path,
                os.path.basename(path),
                os.path.dirname(path),
                self._drive_for_path(path),
                json.dumps(self._folder_aliases(path)),
                stat_info.st_mtime,
                time.time(),
            )
        )

    def _index_file(self, path: str) -> None:
        try:
            stat_info = os.stat(path)
            text = self._read_text(path)
            keywords = self._extract_keywords(text)
            extension = os.path.splitext(path)[1].lower()
            self._pending_file_rows.append(
                (
                    path,
                    os.path.basename(path),
                    extension,
                    stat_info.st_size,
                    stat_info.st_ctime,
                    stat_info.st_mtime,
                    self._hash(path),
                    (text or "")[:4000],
                    "",
                    os.path.splitext(os.path.basename(path))[0],
                    json.dumps(keywords),
                    json.dumps([]),
                    time.time(),
                )
            )
        except FileNotFoundError:
            # File was deleted between the directory walk and this stat
            # (e.g. Claude's ephemeral AppData session cache). Skip quietly;
            # there is never anything worth recovering here.
            self._warn_file = getattr(self, "_warn_file", None)
            if self._warn_file != path:
                self._warn_file = path
                logger.debug("Skipping file that disappeared mid-scan: %s", path)
        except Exception as exc:
            logger.exception("Failed to index file %s: %s", path, exc)

    def _write_metadata_cache(self) -> None:
        try:
            with closing(sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)) as conn:
                rows = conn.execute(
                    "SELECT path, filename, extension, size, created, modified, indexed_at, caption, keywords FROM files"
                ).fetchall()
            payload = {
                "indexed_at": time.time(),
                "files": [
                    {
                        "filename": filename, "path": path, "extension": extension, "size": size,
                        "created_time": created, "modified_time": modified, "indexed_at": indexed_at,
                        "caption": caption, "keywords": json.loads(keywords_json) if keywords_json else [],
                    }
                    for path, filename, extension, size, created, modified, indexed_at, caption, keywords_json in rows
                ],
            }
            self.metadata_cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            logger.exception("Failed to write metadata cache: %s", exc)

    def _read_text(self, path: str) -> str:
        extension = os.path.splitext(path)[1].lower()
        if extension not in {".txt", ".md", ".csv"}:
            return ""
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:
                return ""
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                return handle.read()
        except Exception:
            return ""

    def _is_supported(self, path: str) -> bool:
        extension = os.path.splitext(path)[1].lower()
        return extension in {
            ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
            ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".flac",
            ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ppt", ".pptx",
            ".txt", ".md", ".json", ".csv", ".py", ".cpp", ".cc", ".c",
            ".java", ".js", ".ts", ".cs", ".html", ".css",
            ".zip", ".rar", ".tar", ".gz", ".exe", ".lnk",
        }

    def _extract_keywords(self, text: str) -> List[str]:
        words = [w.strip(".,;:()[]{}\"'\n") for w in text.lower().split() if len(w) > 0]
        filtered = []
        for word in words:
            if not word:
                continue
            if word in {"open", "my", "the", "a", "an", "please", "latest", "newest", "oldest", "biggest", "smallest", "find", "show", "locate", "for", "and", "or", "of", "to", "in", "on", "with", "from"}:
                continue
            if len(word) <= 2:
                continue
            filtered.append(word)
        return list(dict.fromkeys(filtered[:30]))

    def _drive_for_path(self, path: str) -> str:
        try:
            as_path = Path(path)
            return str(as_path.drive).upper() if getattr(as_path, "drive", "") else ""
        except Exception:
            return ""

    def _folder_aliases(self, path: str) -> List[str]:
        name = os.path.basename(path)
        parent_name = os.path.basename(os.path.dirname(path)) if os.path.dirname(path) != path else ""
        aliases = [name]
        if parent_name:
            aliases.append(parent_name)
        return list(dict.fromkeys([alias.lower() for alias in aliases if alias]))

    def _hash(self, path: str) -> str:
        try:
            return str(os.stat(path).st_mtime_ns)
        except Exception:
            return ""

    def _count_files(self) -> int:
        with closing(sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
            return int(row[0] if row else 0)

    def _count_folders(self) -> int:
        with closing(sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM folders").fetchone()
            return int(row[0] if row else 0)

    def _db_size(self) -> int:
        try:
            return int(self.db_path.stat().st_size) if self.db_path.exists() else 0
        except Exception:
            return 0

    def needs_initial_index(self) -> bool:
        """Return True if an initial index build is required.

        Conditions that require a full rebuild:
        - The database file does not exist.
        - Both the files and folders tables are empty.

        Returns False when the existing index contains data and can be reused.
        """
        try:
            # If the database file is missing, we need an initial index.
            if not self.db_path.exists():
                return True
            # If both tables are empty, we need an initial index.
            files_count = self._count_files()
            folders_count = self._count_folders()
            return (files_count == 0 and folders_count == 0)
        except Exception:
            # On any error determining state, err on the side of rebuilding.
            logger.exception("Failed to determine if initial index is needed; requesting rebuild")
            return True

    def status(self) -> Dict[str, Any]:
        with self._lock:
            if not self._status.get("running"):
                self._status["indexed_files"] = self._count_files()
                self._status["indexed_folders"] = self._count_folders()
                self._status["index_size"] = self._db_size()
            return dict(self._status)

    def rebuild_index(self) -> Dict[str, Any]:
        self._stop_event.clear()
        self._paused_event.clear()
        return self.index_roots(self._get_index_roots())

    def add_exclusion(self, path: str) -> Dict[str, Any]:
        self.config.setdefault("exclude", []).append(path)
        self._prepare_exclude_patterns()
        self._save_config()
        return {"success": True, "excluded": path}

    def remove_from_index(self, path: str) -> Dict[str, Any]:
        with closing(sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)) as conn:
            conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._write_metadata_cache()
        return {"success": True, "removed": path}

    def index_folder(self, path: str) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        return self.index_roots([target])

    # ------------------------------------------------------------------ #
    # Raw record access (consumed by SearchEngine) + search entry point
    # ------------------------------------------------------------------ #

    def get_raw_records(self, limit: int = 10000) -> List[Dict[str, Any]]:
        with closing(sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)) as conn:
            file_rows = conn.execute(
                f"SELECT path, filename, extension, size, created, modified, extracted_text, caption, keywords FROM files ORDER BY modified DESC LIMIT {limit}"
            ).fetchall()
            folder_columns = set(self._get_table_columns("folders"))
            if {"path", "name", "parent_path", "drive", "aliases", "modified", "scanned_at"}.issubset(folder_columns):
                folder_rows = conn.execute(
                    f"SELECT path, name, parent_path, drive, aliases, modified, scanned_at FROM folders ORDER BY modified DESC LIMIT {limit}"
                ).fetchall()
            else:
                folder_rows = conn.execute("SELECT path FROM folders LIMIT 1000").fetchall()

        records: List[Dict[str, Any]] = []
        for path, filename, extension, size, created, modified, extracted_text, caption, keywords_json in file_rows:
            records.append({
                "path": path, "filename": filename, "extension": extension, "size": size,
                "created_time": created, "modified_time": modified,
                "summary": (extracted_text or "")[:300], "caption": caption,
                "keywords": json.loads(keywords_json) if keywords_json else [],
                "is_dir": False, "name": filename, "parent_path": str(Path(path).parent),
            })

        if folder_columns and {"path", "name", "parent_path", "drive", "aliases", "modified", "scanned_at"}.issubset(folder_columns):
            for path, name, parent_path, drive, aliases, modified, scanned_at in folder_rows:
                records.append({
                    "path": path, "filename": name, "extension": "", "size": 0,
                    "created_time": scanned_at, "modified_time": modified, "summary": "",
                    "caption": name, "keywords": json.loads(aliases or "[]") if aliases else [],
                    "is_dir": True, "name": name, "parent_path": parent_path,
                })
        else:
            for row in folder_rows:
                path = row[0]
                derived_name = Path(path).name if path else ""
                parent_path = str(Path(path).parent) if path else ""
                records.append({
                    "path": path, "filename": derived_name, "extension": "", "size": 0,
                    "created_time": None, "modified_time": None, "summary": "",
                    "caption": derived_name, "keywords": [], "is_dir": True,
                    "name": derived_name, "parent_path": parent_path,
                })
        return records

    def search(
        self,
        query: str,
        limit: int = 10,
        action: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        sort: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Delegates to SearchEngine. Returns a dict with a `status` key:
        "ok" (single confident result), "clarify" (ambiguous -- caller
        should ask the user), or "no_match"."""
        if self._search_engine is None:
            from .search_engine import SearchEngine
            self._search_engine = SearchEngine(self)
        return self._search_engine.search(query=query, limit=limit, action=action, filters=filters, sort=sort)
