import os
import time
from pathlib import Path

from file_manager.indexer import FileIndexManager


def _write_test_file(path: Path, content: str = "sample") -> None:
    path.write_text(content, encoding="utf-8")


def test_latest_pdf_query_uses_metadata_sorting(tmp_path):
    manager = FileIndexManager(
        db_path=tmp_path / "index.sqlite3",
        config_path=tmp_path / "index.json",
    )

    older_pdf = tmp_path / "older.pdf"
    newer_pdf = tmp_path / "newer.pdf"
    _write_test_file(older_pdf, "older")
    _write_test_file(newer_pdf, "newer")

    older_time = time.time() - 120
    newer_time = time.time() - 30
    os.utime(older_pdf, (older_time, older_time))
    os.utime(newer_pdf, (newer_time, newer_time))

    manager._index_file(older_pdf)
    manager._index_file(newer_pdf)

    parsed = manager.parse_query("open my latest pdf")

    assert parsed["action"] == "open_file"
    assert parsed["filters"]["extension"] == ".pdf"
    assert parsed["sort"]["field"] == "modified_time"
    assert parsed["sort"]["direction"] == "descending"

    results = manager.search("open my latest pdf", limit=5)
    assert results[0]["path"] == str(newer_pdf)


def test_biggest_video_query_uses_size_sorting(tmp_path):
    manager = FileIndexManager(
        db_path=tmp_path / "index.sqlite3",
        config_path=tmp_path / "index.json",
    )

    smaller_video = tmp_path / "small.mp4"
    bigger_video = tmp_path / "big.mp4"
    _write_test_file(smaller_video, "x" * 100)
    _write_test_file(bigger_video, "x" * 400)

    manager._index_file(smaller_video)
    manager._index_file(bigger_video)

    parsed = manager.parse_query("find my biggest video")

    assert parsed["filters"]["extension"] == [".mp4", ".mov", ".avi", ".mkv", ".webm"]
    assert parsed["sort"]["field"] == "size"
    assert parsed["sort"]["direction"] == "descending"

    results = manager.search("find my biggest video", limit=5)
    assert results[0]["path"] == str(bigger_video)


def test_keyword_queries_are_not_ignored(tmp_path):
    manager = FileIndexManager(
        db_path=tmp_path / "index.sqlite3",
        config_path=tmp_path / "index.json",
    )

    hindi_file = tmp_path / "hindi_notes.pdf"
    unrelated_file = tmp_path / "math_notes.pdf"
    _write_test_file(hindi_file, "hindi")
    _write_test_file(unrelated_file, "math")

    manager._index_file(hindi_file)
    manager._index_file(unrelated_file)

    parsed = manager.parse_query("open my latest hindi file")
    assert parsed["keywords"] == ["hindi"]

    results = manager.search("open my latest hindi file", limit=5)
    assert results[0]["path"] == str(hindi_file)
