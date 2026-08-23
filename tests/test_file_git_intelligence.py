import subprocess

from tools.file_git import safe_stage_plan, summarize_git_status


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def test_git_summary_explains_safe_review_and_blocked_files(tmp_path):
    _git(tmp_path, "init")
    (tmp_path / "feature.py").write_text("print('safe source candidate')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=do-not-commit\n", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "module.pyc").write_bytes(b"generated")

    summary = summarize_git_status(tmp_path)
    assert summary.success
    assert summary.staged_anything is False
    assert "feature.py" in summary.safe_to_stage
    assert ".env" in summary.do_not_stage
    assert "build/module.pyc" in summary.do_not_stage
    assert "changed path" in summary.human_summary


def test_safe_stage_is_a_plan_and_never_runs_git_add(tmp_path):
    _git(tmp_path, "init")
    (tmp_path / "change.py").write_text("value = 1\n", encoding="utf-8")
    plan = safe_stage_plan(tmp_path)
    assert plan["success"]
    assert plan["requires_explicit_user_request_to_stage"] is True
    assert plan["staged_anything"] is False
    status = _git(tmp_path, "status", "--porcelain=v1").stdout
    assert status.startswith("??")

