"""Bounded Windows app discovery without recursive Program Files scans."""

from __future__ import annotations

import glob
import json
import os
import shutil
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Mapping

from tools.app_catalog import KNOWN_APPS, normalize_app_name, resolve_known_app
from tools.desktop_models import AppIdentity, AppStatus


ShortcutResolver = Callable[[Path], str | None]


def configured_aliases_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read optional aliases from JARVIS_APP_ALIASES JSON without user-file writes."""
    raw = (env or os.environ).get("JARVIS_APP_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {str(name): str(path) for name, path in value.items()} if isinstance(value, dict) else {}


def default_start_menu_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    values = env or os.environ
    roots = []
    for base in (values.get("ProgramData"), values.get("APPDATA")):
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return tuple(roots)


def resolve_windows_shortcut(path: Path) -> str | None:
    """Resolve a .lnk when pywin32 is present; absence is an evidence gap."""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return None
    try:
        shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
        target = str(shortcut.TargetPath or "").strip()
        return target or None
    except Exception:
        return None


class WindowsAppDiscovery:
    def __init__(self, *, env: Mapping[str, str] | None = None,
                 shortcut_roots: Iterable[Path | str] | None = None,
                 shortcut_resolver: ShortcutResolver | None = None,
                 configured_aliases: Mapping[str, str] | None = None,
                 path_lookup: Callable[[str], str | None] | None = None,
                 include_registry: bool = True):
        self.env = dict(env or os.environ)
        roots = shortcut_roots if shortcut_roots is not None else default_start_menu_roots(self.env)
        self.shortcut_roots = tuple(Path(item) for item in roots)
        self.shortcut_resolver = shortcut_resolver or resolve_windows_shortcut
        self.configured_aliases = dict(configured_aliases if configured_aliases is not None else configured_aliases_from_env(self.env))
        self.path_lookup = path_lookup or shutil.which
        self.include_registry = include_registry
        self.diagnostics: list[str] = []

    @staticmethod
    def _identity(name: str, path: str, source: str, evidence: list[str], aliases: tuple[str, ...] = ()) -> AppIdentity:
        known = resolve_known_app(name) or resolve_known_app(Path(path).name)
        canonical = known.canonical_name if known else name
        return AppIdentity(
            name=canonical, canonical_name=canonical, executable_path=str(Path(path)),
            status=AppStatus.UNKNOWN, safe_actions=("open", "focus"),
            risky_actions=("close", "kill_process"),
            confidence=0.94 if source == "configured_alias" else 0.86,
            evidence=tuple(evidence), source=source,
            aliases=tuple(known.aliases if known else aliases),
        )

    def _configured(self) -> list[AppIdentity]:
        values = []
        for name, raw_path in self.configured_aliases.items():
            expanded = os.path.expandvars(os.path.expanduser(str(raw_path)))
            if Path(expanded).is_file():
                values.append(self._identity(name, expanded, "configured_alias", [f"configured alias '{name}'", "target exists"]))
        return values

    def _shortcuts(self) -> list[AppIdentity]:
        values = []
        if self.shortcut_resolver is resolve_windows_shortcut:
            try:
                import win32com.client  # type: ignore  # noqa: F401
            except ImportError:
                self.diagnostics.append("Start Menu shortcut resolution unavailable: optional pywin32 dependency is not installed.")
                return values
        for root in self.shortcut_roots:
            if not root.is_dir():
                continue
            for shortcut in root.rglob("*.lnk"):
                target = self.shortcut_resolver(shortcut)
                if target and Path(target).is_file():
                    values.append(self._identity(shortcut.stem, target, "start_menu", [f"Start Menu shortcut: {shortcut}", "shortcut target exists"]))
        return values

    def _execution_aliases(self) -> list[AppIdentity]:
        local = self.env.get("LOCALAPPDATA") or self.env.get("LocalAppData")
        root = Path(local) / "Microsoft" / "WindowsApps" if local else None
        if not root or not root.is_dir():
            return []
        values = []
        for alias in root.glob("*.exe"):
            known = resolve_known_app(alias.name)
            if known and alias.is_file():
                values.append(self._identity(known.canonical_name, str(alias), "app_execution_alias",
                                             [f"Windows App Execution Alias: {alias.name}", "alias stub exists"], known.aliases))
        return values

    def _known_paths(self) -> list[AppIdentity]:
        values = []
        env_lower = {key.lower(): value for key, value in self.env.items()}
        for app in KNOWN_APPS:
            candidates: list[tuple[str, str]] = []
            for executable in app.executables:
                found = self.path_lookup(executable)
                if found:
                    candidates.append((found, "PATH executable"))
            for template in app.common_paths:
                expanded = template
                for token in set(part for part in template.split("%") if part):
                    replacement = env_lower.get(token.lower())
                    if replacement:
                        expanded = expanded.replace(f"%{token}%", replacement)
                if "%" in expanded:
                    continue
                for match in glob.glob(expanded):
                    if Path(match).is_file():
                        candidates.append((match, "known install path"))
            for path, source in candidates:
                values.append(self._identity(app.canonical_name, path, "known_path", [source, f"matched {app.canonical_name}"], app.aliases))
        return values

    def _registry_apps(self) -> list[AppIdentity]:
        if not self.include_registry or os.name != "nt":
            return []
        try:
            import winreg
        except ImportError:
            return []
        values = []
        for app in KNOWN_APPS:
            for executable in app.executables:
                subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
                for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(root, subkey) as key:
                            path = str(winreg.QueryValue(key, None))
                        if Path(path).is_file():
                            values.append(self._identity(app.canonical_name, path, "registry_app_paths", [f"Windows App Paths: {executable}"]))
                    except OSError:
                        continue
        return values

    def discover(self) -> list[AppIdentity]:
        self.diagnostics.clear()
        found = [*self._configured(), *self._shortcuts(), *self._known_paths(), *self._execution_aliases(), *self._registry_apps()]
        unique: dict[tuple[str, str], AppIdentity] = {}
        for item in found:
            key = (normalize_app_name(item.canonical_name), os.path.normcase(item.executable_path or ""))
            current = unique.get(key)
            if current is None or item.confidence > current.confidence:
                unique[key] = item
        return sorted(unique.values(), key=lambda item: (item.canonical_name.lower(), -item.confidence))

    def find(self, query: str) -> AppIdentity | None:
        key = normalize_app_name(query)
        if not key:
            return None
        scored = []
        known = resolve_known_app(query)
        for item in self.discover():
            names = (item.canonical_name, item.name, *item.aliases, Path(item.executable_path or "").stem)
            normalized = [normalize_app_name(name) for name in names]
            exact = key in normalized
            canonical_match = bool(known and item.canonical_name == known.canonical_name)
            similarity = max((SequenceMatcher(None, key, name).ratio() for name in normalized if name), default=0.0)
            score = 1.0 if exact else 0.98 if canonical_match else similarity
            scored.append((score, item.confidence, item))
        if not scored:
            return None
        score, _, result = max(scored, key=lambda value: (value[0], value[1]))
        return result if score >= 0.72 else None

    def explain_not_found(self, query: str) -> tuple[str, tuple[str, ...]]:
        known = resolve_known_app(query)
        fixes = ["Check the app name or add a configured app alias.", "Install the app and ensure its Start Menu shortcut is present."]
        if known and known.web_fallback:
            fixes.append(f"A web fallback is available: {known.web_fallback}")
        fixes.append("If installed in a custom folder, configure its executable path explicitly.")
        fixes.extend(self.diagnostics)
        return f"Could not find '{query}' in configured aliases, Start Menu shortcuts, known install paths, PATH, or Windows App Paths.", tuple(fixes)


def scan_start_menu() -> list[dict[str, str]]:
    discovery = WindowsAppDiscovery(include_registry=False)
    return [{"name": app.name, "target": app.executable_path or ""} for app in discovery._shortcuts()]


def build_database(path: Path | str | None = None) -> dict[str, dict]:
    """Build the legacy JSON index only when explicitly requested."""
    database = {
        normalize_app_name(app.canonical_name): {
            "name": app.canonical_name, "target": app.executable_path,
            "source": app.source, "aliases": list(app.aliases),
        }
        for app in WindowsAppDiscovery().discover()
    }
    if path is None:
        from config.paths import get_nova_app_file
        path = get_nova_app_file("apps.json")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(database, indent=2), encoding="utf-8")
    return database
