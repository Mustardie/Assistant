"""
Locates the *actual* installation directory of a game or application, as
opposed to its save-game, config, cache, benchmark, or log folder.

This is what "where is Cyberpunk installed" needs: it should never be
answered by ranking indexed files, because save/benchmark/config folders
often contain more keyword-matching text (readmes, .ini files, logs) than
the real install directory does. Instead we go straight to the sources
that actually know where software is installed:

  1. Windows registry uninstall keys (InstallLocation)
  2. Steam library folders (libraryfolders.vdf + steamapps/common)
  3. Known install roots (Program Files, Epic Games, Xbox, etc.) as a
     filesystem fallback

Note: registry lookups only work when running on Windows (winreg import
guarded). Steam/filesystem checks are best-effort against common default
paths -- a fully general solution would also read Steam's registry key
for its install path rather than assuming C:/Program Files (x86)/Steam.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

COMMON_INSTALL_ROOTS = [
    "C:/Program Files",
    "C:/Program Files (x86)",
    "C:/Program Files/Epic Games",
    "C:/XboxGames",
]

STEAM_LIBRARY_HINTS = [
    "C:/Program Files (x86)/Steam/steamapps",
    "C:/Program Files/Steam/steamapps",
    "C:/SteamLibrary/steamapps",
    "D:/SteamLibrary/steamapps",
    "E:/SteamLibrary/steamapps",
]


class GameInstallationLocator:
    def find(self, name: str) -> List[Dict[str, Any]]:
        name_lower = name.lower().strip()
        if not name_lower:
            return []

        results: List[Dict[str, Any]] = []
        results.extend(self._from_registry(name_lower))
        results.extend(self._from_steam(name_lower))
        results.extend(self._from_filesystem(name_lower))

        seen = set()
        deduped = []
        for item in results:
            key = item["path"].lower()
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return deduped

    def _from_registry(self, name_lower: str) -> List[Dict[str, Any]]:
        if sys.platform != "win32":
            return []
        try:
            import winreg
        except ImportError:
            return []

        found = []
        uninstall_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, subkey in uninstall_paths:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            child_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, child_name) as child:
                                display_name = self._read_value(child, "DisplayName")
                                install_location = self._read_value(child, "InstallLocation")
                                if display_name and install_location and name_lower in display_name.lower():
                                    found.append({
                                        "path": install_location,
                                        "source": "registry",
                                        "display_name": display_name,
                                    })
                        except OSError:
                            continue
            except OSError:
                continue
        return found

    def _read_value(self, key, name: str) -> Optional[str]:
        try:
            import winreg
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value else None
        except OSError:
            return None

    def _from_steam(self, name_lower: str) -> List[Dict[str, Any]]:
        found = []
        for library_root in self._discover_steam_libraries():
            common_dir = Path(library_root) / "common"
            if not common_dir.exists():
                continue
            try:
                for child in common_dir.iterdir():
                    if child.is_dir() and name_lower in child.name.lower():
                        found.append({"path": str(child), "source": "steam", "display_name": child.name})
            except OSError:
                continue
        return found

    def _discover_steam_libraries(self) -> List[str]:
        libraries = []
        for hint in STEAM_LIBRARY_HINTS:
            path = Path(hint)
            if path.exists():
                libraries.append(str(path))
                libraries.extend(self._parse_library_folders_vdf(path / "libraryfolders.vdf"))
        return list(dict.fromkeys(libraries))

    def _parse_library_folders_vdf(self, vdf_path: Path) -> List[str]:
        if not vdf_path.exists():
            return []
        try:
            text = vdf_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        paths = re.findall(r'"path"\s*"([^"]+)"', text)
        return [str(Path(p.replace("\\\\", "\\")) / "steamapps") for p in paths]

    def _from_filesystem(self, name_lower: str) -> List[Dict[str, Any]]:
        found = []
        for root in COMMON_INSTALL_ROOTS:
            base = Path(root)
            if not base.exists():
                continue
            try:
                for child in base.iterdir():
                    if child.is_dir() and name_lower in child.name.lower():
                        found.append({"path": str(child), "source": "filesystem", "display_name": child.name})
            except OSError:
                continue
        return found
