import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from config.paths import get_nova_cache_dir, get_nova_index_dir
from .indexer import FileIndexManager


logger = logging.getLogger(__name__)


class SemanticFileManagerService:
    def __init__(self, roots: Optional[Sequence[Path]] = None, index_path: Optional[Path] = None, cache_dir: Optional[Path] = None):
        self.roots = [Path(root).expanduser().resolve() for root in (roots or [])]
        index_dir = get_nova_index_dir()

        self.index_path = Path(index_path or (index_dir / ".assistant_file_index.sqlite3")).resolve()
        self.cache_dir = Path(cache_dir or get_nova_cache_dir()).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        db_path = self.index_path if self.index_path.suffix.lower() in {".sqlite", ".sqlite3", ".db"} else self.index_path.with_suffix('.sqlite3')
        config_path = self.index_path if self.index_path.suffix.lower() == ".json" else self.index_path.with_suffix('.json')
        self.indexer = FileIndexManager(db_path=db_path, config_path=config_path)
        # Only start a full background indexing run when an initial index is required.
        # This preserves existing indexes and avoids rebuilding on every startup.
        try:
            if self.indexer.needs_initial_index():
                logger.info("No existing index found. Building initial index...")
                self.indexer.start_background_indexing()
            else:
                logger.info("Existing file index found. Skipping full rebuild.")
        except Exception:
            # If something goes wrong determining index state, fall back to starting indexing
            logger.exception("Error while checking index state; starting background indexing as a fallback")
            self.indexer.start_background_indexing()

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def index_paths(self, paths: Sequence[Path]) -> Dict[str, Any]:
        return self.indexer.index_roots(list(paths))

    def search(self, query: str, limit: int = 10, action: Optional[str] = None, filters: Optional[Dict[str, Any]] = None, sort: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Returns {status: 'ok'|'clarify'|'no_match', ...}. See search_engine.SearchEngine."""
        return self.indexer.search(query=query, limit=limit, action=action, filters=filters, sort=sort)

    # ------------------------------------------------------------------ #
    # Basic filesystem operations
    # ------------------------------------------------------------------ #

    def create_folder(self, path: Path) -> Dict[str, Any]:
        try:
            target = Path(path).expanduser().resolve()
            target.mkdir(parents=True, exist_ok=True)
            return {"success": True, "path": str(target)}
        except Exception as exc:
            logger.exception("Failed to create folder: %s", exc)
            return {"success": False, "error": str(exc)}

    def list_folder(self, path: Path) -> Dict[str, Any]:
        try:
            target = Path(path).expanduser().resolve()
            items = []
            for child in sorted(target.iterdir()):
                items.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "is_file": child.is_file(),
                })
            return {"success": True, "path": str(target), "items": items}
        except Exception as exc:
            logger.exception("Failed to list folder: %s", exc)
            return {"success": False, "error": str(exc)}

    def extract_archive(self, archive_path: str, destination: Optional[str] = None) -> Dict[str, Any]:
        from .operations import FileOperations
        return FileOperations(self).extract(archive_path, destination)

    def compress_archive(self, sources: Union[str, List[str]], destination_zip: str) -> Dict[str, Any]:
        from .operations import FileOperations
        return FileOperations(self).compress(sources, destination_zip)

    def reveal_in_explorer(self, path: str) -> Dict[str, Any]:
        from .operations import FileOperations
        return FileOperations(self).reveal_in_explorer(path)

    def show_properties(self, path: str) -> Dict[str, Any]:
        from .metadata import FileMetadata
        return FileMetadata(self).metadata(path)

    # ------------------------------------------------------------------ #
    # Indexing controls
    # ------------------------------------------------------------------ #

    def pause_indexing(self) -> Dict[str, Any]:
        return self.indexer.pause_indexing()

    def resume_indexing(self) -> Dict[str, Any]:
        return self.indexer.resume_indexing()

    def show_status(self) -> Dict[str, Any]:
        return self.indexer.status()

    def rebuild_index(self) -> Dict[str, Any]:
        return self.indexer.rebuild_index()

    def index_folder(self, path: str) -> Dict[str, Any]:
        return self.indexer.index_folder(path)

    def exclude_folder(self, path: str) -> Dict[str, Any]:
        return self.indexer.add_exclusion(path)

    def remove_from_index(self, path: str) -> Dict[str, Any]:
        return self.indexer.remove_from_index(path)
