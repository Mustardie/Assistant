"""Known Windows application identities and conservative alias matching."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class KnownApp:
    canonical_name: str
    aliases: tuple[str, ...]
    executables: tuple[str, ...]
    common_paths: tuple[str, ...] = ()
    window_names: tuple[str, ...] = ()
    web_fallback: str | None = None


KNOWN_APPS: tuple[KnownApp, ...] = (
    KnownApp("Google Chrome", ("chrome", "google chrome"), ("chrome.exe",),
             (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"), ("chrome", "google chrome")),
    KnownApp("Microsoft Edge", ("edge", "ms edge", "microsoft edge"), ("msedge.exe",),
             (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"), ("msedge", "microsoft edge")),
    KnownApp("Discord", ("discord",), ("discord.exe",),
             (r"%LOCALAPPDATA%\Discord\Update.exe", r"%LOCALAPPDATA%\Discord\app-*\Discord.exe"), ("discord",)),
    KnownApp("WhatsApp", ("whatsapp", "whats app", "whatsapp desktop"), ("whatsapp.exe",),
             (r"%LOCALAPPDATA%\WhatsApp\WhatsApp.exe", r"%LOCALAPPDATA%\Microsoft\WindowsApps\WhatsApp.exe"), ("whatsapp",), "https://web.whatsapp.com/"),
    KnownApp("Spotify", ("spotify",), ("spotify.exe",),
             (r"%APPDATA%\Spotify\Spotify.exe", r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe"), ("spotify",)),
    KnownApp("Visual Studio Code", ("code", "vs code", "vscode", "visual studio code"), ("code.exe",),
             (r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe", r"%ProgramFiles%\Microsoft VS Code\Code.exe"), ("code", "visual studio code")),
    KnownApp("Adobe Premiere Pro", ("premiere", "premiere pro", "adobe premiere"), ("adobe premiere pro.exe",),
             (r"%ProgramFiles%\Adobe\Adobe Premiere Pro *\Adobe Premiere Pro.exe",), ("adobe premiere pro", "premiere")),
    KnownApp("DaVinci Resolve", ("davinci", "resolve", "davinci resolve"), ("resolve.exe",),
             (r"%ProgramFiles%\Blackmagic Design\DaVinci Resolve\Resolve.exe",), ("resolve", "davinci resolve")),
    KnownApp("Minecraft Launcher", ("minecraft", "minecraft launcher", "mc launcher"), ("minecraftlauncher.exe", "minecraft.exe"),
             (r"%ProgramFiles(x86)%\Minecraft Launcher\MinecraftLauncher.exe", r"%LOCALAPPDATA%\Microsoft\WindowsApps\Minecraft.exe"), ("minecraft launcher", "minecraft")),
    KnownApp("File Explorer", ("explorer", "file explorer", "windows explorer"), ("explorer.exe",),
             (r"%WINDIR%\explorer.exe",), ("explorer", "file explorer")),
    KnownApp("Notepad", ("notepad",), ("notepad.exe",), (r"%WINDIR%\System32\notepad.exe",), ("notepad",)),
    KnownApp("Calculator", ("calculator", "calc"), ("calculatorapp.exe", "calc.exe"),
             (r"%WINDIR%\System32\calc.exe", r"%LOCALAPPDATA%\Microsoft\WindowsApps\calc.exe"), ("calculator",)),
    KnownApp("Windows Terminal", ("terminal", "windows terminal", "wt"), ("windowsterminal.exe", "wt.exe"),
             (r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe",), ("windows terminal", "terminal")),
    KnownApp("PowerShell", ("powershell", "pwsh"), ("pwsh.exe", "powershell.exe"),
             (r"%ProgramFiles%\PowerShell\7\pwsh.exe", r"%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe"), ("powershell",)),
    KnownApp("LM Studio", ("lm studio", "lmstudio"), ("lm studio.exe",),
             (r"%LOCALAPPDATA%\Programs\LM Studio\LM Studio.exe",), ("lm studio",)),
    KnownApp("OBS Studio", ("obs", "obs studio"), ("obs64.exe", "obs32.exe"),
             (r"%ProgramFiles%\obs-studio\bin\64bit\obs64.exe",), ("obs", "obs studio")),
    KnownApp("Blender", ("blender",), ("blender.exe",),
             (r"%ProgramFiles%\Blender Foundation\Blender *\blender.exe",), ("blender",)),
)


def normalize_app_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


_ALIASES = {
    normalize_app_name(alias): app
    for app in KNOWN_APPS
    for alias in (app.canonical_name, *app.aliases, *app.executables, *app.window_names)
}


def resolve_known_app(value: str) -> KnownApp | None:
    key = normalize_app_name(value)
    if not key:
        return None
    exact = _ALIASES.get(key) or _ALIASES.get(key.removesuffix(" exe"))
    if exact:
        return exact
    matches = {
        app for alias, app in _ALIASES.items()
        if key == alias or (len(key) >= 4 and key in alias) or (len(alias) >= 4 and alias in key)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def matches_app(value: str, app: KnownApp | str) -> bool:
    known = resolve_known_app(app) if isinstance(app, str) else app
    if known is None:
        target = normalize_app_name(str(app))
        return bool(target and target in normalize_app_name(value))
    candidate = normalize_app_name(value)
    names = (known.canonical_name, *known.aliases, *known.executables, *known.window_names)
    return any(normalize_app_name(name) in candidate or candidate in normalize_app_name(name) for name in names if candidate)
