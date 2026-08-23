"""Read-only, human-oriented Git file advice for JARVIS.

Nothing in this module stages, deletes, restores, or modifies repository data.
It classifies the paths reported by Git and returns an explicit review plan.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tools.file_intelligence import FileCategory, FileRisk, profile_file


@dataclass(frozen=True)
class GitFileAdvice:
    path: str
    status: str
    summary: str
    risk: str
    recommendation: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitSummary:
    success: bool
    repository: str = ""
    branch: str = ""
    clean: bool = False
    human_summary: str = ""
    files: tuple[GitFileAdvice, ...] = ()
    safe_to_stage: tuple[str, ...] = ()
    review_before_staging: tuple[str, ...] = ()
    do_not_stage: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None
    staged_anything: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_git(root: Path, *arguments: str, timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _repository_root(path: str | Path | None) -> tuple[Path | None, str | None]:
    candidate = Path(path or Path.cwd()).expanduser()
    probe = candidate if candidate.is_dir() else candidate.parent
    try:
        result = _run_git(probe, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, (result.stderr or "Not inside a Git repository").strip()
    return Path(result.stdout.strip()).resolve(), None


def _parse_porcelain(output: str) -> list[tuple[str, str]]:
    """Parse porcelain v1, including ordinary rename lines.

    The status command uses newline output because Windows paths in this app are
    overwhelmingly conventional.  Quoted paths are unwrapped conservatively.
    """
    values: list[tuple[str, str]] = []
    for raw_line in output.splitlines():
        if len(raw_line) < 4:
            continue
        status = raw_line[:2]
        path_value = raw_line[3:].strip()
        if " -> " in path_value:
            path_value = path_value.rsplit(" -> ", 1)[-1]
        if len(path_value) >= 2 and path_value[0] == path_value[-1] == '"':
            path_value = path_value[1:-1]
        values.append((status, path_value.replace("\\", "/")))
    return values


def _recommendation(profile, status: str) -> tuple[str, list[str]]:
    reasons = [profile.summary.text, f"risk is {profile.risk.value}", f"Git status is {status}"]
    if profile.category in {
        FileCategory.SECRET,
        FileCategory.SETTINGS,
        FileCategory.CONVERSATION_HISTORY,
        FileCategory.LOCAL_MODEL,
        FileCategory.BUILD_ARTIFACT,
    }:
        reasons.append(f"{profile.category.value} files are not normal source changes")
        return "do_not_stage", reasons
    if profile.category == FileCategory.CODE:
        reasons.append("project source is a normal commit candidate; inspect its diff first")
        return "safe_candidate", reasons
    if profile.risk in {FileRisk.CRITICAL, FileRisk.HIGH}:
        reasons.append("high-risk content needs an explicit diff/content review")
        return "review", reasons
    if profile.size is not None and profile.size > 25 * 1024 * 1024:
        reasons.append("file is larger than 25 MiB")
        return "review", reasons
    if profile.category in {FileCategory.CONFIG, FileCategory.DOCUMENT}:
        reasons.append("normal project source/document type; inspect the diff before staging")
        return "safe_candidate", reasons
    reasons.append("purpose is not certain enough for automatic staging advice")
    return "review", reasons


def summarize_git_status(path: str | Path | None = None) -> GitSummary:
    root, error = _repository_root(path)
    if root is None:
        return GitSummary(False, error=error, human_summary=error or "Not inside a Git repository.")
    try:
        status_result = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
        branch_result = _run_git(root, "branch", "--show-current")
    except (OSError, subprocess.SubprocessError) as exc:
        return GitSummary(False, repository=str(root), error=str(exc), human_summary=f"Git status failed: {exc}")
    if status_result.returncode != 0:
        message = (status_result.stderr or "git status failed").strip()
        return GitSummary(False, repository=str(root), error=message, human_summary=message)

    entries = _parse_porcelain(status_result.stdout)
    advice: list[GitFileAdvice] = []
    safe: list[str] = []
    review: list[str] = []
    blocked: list[str] = []
    warnings: list[str] = []
    for status, relative in entries:
        target = root / relative
        profile = profile_file(target, include_git=False)
        recommendation, reasons = _recommendation(profile, status)
        if recommendation == "safe_candidate":
            safe.append(relative)
        elif recommendation == "do_not_stage":
            blocked.append(relative)
        else:
            review.append(relative)
        if profile.size is not None and profile.size > 25 * 1024 * 1024:
            warnings.append(f"Large file: {relative} ({profile.size / 1024 / 1024:.1f} MiB)")
        if profile.risk in {FileRisk.CRITICAL, FileRisk.HIGH}:
            warnings.append(f"{profile.risk.value.title()} risk: {relative} — {profile.summary.text}")
        advice.append(
            GitFileAdvice(
                relative,
                status,
                profile.summary.text,
                profile.risk.value,
                recommendation,
                tuple(reasons),
            )
        )

    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    if not entries:
        human = f"Repository {root.name} is clean on branch {branch or '(detached)'}."
    else:
        human = (
            f"Repository {root.name} has {len(entries)} changed path(s): "
            f"{len(safe)} normal candidate(s), {len(review)} needing review, "
            f"and {len(blocked)} that should not be staged."
        )
    return GitSummary(
        True,
        repository=str(root),
        branch=branch,
        clean=not entries,
        human_summary=human,
        files=tuple(advice),
        safe_to_stage=tuple(safe),
        review_before_staging=tuple(review),
        do_not_stage=tuple(blocked),
        warnings=tuple(dict.fromkeys(warnings)),
        staged_anything=False,
        metadata={"status_count": len(entries), "read_only": True},
    )


def safe_stage_plan(path: str | Path | None = None) -> dict[str, Any]:
    """Return exact suggested paths while intentionally staging nothing."""
    summary = summarize_git_status(path)
    value = summary.to_dict()
    value["action"] = "review_stage_plan"
    value["requires_explicit_user_request_to_stage"] = True
    value["staged_anything"] = False
    return value
