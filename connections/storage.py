"""Secure credential and token storage.

Primary backend: Windows Credential Manager (via win32ctypes.win32cred).
Fallback: DPAPI-encrypted JSON files (CryptProtectData). This keeps
tokens out of plaintext config files while remaining dependency-light.
"""

import base64
import ctypes
import ctypes.wintypes
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Windows DPAPI fallback backend (CryptProtectData / CryptUnprotectData)
# --------------------------------------------------------------------- #
if sys.platform == "win32":

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    try:
        CryptProtectData = ctypes.windll.crypt32.CryptProtectData
        CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
        LocalFree = ctypes.windll.kernel32.LocalFree
    except Exception as exc:
        logger.warning("Failed to bind Crypt32 API: %s", exc)
        CryptProtectData = None
        CryptUnprotectData = None


def _dpapi_encrypt(data_bytes: bytes) -> bytes:
    if sys.platform != "win32" or not CryptProtectData:
        return base64.b64encode(data_bytes)
    in_blob = DATA_BLOB(
        len(data_bytes),
        ctypes.cast(ctypes.create_string_buffer(data_bytes), ctypes.POINTER(ctypes.c_byte)),
    )
    out_blob = DATA_BLOB()
    if CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            LocalFree(out_blob.pbData)
    logger.warning("CryptProtectData failed; falling back to base64 encoding")
    return base64.b64encode(data_bytes)


def _dpapi_decrypt(encrypted_bytes: bytes) -> bytes:
    if sys.platform != "win32" or not CryptUnprotectData:
        return base64.b64decode(encrypted_bytes)
    in_blob = DATA_BLOB(
        len(encrypted_bytes),
        ctypes.cast(ctypes.create_string_buffer(encrypted_bytes), ctypes.POINTER(ctypes.c_byte)),
    )
    out_blob = DATA_BLOB()
    if CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            LocalFree(out_blob.pbData)
    try:
        return base64.b64decode(encrypted_bytes)
    except Exception:
        raise ValueError("Failed to decrypt credentials using DPAPI")


def _cred_man_available() -> bool:
    """True when the Windows Credential Manager backend can be used."""
    if sys.platform != "win32":
        return False
    try:
        from win32ctypes.pywin32 import win32cred  # noqa: F401
        return True
    except Exception:
        return False


class CredentialStorage:
    """Stores service credentials/tokens securely.

    Windows Credential Manager is used when available; otherwise tokens
    are DPAPI-encrypted and stored as JSON files under data/credentials.
    """

    def __init__(self, storage_dir: str | Path | None = None, *, use_cred_manager: bool | None = None):
        self.storage_dir = Path(storage_dir) if storage_dir else (
            Path(__file__).resolve().parent.parent / "data" / "credentials"
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if use_cred_manager is None:
            self._use_cred_manager = _cred_man_available()
        else:
            self._use_cred_manager = use_cred_manager
        self._cache: dict[str, dict] = {}
        self._load_cache()

    # ------------------------------------------------------------------ #
    # Windows Credential Manager backend
    # ------------------------------------------------------------------ #

    @staticmethod
    def _cred_target(service_name: str) -> str:
        return f"jarvis/{service_name.lower().strip()}"

    def _cm_save(self, service_name: str, payload: bytes) -> bool:
        from win32ctypes.pywin32 import win32cred

        # pywin32-ctypes uses a dict-like PyCREDENTIAL structure.
        credential = {
            "Type": 1,  # CRED_TYPE_GENERIC
            "TargetName": self._cred_target(service_name),
            "UserName": service_name,
            "CredentialBlob": payload,
        }
        win32cred.CredWrite(credential, 0)
        return True

    def _cm_read(self, service_name: str) -> bytes | None:
        from win32ctypes.pywin32 import win32cred

        try:
            cred = win32cred.CredRead(self._cred_target(service_name), 1, 0)
        except Exception:
            return None
        if cred is None:
            return None
        blob = cred.get("CredentialBlob")
        return blob if isinstance(blob, bytes) else bytes(blob)

    def _cm_delete(self, service_name: str) -> bool:
        from win32ctypes.pywin32 import win32cred

        try:
            win32cred.CredDelete(self._cred_target(service_name), 1, 0)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # File backend (DPAPI-encrypted JSON)
    # ------------------------------------------------------------------ #

    def _get_filepath(self, service_name: str) -> Path:
        safe_name = "".join(c for c in service_name.lower() if c.isalnum() or c in ("_", "-"))
        return self.storage_dir / f"{safe_name}.dat"

    def _file_save(self, service_name: str, payload: bytes) -> bool:
        self._get_filepath(service_name).write_bytes(_dpapi_encrypt(payload))
        return True

    def _file_read(self, service_name: str) -> bytes | None:
        filepath = self._get_filepath(service_name)
        if not filepath.exists():
            return None
        return _dpapi_decrypt(filepath.read_bytes())

    def _file_delete(self, service_name: str) -> bool:
        filepath = self._get_filepath(service_name)
        if filepath.exists():
            filepath.unlink()
        return True

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def _load_cache(self):
        """Preload the cache (file backend) for list_services."""
        if self._use_cred_manager:
            return
        for file in self.storage_dir.glob("*.dat"):
            try:
                payload = _dpapi_decrypt(file.read_bytes())
                self._cache[file.stem] = json.loads(payload.decode("utf-8"))
            except Exception as exc:
                logger.warning("Could not decrypt credential file %s: %s", file, exc)

    def save_credentials(self, service_name: str, credentials: dict) -> bool:
        try:
            key = service_name.lower().strip()
            payload = json.dumps(credentials, ensure_ascii=False).encode("utf-8")
            if self._use_cred_manager:
                self._cm_save(key, payload)
            else:
                self._file_save(key, payload)
            self._cache[key] = credentials
            return True
        except Exception as exc:
            logger.error("Failed to save credentials for %s: %s", service_name, exc)
            return False

    def get_credentials(self, service_name: str) -> dict | None:
        key = service_name.lower().strip()
        if key in self._cache and not self._use_cred_manager:
            return dict(self._cache[key])
        try:
            if self._use_cred_manager:
                payload = self._cm_read(key)
            else:
                payload = self._file_read(key)
            if payload is None:
                return None
            data = json.loads(payload.decode("utf-8"))
            self._cache[key] = data
            return data
        except Exception as exc:
            logger.error("Failed to read credentials for %s: %s", service_name, exc)
            return None

    def delete_credentials(self, service_name: str) -> bool:
        key = service_name.lower().strip()
        self._cache.pop(key, None)
        try:
            if self._use_cred_manager:
                return self._cm_delete(key)
            return self._file_delete(key)
        except Exception as exc:
            logger.error("Failed to delete credentials for %s: %s", service_name, exc)
            return False

    def list_services(self) -> list[str]:
        if not self._use_cred_manager:
            return list(self._cache.keys())
        from win32ctypes.pywin32 import win32cred

        try:
            creds = win32cred.CredEnumerate("jarvis/", 0)
        except Exception:
            return list(self._cache.keys())
        services = []
        for cred in creds or []:
            target = cred.get("TargetName", "") if isinstance(cred, dict) else getattr(cred, "TargetName", "")
            if target.startswith("jarvis/"):
                services.append(target[len("jarvis/"):])
        return services


credential_storage = CredentialStorage()