"""Install the CEP panel into Premiere.

Run once on the machine that has Premiere:

    python -m premiere.install

It copies ``extensions/PremiereBridge`` into the user's CEP extensions folder
and enables unsigned extensions (PlayerDebugMode). Both steps are required:
without the copy Premiere never sees the panel, and without PlayerDebugMode it
sees it and silently refuses to load it because the bundle is not signed with
an Adobe-issued certificate.

``--check`` reports what is installed without changing anything.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import xml.parsers.expat

SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "extensions", "PremiereBridge",
)

BUNDLE = "com.nova.premierebridge"

#: CEP looks for extensions in a per-user folder that differs by OS. The
#: version-numbered CSXS keys cover the CEP runtimes Premiere has shipped.
CSXS_VERSIONS = ["12", "11", "10", "9"]


def extensions_dir() -> str:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", "")
        return os.path.join(base, "Adobe", "CEP", "extensions")
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Adobe/CEP/extensions")
    return os.path.expanduser("~/.adobe/CEP/extensions")


def target_dir() -> str:
    return os.path.join(extensions_dir(), BUNDLE)


def validate_manifest(path: str = "") -> dict:
    """Whether CEP will accept the manifest.

    Worth its own step because of how this fails: CEP parses every manifest
    it finds, logs one DEBUG line when one is malformed, discards it, and
    carries on. From inside Premiere that is indistinguishable from the
    extension never having been installed -- no error, no menu entry, no
    panel -- so a broken manifest can hide for a long time. Checking it here
    turns that into a refusal at install time.

    The specific trap: XML forbids a double hyphen inside a comment, and
    command-line switches are the natural thing to write in a comment about
    CEFCommandLine.
    """
    target = path or os.path.join(SOURCE, "CSXS", "manifest.xml")
    if not os.path.isfile(target):
        return {"ok": False, "path": target, "error": "manifest.xml is missing"}
    try:
        parser = xml.parsers.expat.ParserCreate()
        with open(target, "rb") as handle:
            parser.ParseFile(handle)
    except xml.parsers.expat.ExpatError as exc:
        return {
            "ok": False,
            "path": target,
            "error": f"manifest.xml is not well-formed XML: {exc}",
            "hint": "CEP discards a malformed manifest silently, so the panel "
                    "would never appear in Premiere.",
        }
    return {"ok": True, "path": target}


def install(force: bool = False) -> dict:
    if not os.path.isdir(SOURCE):
        return {"success": False,
                "error": f"Extension source not found at {SOURCE}"}

    manifest = validate_manifest()
    if not manifest["ok"]:
        return {"success": False, "error": manifest["error"],
                "hint": manifest.get("hint", "")}

    destination = target_dir()
    if os.path.exists(destination):
        if not force:
            shutil.rmtree(destination, ignore_errors=True)
        else:
            shutil.rmtree(destination, ignore_errors=True)

    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copytree(SOURCE, destination)
    except OSError as exc:
        return {"success": False, "error": f"Could not copy the extension: {exc}"}

    debug = enable_debug_mode()
    return {
        "success": True,
        "installed_to": destination,
        "debug_mode": debug,
        "next_steps": [
            "Restart Adobe Premiere Pro completely (quit, not just close the project).",
            "Open Window > Extensions > Nova Premiere Bridge.",
            "Leave the panel open; run premiere_status() to confirm the connection.",
        ],
    }


def enable_debug_mode() -> dict:
    """Allow Premiere to load an unsigned CEP extension."""
    system = platform.system()
    results = {}

    if system == "Windows":
        try:
            import winreg
        except ImportError:
            return {"success": False, "error": "winreg unavailable"}
        for version in CSXS_VERSIONS:
            key_path = f"Software\\Adobe\\CSXS.{version}"
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    winreg.SetValueEx(key, "PlayerDebugMode", 0, winreg.REG_SZ, "1")
                results[version] = "set"
            except OSError as exc:
                results[version] = f"failed: {exc}"
        return {"success": any(v == "set" for v in results.values()),
                "keys": results}

    if system == "Darwin":
        for version in CSXS_VERSIONS:
            domain = f"com.adobe.CSXS.{version}"
            try:
                subprocess.run(
                    ["defaults", "write", domain, "PlayerDebugMode", "1"],
                    check=True, capture_output=True,
                )
                results[version] = "set"
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                results[version] = f"failed: {exc}"
        return {"success": any(v == "set" for v in results.values()),
                "domains": results}

    return {
        "success": False,
        "error": f"Premiere does not run on {system}; "
                 "install this on the machine running Premiere.",
    }


def check() -> dict:
    destination = target_dir()
    installed = os.path.isdir(destination)
    manifest = os.path.join(destination, "CSXS", "manifest.xml")
    return {
        "source": SOURCE,
        "source_present": os.path.isdir(SOURCE),
        "target": destination,
        "installed": installed,
        "manifest_present": os.path.isfile(manifest),
        "platform": platform.system(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report install state without changing anything")
    parser.add_argument("--force", action="store_true",
                        help="reinstall over an existing copy")
    args = parser.parse_args(argv)

    if args.check:
        state = check()
        state["manifest_valid"] = validate_manifest()
        for key, value in state.items():
            print(f"{key:18} {value}")
        return 0

    result = install(force=args.force)
    if not result.get("success"):
        print(f"Install failed: {result.get('error')}", file=sys.stderr)
        return 1

    print(f"Installed to {result['installed_to']}")
    if not result["debug_mode"].get("success"):
        print("WARNING: could not enable PlayerDebugMode; Premiere will refuse "
              "to load the unsigned panel.", file=sys.stderr)
        print(f"  {result['debug_mode']}", file=sys.stderr)
    for step in result["next_steps"]:
        print(f"  - {step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
