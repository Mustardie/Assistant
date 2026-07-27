import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


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
        if not src.exists():
            return {"success": False, "error": f"Source does not exist: {src}"}
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                return {"success": False, "error": f"Destination already exists: {dst}"}
            shutil.move(str(src), str(dst))
            return {"success": True, "path": str(dst)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def copy(self, source: str, destination: str) -> Dict[str, Any]:
        src = Path(source).expanduser().resolve()
        dst = Path(destination).expanduser().resolve()
        if not src.exists():
            return {"success": False, "error": f"Source does not exist: {src}"}
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                return {"success": False, "error": f"Destination already exists: {dst}"}
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=False)
            else:
                shutil.copy2(src, dst)
            return {"success": True, "path": str(dst)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def delete(self, path: str, confirm: bool = False) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return {"success": False, "error": f"Path does not exist: {target}"}
        if not confirm:
            return {"success": False, "requires_confirmation": True, "path": str(target)}
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return {"success": True, "path": str(target)}
        except PermissionError as exc:
            return {"success": False, "error": f"Permission denied: {exc}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def compress(self, sources: Union[str, List[str]], destination_zip: str) -> Dict[str, Any]:
        dst = Path(destination_zip).expanduser().resolve()
        source_list = [sources] if isinstance(sources, str) else list(sources)
        suffix = dst.suffix.lower()
        name_lower = dst.name.lower()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                return {"success": False, "error": f"Destination already exists: {dst}"}

            if suffix == ".zip" or name_lower.endswith(".zip"):
                with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                    for source in source_list:
                        src = Path(source).expanduser().resolve()
                        if not src.exists():
                            continue
                        if src.is_file():
                            zf.write(src, arcname=src.name)
                        elif src.is_dir():
                            for file_path in src.rglob("*"):
                                if file_path.is_file():
                                    zf.write(file_path, arcname=str(file_path.relative_to(src.parent)))
                return {"success": True, "path": str(dst), "format": "zip"}

            if suffix in (".tar", ".gz", ".bz2", ".xz") or any(
                name_lower.endswith(ext) for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")
            ):
                mode = "w"
                if name_lower.endswith(".gz") or name_lower.endswith(".tgz"):
                    mode = "w:gz"
                elif name_lower.endswith(".bz2"):
                    mode = "w:bz2"
                elif name_lower.endswith(".xz"):
                    mode = "w:xz"
                with tarfile.open(dst, mode) as tf:
                    for source in source_list:
                        src = Path(source).expanduser().resolve()
                        if not src.exists():
                            continue
                        tf.add(src, arcname=src.name)
                fmt = "tar.gz" if "gz" in mode else "tar.bz2" if "bz2" in mode else "tar.xz" if "xz" in mode else "tar"
                return {"success": True, "path": str(dst), "format": fmt}

            return {"success": False, "error": f"Unsupported archive format: {suffix}. Supported: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def extract(self, archive_path: str, destination: Optional[str] = None) -> Dict[str, Any]:
        src = Path(archive_path).expanduser().resolve()
        dst = Path(destination).expanduser().resolve() if destination else src.parent / src.stem
        if not src.exists():
            return {"success": False, "error": f"Archive does not exist: {src}"}
        try:
            if dst.exists():
                stem = dst.stem
                suffix = dst.suffix
                dst = dst.parent / f"{stem}_extracted_{int(time.time())}{suffix}"
            dst.mkdir(parents=True, exist_ok=True)

            suffix = src.suffix.lower()
            name_lower = src.name.lower()

            if suffix == ".zip" or name_lower.endswith(".zip"):
                with zipfile.ZipFile(src) as zf:
                    zf.extractall(dst)
                return {"success": True, "path": str(dst), "format": "zip"}

            if suffix in (".tar", ".gz", ".bz2", ".xz") or any(
                name_lower.endswith(ext) for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")
            ):
                if not tarfile.is_tarfile(str(src)):
                    return {"success": False, "error": f"File is not a valid tar archive: {src}"}
                with tarfile.open(src) as tf:
                    tf.extractall(dst)
                fmt = "tar.gz" if name_lower.endswith(".gz") else "tar.bz2" if name_lower.endswith(".bz2") else "tar.xz" if name_lower.endswith(".xz") else "tar"
                return {"success": True, "path": str(dst), "format": fmt}

            return {"success": False, "error": f"Unsupported archive format: {suffix}. Supported: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz"}
        except (zipfile.BadZipFile, tarfile.ReadError) as exc:
            return {"success": False, "error": f"Invalid or corrupted archive: {exc}"}
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
