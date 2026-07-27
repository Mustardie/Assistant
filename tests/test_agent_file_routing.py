from pathlib import Path

from brain.agent import Agent
from file_manager.service import SemanticFileManagerService


def test_service_uses_sqlite_index_for_temp_paths(tmp_path):
    root = tmp_path / "tests"
    root.mkdir(parents=True, exist_ok=True)
    service = SemanticFileManagerService(roots=[root], index_path=root / '.tmp_index.json', cache_dir=root / '.cache')
    assert service.indexer.db_path.suffix.lower() == '.sqlite3'


def test_file_request_is_detected(monkeypatch):
    agent = Agent()

    captured = {}

    def fake_run_tool(tool_name, arguments):
        captured['tool'] = tool_name
        captured['arguments'] = arguments
        return True, [{"path": "C:/Users/test/Documents/chemistry_notes.pdf"}]

    monkeypatch.setattr('brain.agent.run_tool', fake_run_tool)
    agent.run('find my chemistry notes')

    assert captured['tool'] == 'file_search'


def test_personal_latest_video_request_uses_file_search(monkeypatch):
    agent = Agent()

    captured = {"calls": []}

    def fake_run_tool(tool_name, arguments):
        captured['calls'].append(tool_name)
        captured['arguments'] = arguments
        if tool_name == 'file_search':
            return True, [{"path": "C:/Users/test/Videos/latest.mp4"}]
        if tool_name == 'file_open':
            return True, "Opened"
        return True, ""

    monkeypatch.setattr('brain.agent.run_tool', fake_run_tool)
    monkeypatch.setattr('brain.agent.Brain.think', lambda self, user: {"response": "", "steps": []})
    agent.run('open my latest video')

    assert captured['calls'][0] == 'file_search'
    assert 'youtube_recommend' not in captured['calls']


def test_entertainment_video_request_uses_youtube_routing(monkeypatch):
    agent = Agent()

    captured = {"calls": []}

    def fake_run_tool(tool_name, arguments):
        captured['calls'].append(tool_name)
        captured['arguments'] = arguments
        return True, ""

    monkeypatch.setattr('brain.agent.run_tool', fake_run_tool)
    monkeypatch.setattr('brain.agent.Brain.think', lambda self, user: {"response": "", "steps": []})
    agent.run('open a fun minecraft video')

    assert captured['calls'] == ['youtube_recommend']
