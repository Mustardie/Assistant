from pathlib import Path

from tools.tool_registry import file_open


def test_file_open_uses_startfile_on_windows(tmp_path, monkeypatch):
    target = tmp_path / "sample.pdf"
    target.write_text("sample", encoding="utf-8")

    calls = {}

    def fake_startfile(path):
        calls["path"] = path

    monkeypatch.setattr("tools.tool_registry.platform.system", lambda: "Windows")
    monkeypatch.setattr("tools.tool_registry.os.startfile", fake_startfile)

    result = file_open(target)

    assert result["success"] is True
    assert result["launched"] is True
    assert calls["path"] == str(target)
