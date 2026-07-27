import tempfile
from pathlib import Path

from file_manager.service import SemanticFileManagerService


def test_semantic_search_returns_content_matches():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        notes = root / "notes.txt"
        notes.write_text("Quarterly planning for a dog adoption event in the park.", encoding="utf-8")

        service = SemanticFileManagerService(roots=[root], index_path=root / "index.json", cache_dir=root / ".cache")
        service.index_paths([root])

        results = service.search("dog adoption", limit=5)

        assert results
        assert results[0]["path"].endswith("notes.txt")


def test_create_folder_and_list_folder():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        service = SemanticFileManagerService(roots=[root], index_path=root / "index.json", cache_dir=root / ".cache")

        created = service.create_folder(root / "Projects")
        assert created["success"] is True

        listing = service.list_folder(root)
        assert any(item["name"] == "Projects" for item in listing["items"])
