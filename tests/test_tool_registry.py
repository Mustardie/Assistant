from tools.tool_registry import TOOLS, run_tool, tool_context


def test_run_tool_resolves_placeholders(monkeypatch):
    tool_context.clear()
    tool_context["file_search_result"] = {"path": "C:/Users/test/Documents/sample.txt"}

    def fake_tool(path):
        return {"used_path": path}

    monkeypatch.setitem(TOOLS, "file_open", fake_tool)

    success, result = run_tool("file_open", {"path": "{{file_search_result.path}}"})

    assert success is True
    assert result == {"used_path": "C:/Users/test/Documents/sample.txt"}
    assert tool_context["file_open_result"] == result


def test_run_tool_resolves_nested_placeholders_in_lists(monkeypatch):
    tool_context.clear()
    tool_context["file_search_result"] = {"candidates": [{"path": "C:/Users/test/file1.txt"}, {"path": "C:/Users/test/file2.txt"}]}

    def fake_tool(paths):
        return paths

    monkeypatch.setitem(TOOLS, "echo", fake_tool)

    success, result = run_tool("echo", {"paths": ["{{file_search_result.candidates.1.path}}", "static"]})

    assert success is True
    assert result == ["C:/Users/test/file2.txt", "static"]
    assert tool_context["echo_result"] == result


def test_run_tool_resolves_placeholders_inside_strings(monkeypatch):
    tool_context.clear()
    tool_context["file_search_result"] = {"path": "C:/Users/test/Documents/sample.txt"}

    def fake_tool(message):
        return message

    monkeypatch.setitem(TOOLS, "echo", fake_tool)

    success, result = run_tool("echo", {"message": "Opening {{file_search_result.path}} now."})

    assert success is True
    assert result == "Opening C:/Users/test/Documents/sample.txt now."
    assert tool_context["echo_result"] == result


def test_run_tool_returns_error_for_unresolved_placeholder(monkeypatch):
    tool_context.clear()

    def fake_tool(path):
        return path

    monkeypatch.setitem(TOOLS, "echo", fake_tool)

    success, result = run_tool("echo", {"path": "{{missing_result.path}}"})

    assert success is False
    assert "Unresolved placeholder" in result
    assert "echo_result" not in tool_context
