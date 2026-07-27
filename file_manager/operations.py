import platform
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Union


class FileOperations:
    def __init__(self, service):
        self.service = service

    def create(self, path: str) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        try:
            target.write_text("", encoding="utf-8")
            return {"success": True, "path": str(target)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def rename(self, source: str, destination: str) -> Dict[str, Any]:
        src = Path(source).expanduser().resolve()
        proposed = Path(destination)

        if not proposed.name:
            return {"success": False, "error": "No destination file name provided for rename."}

        if proposed.parent == Path("."):
            dst = src.parent / proposed.name
        else:
            dst = proposed

        if dst.parent != src.parent:
            return {
                "success": False,
                "error": (
                    "Rename should keep the file in the same directory. "
                    "Use move when you want to relocate the file to another folder."
                ),
            }

        if not dst.suffix:
            dst = dst.with_suffix(src.suffix)
        elif dst.suffix.lower() != src.suffix.lower():
            dst = dst.with_suffix(src.suffix)

        try:
            src.rename(dst)
            return {"success": True, "path": str(dst)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def move(self, source: str, destination: str) -> Dict[str, Any]:
        src = Path(source).expanduser().resolve()
        dst = Path(destination).expanduser().resolve()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return {"success": True, "path": str(dst)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def copy(self, source: str, destination: str) -> Dict[str, Any]:
        src = Path(source).expanduser().resolve()
        dst = Path(destination).expanduser().resolve()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                dst.write_bytes(src.read_bytes())
            else:
                dst.mkdir(parents=True, exist_ok=True)
            return {"success": True, "path": str(dst)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def delete(self, path: str, confirm: bool = False) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        if not confirm:
            return {"success": False, "requires_confirmation": True, "path": str(target)}
        try:
            if target.is_dir():
                target.rmdir()
            else:
                target.unlink()
            return {"success": True, "path": str(target)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def compress(self, sources: Union[str, List[str]], destination_zip: str) -> Dict[str, Any]:
        dst = Path(destination_zip).expanduser().resolve()
        source_list = [sources] if isinstance(sources, str) else list(sources)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                for source in source_list:
                    src = Path(source).expanduser().resolve()
                    if src.is_file():
                        zf.write(src, arcname=src.name)
                    elif src.is_dir():
                        for file_path in src.rglob("*"):
                            if file_path.is_file():
                                zf.write(file_path, arcname=str(file_path.relative_to(src.parent)))
            return {"success": True, "path": str(dst)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def extract(self, archive_path: str, destination: str = None) -> Dict[str, Any]:
        src = Path(archive_path).expanduser().resolve()
        dst = Path(destination).expanduser().resolve() if destination else src.parent / src.stem
        try:
            if src.suffix.lower() != ".zip":
                # .rar/.7z need a third-party library (rarfile/py7zr) that
                # isn't in the current dependency set -- flagging rather
                # than silently failing.
                return {"success": False, "error": f"Unsupported archive type for extraction: {src.suffix}"}
            with zipfile.ZipFile(src) as zf:
                zf.extractall(dst)
            return {"success": True, "path": str(dst)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def reveal_in_explorer(self, path: str) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        try:
            if platform.system().lower() == "windows":
                subprocess.Popen(["explorer", "/select,", str(target)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target.parent)])
            return {"success": True, "path": str(target)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
