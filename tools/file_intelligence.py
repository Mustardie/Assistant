"""Evidence-based file purpose, risk, and intent search for general JARVIS.

Classification is deliberately bounded.  Small text-like files may be sampled;
large/binary/media files are described from metadata unless an optional safe
metadata reader is available.  A profile never claims content inspection when
only path/name/type evidence was used.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence


class FileCategory(str, Enum):
    DIRECTORY = "directory"
    DOCUMENT = "document"
    CODE = "code"
    CONFIG = "config"
    LOG = "log"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    ARCHIVE = "archive"
    INSTALLER = "installer"
    LOCAL_MODEL = "local_model"
    BUILD_ARTIFACT = "build_artifact"
    CONVERSATION_HISTORY = "conversation_history"
    SETTINGS = "settings"
    SECRET = "secret"
    DATA = "data"
    UNKNOWN = "unknown"


class FileRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FileSource(str, Enum):
    LOCAL = "local"
    EMAIL_ATTACHMENT = "email_attachment"
    GOOGLE_DRIVE = "google_drive"
    BROWSER_DOWNLOAD = "browser_download"
    MESSAGING_MEDIA = "messaging_media"
    CALENDAR_ATTACHMENT = "calendar_attachment"
    CONNECTOR = "connector"


@dataclass(frozen=True)
class FileIntent:
    purpose: str
    likely_project: str = ""
    domain: str = "general"
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileSummary:
    text: str
    content_inspected: bool = False
    inferred_from_metadata: bool = True
    extraction: str = "metadata"


@dataclass(frozen=True)
class FileRelationship:
    path: str
    relationship: str
    confidence: float = 0.5


@dataclass(frozen=True)
class FileActionSuggestion:
    action: str
    reason: str
    safe: bool
    requires_confirmation: bool = False
    confidence: float = 0.5


@dataclass(frozen=True)
class FileProfile:
    path: str
    filename: str
    extension: str
    size: int | None
    modified_time: float | None
    media_type: str
    category: FileCategory
    intent: FileIntent
    summary: FileSummary
    likely_project: str
    importance: str
    risk: FileRisk
    source: FileSource = FileSource.LOCAL
    safe_actions: tuple[FileActionSuggestion, ...] = ()
    unsafe_actions: tuple[FileActionSuggestion, ...] = ()
    related_files: tuple[FileRelationship, ...] = ()
    tags: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    git: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["category"] = self.category.value
        value["risk"] = self.risk.value
        value["source"] = self.source.value
        return value


_CODE_EXTENSIONS = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".java", ".cs", ".cpp", ".cc",
    ".c", ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".sh", ".ps1", ".bat", ".cmd", ".sql", ".html", ".css", ".scss", ".vue",
}
_CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".properties"}
_TEXT_EXTENSIONS = _CODE_EXTENSIONS | _CONFIG_EXTENSIONS | {".txt", ".md", ".rst", ".csv", ".tsv", ".log"}
_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".xls", ".xlsx"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"}
_INSTALLER_EXTENSIONS = {".exe", ".msi", ".msix", ".appx", ".dmg", ".pkg", ".deb", ".rpm"}
_MODEL_EXTENSIONS = {".onnx", ".safetensors", ".gguf", ".ggml", ".pt", ".pth", ".ckpt"}
_BUILD_EXTENSIONS = {".pyc", ".pyo", ".class", ".o", ".obj", ".dll", ".so", ".dylib", ".whl"}
_BUILD_PARTS = {"build", "dist", "out", "target", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".cache", "temp", "tmp"}
_PROTECTED_PARTS = {"windows", "program files", "program files (x86)", ".git", "system32"}
_SECRET_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "client_secret.json", "token.json", "id_rsa", "id_ed25519"}
_SETTINGS_WORDS = {"settings", "preferences", "profile", "config", "configuration"}
_CONVERSATION_WORDS = {"conversation", "conversations", "chat_history", "history", "messages"}
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*[:=]"),
    re.compile(r"(?i)(secret|token|password|credential)\s*[:=]"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----"),
]
_QUERY_STOPWORDS = {
    "find", "show", "open", "locate", "the", "a", "an", "my", "file", "files", "that", "this",
    "from", "for", "of", "to", "in", "is", "are", "should", "me", "please", "likely", "what",
}
_CONCEPTS = {
    "minecraft": {"minecraft", "fabric", "sodium", "iris", "replay", "crash", "latest.log"},
    "crash": {"crash", "exception", "traceback", "fatal", "error", "stacktrace", "log"},
    "worksheet": {"worksheet", "assignment", "homework", "school", "class", "questions", "pdf"},
    "hindi": {"hindi", "worksheet", "school", "class", "हिंदी"},
    "settings": {"settings", "preferences", "profile", "config", "personal", "secret"},
    "reference": {"reference", "inspiration", "design", "ui", "image", "screenshot", "mockup"},
    "render": {"render", "export", "output", "final", "video"},
    "junk": {"build", "cache", "temp", "generated", "artifact", "ignored", "junk"},
    "commit": {"git", "tracked", "untracked", "modified", "commit", "secret", "generated"},
    "tools": {"tools", "tool", "registry", "module", "python", "code"},
}


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_+#.-]+|[\u0900-\u097f]+", str(value).lower()))


def _bounded_text(path: Path, *, max_bytes: int = 48 * 1024) -> tuple[str, str]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return "", "metadata_only_large_file"
        raw = path.read_bytes()[:max_bytes]
        if b"\x00" in raw[:4096]:
            return "", "metadata_only_binary"
        return raw.decode("utf-8", errors="replace"), "bounded_text_sample"
    except (OSError, UnicodeError):
        return "", "metadata_only_unreadable"


def _bounded_pdf(path: Path) -> tuple[str, str]:
    try:
        if path.stat().st_size > 10 * 1024 * 1024:
            return "", "metadata_only_large_pdf"
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages[:3]:
            chunks.append((page.extract_text() or "")[:1800])
        return "\n".join(chunks)[:5000], "bounded_pdf_text"
    except Exception:
        return "", "metadata_only_pdf_parser_unavailable"


def _image_metadata(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image
        with Image.open(path) as image:
            return {"width": image.width, "height": image.height, "format": image.format or ""}
    except Exception:
        return {}


def _media_metadata(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=4, check=False,
        )
        if completed.returncode != 0:
            return {}
        payload = json.loads(completed.stdout or "{}")
        metadata: dict[str, Any] = {}
        duration = (payload.get("format") or {}).get("duration")
        if duration:
            metadata["duration_seconds"] = round(float(duration), 1)
        video = next((stream for stream in payload.get("streams") or [] if stream.get("codec_type") == "video"), None)
        if video:
            metadata.update({"width": video.get("width"), "height": video.get("height")})
        return metadata
    except Exception:
        return {}


def _category(path: Path, filename: str, extension: str, parts: set[str]) -> FileCategory:
    lower_name = filename.lower()
    if path.exists() and path.is_dir():
        return FileCategory.DIRECTORY
    if lower_name in _SECRET_NAMES or any(word in lower_name for word in ("secret", "credential", "private_key")):
        return FileCategory.SECRET
    if extension in _MODEL_EXTENSIONS or "models" in parts or "weights" in parts or (
        extension == ".bin" and any(word in lower_name for word in ("model", "weight"))
    ):
        return FileCategory.LOCAL_MODEL
    if parts & _BUILD_PARTS or extension in _BUILD_EXTENSIONS:
        return FileCategory.BUILD_ARTIFACT
    if any(word in lower_name for word in _CONVERSATION_WORDS) and extension in {".json", ".db", ".sqlite", ".sqlite3"}:
        return FileCategory.CONVERSATION_HISTORY
    if any(word in lower_name for word in _SETTINGS_WORDS) and extension in _CONFIG_EXTENSIONS | {".db", ".sqlite", ".sqlite3"}:
        return FileCategory.SETTINGS
    if extension == ".log" or "log" in lower_name or "crash-report" in str(path).lower():
        return FileCategory.LOG
    if extension in _CODE_EXTENSIONS:
        return FileCategory.CODE
    if extension in _CONFIG_EXTENSIONS or lower_name in {"dockerfile", "makefile", "requirements.txt", "pyproject.toml"}:
        return FileCategory.CONFIG
    if extension in _DOCUMENT_EXTENSIONS or extension in {".txt", ".md", ".rst"}:
        return FileCategory.DOCUMENT
    if extension in _IMAGE_EXTENSIONS:
        return FileCategory.IMAGE
    if extension in _VIDEO_EXTENSIONS:
        return FileCategory.VIDEO
    if extension in _AUDIO_EXTENSIONS:
        return FileCategory.AUDIO
    if extension in _ARCHIVE_EXTENSIONS:
        return FileCategory.ARCHIVE
    if extension in _INSTALLER_EXTENSIONS:
        return FileCategory.INSTALLER
    if extension in {".csv", ".tsv", ".db", ".sqlite", ".sqlite3", ".parquet"}:
        return FileCategory.DATA
    return FileCategory.UNKNOWN


def _git_context(path: Path) -> dict[str, Any]:
    probe = path if path.is_dir() else path.parent
    try:
        root_result = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if root_result.returncode != 0:
            return {"inside_repo": False}
        root = Path(root_result.stdout.strip()).resolve()
        try:
            relative = str(path.resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            return {"inside_repo": False}
        status_result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--ignored", "--", relative],
            capture_output=True, text=True, timeout=3, check=False,
        )
        line = next((item for item in status_result.stdout.splitlines() if item.strip()), "")
        code = line[:2] if len(line) >= 2 else ""
        return {
            "inside_repo": True,
            "root": str(root),
            "relative_path": relative,
            "status": code.strip() or "clean",
            "tracked": bool(code and code != "??" and code != "!!"),
            "untracked": code == "??",
            "ignored": code == "!!",
            "staged": bool(code and code[0] not in {" ", "?", "!"}),
            "modified": bool(code and len(code) > 1 and code[1] not in {" ", "?", "!"}),
        }
    except (OSError, subprocess.SubprocessError):
        return {"inside_repo": False}


def _relationships(path: Path, *, limit: int = 5) -> tuple[FileRelationship, ...]:
    if not path.exists() or path.is_dir():
        return ()
    try:
        siblings = list(path.parent.iterdir())[:250]
    except OSError:
        return ()
    stem = path.stem.lower()
    base = re.sub(r"(?:[-_.](?:final|copy|edited|render|export|v\d+|\d+))+$", "", stem)
    values = []
    for sibling in siblings:
        if sibling == path:
            continue
        other = sibling.stem.lower()
        if other == stem or (base and base in other):
            relation = "same_stem" if other == stem else "likely_variant"
            values.append(FileRelationship(str(sibling), relation, 0.8 if relation == "same_stem" else 0.62))
        if len(values) >= limit:
            break
    return tuple(values)


def _infer_purpose(path: Path, category: FileCategory, sample: str, metadata: dict[str, Any]) -> tuple[str, str, str, list[str], list[str], float]:
    lower_path = str(path).lower().replace("\\", "/")
    name = path.name.lower()
    text = sample.lower()
    tags: list[str] = [category.value]
    evidence: list[str] = [f"extension {path.suffix.lower() or '(none)'}", f"path {path.parent.name or path.parent}"]
    project = ""
    domain = "general"
    confidence = 0.58

    if sample and any(pattern.search(sample) for pattern in _SENSITIVE_PATTERNS):
        tags.extend(["secret", "credentials", "do_not_share"])
        evidence.append("secret-like assignment found in bounded text")
        return (
            f"{category.value.replace('_', ' ').title()} containing possible credentials; do not share or commit until reviewed.",
            path.parent.name,
            "security",
            tags,
            evidence,
            0.97,
        )

    if category == FileCategory.LOG and ("minecraft" in lower_path or any(token in text for token in ("minecraft", "fabric loader", "sodium", "iris"))):
        found = [token for token in ("Minecraft", "Fabric", "Sodium", "Iris") if token.lower() in lower_path or token.lower() in text]
        stack = "/".join(found) if found else "Minecraft"
        tags.extend(["minecraft", "crash", "log"])
        evidence.append(f"game/mod markers: {', '.join(found) or 'Minecraft path'}")
        return f"Crash log from {stack}.", "Minecraft", "gaming", tags, evidence, 0.94
    if category in {FileCategory.DOCUMENT, FileCategory.DATA} and ("hindi" in lower_path or "hindi" in text or "हिंदी" in sample):
        grade = re.search(r"(?:class|grade)[ _-]?(\d{1,2})", lower_path + " " + text)
        label = f"Class {grade.group(1)} Hindi worksheet" if grade else "Hindi school worksheet or notes"
        tags.extend(["hindi", "school", "worksheet"])
        evidence.append("Hindi/school terms in name or bounded text")
        return f"{label} {path.suffix.upper().lstrip('.') or 'document'}.", "School", "education", tags, evidence, 0.9
    if category == FileCategory.SECRET:
        tags.extend(["secret", "credentials", "do_not_share"])
        evidence.append("sensitive filename pattern")
        return "Credentials or secrets file; do not share or commit.", "Personal settings", "security", tags, evidence, 0.98
    if category == FileCategory.CONVERSATION_HISTORY:
        tags.extend(["personal", "conversation", "history"])
        evidence.append("conversation/history filename with structured data extension")
        return "Assistant conversation history containing personal context.", "JARVIS", "personal_data", tags, evidence, 0.92
    if category == FileCategory.SETTINGS:
        tags.extend(["settings", "personal", "do_not_share"])
        evidence.append("settings/profile filename")
        return "User or application settings; review before sharing or committing.", path.parent.name, "settings", tags, evidence, 0.9
    if category == FileCategory.LOCAL_MODEL:
        tags.extend(["model", "weights", "large", "do_not_commit"])
        evidence.append("model/weight extension or folder")
        return "Local model weights or runtime model data; do not commit.", path.parent.name, "machine_learning", tags, evidence, 0.96
    if category == FileCategory.BUILD_ARTIFACT:
        tags.extend(["generated", "build", "git_ignore_candidate"])
        evidence.append("build/cache folder or generated binary extension")
        return "Generated build or cache artifact; usually exclude from Git.", path.parent.name, "build", tags, evidence, 0.9
    if category == FileCategory.CODE and "/brain/" in lower_path:
        tags.extend(["jarvis", "brain", "runtime", "source"])
        evidence.append("source file under brain folder")
        return "JARVIS brain runtime source file.", "JARVIS", "assistant_runtime", tags, evidence, 0.95
    if category == FileCategory.CODE and "/tools/" in lower_path:
        tags.extend(["jarvis", "tool", "module", "source"])
        evidence.append("source file under tools folder")
        return f"JARVIS {path.suffix.lstrip('.').upper() or 'code'} tool module.", "JARVIS", "assistant_tools", tags, evidence, 0.94
    if category == FileCategory.CODE:
        language = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript/React",
            ".jsx": "JavaScript/React", ".java": "Java", ".cs": "C#", ".cpp": "C++",
            ".c": "C", ".rs": "Rust", ".go": "Go", ".ps1": "PowerShell", ".sh": "shell",
        }.get(path.suffix.lower(), "project")
        tags.extend(["source", "project"])
        evidence.append(f"recognized {language} source extension")
        return f"{language} project source file.", path.parent.name, "software", tags, evidence, 0.82
    if category == FileCategory.CONFIG:
        tags.extend(["project", "configuration"])
        if any(pattern.search(sample) for pattern in _SENSITIVE_PATTERNS):
            tags.extend(["secret", "do_not_share"])
            evidence.append("secret-like assignment found in bounded text")
            return "Project configuration containing possible credentials; do not share or commit until reviewed.", path.parent.name, "security", tags, evidence, 0.97
        return "Project or application configuration file.", path.parent.name, "configuration", tags, evidence, 0.82
    if category == FileCategory.LOG:
        crash = any(token in text or token in name for token in ("traceback", "exception", "fatal", "crash", "error"))
        tags.extend(["crash" if crash else "runtime", "diagnostic"])
        evidence.append("error/exception markers" if crash else "log extension/name")
        return ("Crash or error log for diagnosing a failed run." if crash else "Application runtime log."), path.parent.name, "diagnostics", tags, evidence, 0.86 if crash else 0.7
    if category == FileCategory.IMAGE:
        screenshot = "screenshot" in name or "screen shot" in name
        error = any(word in lower_path for word in ("error", "bug", "crash", "exception", "failure"))
        ui = any(word in lower_path for word in ("reference", "inspiration", "mockup", "ui", "design"))
        if error:
            summary = "Screenshot of an error or bug report (inferred from metadata)."
            tags.extend(["screenshot", "error", "debug"])
        elif ui:
            summary = "Image reference for UI or visual design (inferred from metadata)."
            tags.extend(["ui", "reference", "design"])
        elif screenshot:
            summary = "Screen capture (inferred from filename and image metadata)."
            tags.append("screenshot")
        else:
            summary = "Image asset or photograph (inferred from metadata)."
        if metadata.get("width"):
            evidence.append(f"image dimensions {metadata['width']}x{metadata['height']}")
        return summary, path.parent.name, "visual", tags, evidence, 0.77 if (screenshot or ui) else 0.62
    if category == FileCategory.VIDEO:
        replay = "replay" in lower_path or "minecraft" in lower_path
        render = any(word in lower_path for word in ("render", "export", "output", "final"))
        original = any(word in lower_path for word in ("footage", "original", "camera", "raw"))
        if replay:
            summary = "Minecraft replay or gameplay clip (inferred from metadata)."
            tags.extend(["minecraft", "replay", "gameplay"])
        elif render:
            summary = "Video render or export output (inferred from metadata)."
            tags.extend(["render", "export", "generated"])
        elif original:
            summary = "Original video footage; preserve before editing or moving (inferred from metadata)."
            tags.extend(["media_original", "footage"])
        else:
            summary = "Local video clip (inferred from metadata)."
        if metadata.get("duration_seconds") is not None:
            evidence.append(f"duration {metadata['duration_seconds']} seconds")
        return summary, path.parent.name, "media", tags, evidence, 0.82 if (replay or render or original) else 0.62
    if category == FileCategory.AUDIO:
        tags.append("audio")
        return "Local audio recording or music file (inferred from metadata).", path.parent.name, "audio", tags, evidence, 0.62
    if category == FileCategory.ARCHIVE:
        tags.extend(["archive", "compressed"])
        return "Compressed archive; inspect contents before extracting or sharing.", path.parent.name, "archive", tags, evidence, 0.76
    if category == FileCategory.INSTALLER:
        tags.extend(["installer", "executable", "verify_source"])
        return "Application installer or executable build artifact; verify its source before running.", path.parent.name, "software", tags, evidence, 0.84
    if category == FileCategory.DOCUMENT:
        tags.append("document")
        return "Text or office document; purpose is not clear from available evidence.", path.parent.name, "document", tags, evidence, 0.5
    if category == FileCategory.DIRECTORY:
        tags.append("folder")
        return "Folder containing related files; contents were not recursively inspected.", path.name, "folder", tags, evidence, 0.5
    return "File purpose is unclear from bounded metadata.", path.parent.name, domain, tags, evidence, confidence


def _risk(category: FileCategory, path: Path, tags: Sequence[str], git: dict[str, Any]) -> tuple[FileRisk, str]:
    parts = {part.lower() for part in path.parts}
    if category == FileCategory.SECRET or "secret" in tags:
        return FileRisk.CRITICAL, "credentials or secret evidence"
    if parts & _PROTECTED_PARTS:
        return FileRisk.CRITICAL, "protected system or repository metadata folder"
    if category in {FileCategory.SETTINGS, FileCategory.CONVERSATION_HISTORY, FileCategory.LOCAL_MODEL, FileCategory.CODE}:
        return FileRisk.HIGH, f"{category.value} should be preserved and reviewed"
    if "school" in tags or "media_original" in tags:
        return FileRisk.HIGH, "personal/original material"
    if category == FileCategory.BUILD_ARTIFACT and (git.get("ignored") or parts & _BUILD_PARTS):
        return FileRisk.LOW, "generated artifact with build/ignore evidence"
    if category == FileCategory.LOG and "secret" not in tags:
        return FileRisk.LOW, "diagnostic log without detected secret markers"
    return FileRisk.MEDIUM, "insufficient evidence for destructive or sharing claims"


def _actions(category: FileCategory, risk: FileRisk, git: dict[str, Any], risk_reason: str) -> tuple[tuple[FileActionSuggestion, ...], tuple[FileActionSuggestion, ...]]:
    safe = [
        FileActionSuggestion("open", "Read-only inspection does not modify the file.", True, False, 0.98),
        FileActionSuggestion("reveal", "Showing the containing folder is non-destructive.", True, False, 0.98),
        FileActionSuggestion("inspect_metadata", "Metadata inspection is bounded and read-only.", True, False, 0.98),
    ]
    unsafe: list[FileActionSuggestion] = []
    if category == FileCategory.BUILD_ARTIFACT and risk == FileRisk.LOW:
        safe.append(FileActionSuggestion("ignore_in_git", "Build/cache evidence supports excluding it from commits.", True, False, 0.9))
        safe.append(FileActionSuggestion("delete_after_review", "Likely regenerated, but deletion still requires confirmation.", True, True, 0.78))
    if git.get("tracked") and risk not in {FileRisk.CRITICAL} and category not in {FileCategory.SETTINGS, FileCategory.CONVERSATION_HISTORY, FileCategory.LOCAL_MODEL}:
        safe.append(FileActionSuggestion("consider_staging", "Git reports a tracked change; review the diff first.", True, True, 0.72))
    for action in ("delete", "move", "share"):
        unsafe.append(FileActionSuggestion(action, f"Requires explicit confirmation: {risk_reason}.", False, True, 0.95))
    if risk in {FileRisk.CRITICAL, FileRisk.HIGH} or category in {FileCategory.BUILD_ARTIFACT, FileCategory.LOCAL_MODEL}:
        unsafe.append(FileActionSuggestion("commit", f"Do not commit without review: {risk_reason}.", False, True, 0.93))
    return tuple(safe), tuple(unsafe)


def profile_file(path: str | Path, *, source: FileSource = FileSource.LOCAL, inspect_content: bool = True, include_git: bool = True) -> FileProfile:
    target = Path(path).expanduser()
    try:
        resolved = target.resolve(strict=False)
    except OSError:
        resolved = target.absolute()
    exists = resolved.exists()
    is_dir = exists and resolved.is_dir()
    extension = "" if is_dir else resolved.suffix.lower()
    filename = resolved.name or str(resolved)
    stat = None
    try:
        stat = resolved.stat() if exists else None
    except OSError:
        pass
    size = 0 if is_dir else (stat.st_size if stat else None)
    modified = stat.st_mtime if stat else None
    media_type = "inode/directory" if is_dir else (mimetypes.guess_type(filename)[0] or "application/octet-stream")
    parts = {part.lower() for part in resolved.parts}
    category = _category(resolved, filename, extension, parts)
    sample, extraction = "", "metadata"
    if inspect_content and exists and not is_dir:
        if extension in _TEXT_EXTENSIONS or filename.lower() in {"dockerfile", "makefile"}:
            sample, extraction = _bounded_text(resolved)
        elif extension == ".pdf":
            sample, extraction = _bounded_pdf(resolved)
    metadata: dict[str, Any] = {}
    if exists and category == FileCategory.IMAGE:
        metadata = _image_metadata(resolved)
    elif exists and category in {FileCategory.VIDEO, FileCategory.AUDIO}:
        metadata = _media_metadata(resolved)
    summary_text, project, domain, tags, evidence, confidence = _infer_purpose(resolved, category, sample, metadata)
    content_inspected = bool(sample)
    evidence.append(f"content {'sampled' if content_inspected else 'not inspected'} ({extraction})")
    if not exists:
        evidence.append("path does not currently exist; metadata inferred from name")
        confidence = min(confidence, 0.55)
    git = _git_context(resolved) if include_git and source == FileSource.LOCAL else {"inside_repo": False}
    if git.get("inside_repo"):
        evidence.append(f"git status {git.get('status')}")
        if git.get("ignored"):
            tags.append("git_ignored")
    risk, risk_reason = _risk(category, resolved, tags, git)
    evidence.append(f"risk {risk.value}: {risk_reason}")
    safe, unsafe = _actions(category, risk, git, risk_reason)
    intent = FileIntent(summary_text.rstrip("."), project, domain, confidence, tuple(evidence[:8]))
    summary = FileSummary(summary_text, content_inspected, not content_inspected, extraction)
    importance = "high" if risk in {FileRisk.CRITICAL, FileRisk.HIGH} else "low" if risk == FileRisk.LOW else "medium"
    return FileProfile(
        path=str(resolved), filename=filename, extension=extension, size=size, modified_time=modified,
        media_type=media_type, category=category, intent=intent, summary=summary,
        likely_project=project, importance=importance, risk=risk, source=source,
        safe_actions=safe, unsafe_actions=unsafe, related_files=_relationships(resolved),
        tags=tuple(dict.fromkeys(tags)), confidence=confidence, evidence=tuple(dict.fromkeys(evidence)), git=git,
    )


def profile_connector_item(item: dict[str, Any], *, source: FileSource = FileSource.CONNECTOR) -> FileProfile:
    """Create a non-local profile for an attachment/download-like connector item."""
    value = dict(item or {})
    filename = str(value.get("filename") or value.get("name") or value.get("title") or "connector-item")
    pseudo = Path(filename)
    profile = profile_file(pseudo, source=source, inspect_content=False, include_git=False)
    size = value.get("size") or value.get("size_bytes")
    media_type = str(value.get("mime_type") or value.get("media_type") or profile.media_type)
    evidence = tuple(profile.evidence) + (f"connector source {source.value}", "connector content not downloaded or inspected")
    return FileProfile(
        **{
            **profile.__dict__,
            "path": str(value.get("path") or value.get("url") or value.get("id") or filename),
            "filename": filename,
            "size": int(size) if isinstance(size, (int, float)) else None,
            "media_type": media_type,
            "source": source,
            "summary": FileSummary(profile.summary.text, False, True, "connector_metadata"),
            "evidence": evidence,
        }
    )


def assess_file_action(path: str | Path, action: str) -> dict[str, Any]:
    profile = profile_file(path, inspect_content=True)
    action_key = str(action).lower().strip()
    safe_match = next((item for item in profile.safe_actions if item.action == action_key), None)
    unsafe_match = next((item for item in profile.unsafe_actions if item.action == action_key), None)
    if unsafe_match:
        allowed = False
        reason = unsafe_match.reason
        confirmation = True
    elif safe_match:
        allowed = safe_match.safe
        reason = safe_match.reason
        confirmation = safe_match.requires_confirmation
    else:
        allowed = action_key in {"open", "reveal", "inspect_metadata"}
        reason = "No evidence supports treating this action as safe." if not allowed else "Read-only action."
        confirmation = not allowed
    return {
        "allowed_without_confirmation": allowed and not confirmation,
        "requires_confirmation": confirmation,
        "reason": reason,
        "risk": profile.risk.value,
        "profile": profile.to_dict(),
    }


def _expand_query(query: str) -> set[str]:
    values = _tokens(query) - _QUERY_STOPWORDS
    expanded = set(values)
    for token in values:
        expanded.update(_CONCEPTS.get(token, ()))
    if "not" in values and "commit" in values:
        expanded.update({"secret", "generated", "model", "settings", "ignored"})
    return expanded


def _cheap_record_score(record: dict[str, Any], query_tokens: set[str]) -> float:
    haystack = " ".join(
        str(record.get(key) or "") for key in ("filename", "path", "extension", "caption", "summary", "keywords")
    ).lower()
    record_tokens = _tokens(haystack)
    overlap = query_tokens & record_tokens
    score = len(overlap) * 1.8
    for token in query_tokens:
        if token in haystack:
            score += 0.7
    return score


def search_file_intent(query: str, records: Iterable[dict[str, Any]], *, limit: int = 10, profile_limit: int = 48) -> list[dict[str, Any]]:
    """Rank index records by inferred purpose, with bounded profiling."""
    query_tokens = _expand_query(query)
    cheap = sorted(
        ((record, _cheap_record_score(record, query_tokens)) for record in records if record.get("path")),
        key=lambda item: item[1], reverse=True,
    )[: max(limit, profile_limit)]
    results = []
    now = datetime.now(timezone.utc).timestamp()
    wants_recent = any(word in query.lower() for word in ("yesterday", "latest", "newest", "recent"))
    wants_not_commit = "not commit" in query.lower() or "shouldn't commit" in query.lower() or "should not commit" in query.lower()
    for record, cheap_score in cheap:
        include_git = wants_not_commit or "git" in query.lower() or "commit" in query.lower()
        profile = profile_file(record["path"], inspect_content=True, include_git=include_git)
        profile_tokens = _tokens(" ".join([profile.summary.text, profile.intent.purpose, profile.likely_project, " ".join(profile.tags), " ".join(profile.evidence)]))
        overlap = query_tokens & profile_tokens
        score = cheap_score + len(overlap) * 2.4 + profile.confidence
        reasons = [f"intent terms: {', '.join(sorted(overlap)[:8])}" if overlap else "metadata candidate"]
        if wants_not_commit and any(item.action == "commit" for item in profile.unsafe_actions):
            score += 4.0
            reasons.append("profile warns against committing")
        if wants_recent and profile.modified_time:
            age_hours = max(0.0, (now - profile.modified_time) / 3600)
            score += max(0.0, 2.0 - age_hours / 24.0)
            reasons.append(f"modified {age_hours:.1f} hours ago")
        results.append({
            "path": profile.path,
            "summary": profile.summary.text,
            "confidence": round(min(0.99, 0.35 + score / 18.0), 3),
            "score": round(score, 3),
            "evidence": list(profile.evidence) + reasons,
            "safe_next_actions": [asdict(item) for item in profile.safe_actions],
            "risk": profile.risk.value,
            "category": profile.category.value,
            "tags": list(profile.tags),
            "profile": profile.to_dict(),
        })
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[: max(1, int(limit))]


def enrich_search_response(query: str, response: dict[str, Any], records: Iterable[dict[str, Any]] | None = None, *, limit: int = 10) -> dict[str, Any]:
    """Attach profiles to legacy search results and use intent ranking when useful."""
    value = dict(response or {})
    candidates = []
    if value.get("status") == "ok" and isinstance(value.get("result"), dict):
        candidates = [value["result"]]
    elif value.get("status") == "clarify":
        candidates = [item for item in value.get("candidates") or [] if isinstance(item, dict)]
    enriched = []
    for candidate in candidates:
        path = candidate.get("path")
        if not path:
            continue
        profile = profile_file(path)
        profile_value = profile.to_dict()
        enriched.append({
            **candidate,
            "summary": profile.summary.text,
            "category": profile.category.value,
            "risk": profile.risk.value,
            "confidence": profile.confidence,
            "evidence": list(profile.evidence),
            "safe_next_actions": [asdict(item) for item in profile.safe_actions],
            "profile": profile_value,
        })
    intent_cues = any(word in query.lower() for word in (
        "crash", "worksheet", "assignment", "what is", "what's this", "settings", "reference",
        "build junk", "generated", "should not commit", "shouldn't commit", "render", "controls tools",
        "screenshot", "model weights", "safe to delete", "summarize",
    ))
    intent_results = search_file_intent(query, records or [], limit=limit) if records is not None and intent_cues else []
    if intent_results:
        top = intent_results[0]
        gap = top["score"] - (intent_results[1]["score"] if len(intent_results) > 1 else 99)
        intent_name = value.get("intent") or "search"
        if top["confidence"] >= 0.62 and (gap >= 1.2 or len(intent_results) == 1):
            return {**value, "status": "ok", "intent": intent_name, "result": top, "intent_ranked": True, "ranked_results": intent_results}
        return {**value, "status": "clarify", "intent": intent_name, "candidates": intent_results[:5], "intent_ranked": True, "ranked_results": intent_results}
    if value.get("status") == "ok" and enriched:
        value["result"] = enriched[0]
    elif value.get("status") == "clarify" and enriched:
        value["candidates"] = enriched
    return value
