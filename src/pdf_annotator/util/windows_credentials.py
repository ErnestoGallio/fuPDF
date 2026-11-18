"""Per-user generic credential storage for the portable Windows app."""

import ctypes
from ctypes import wintypes
import os
import re
from typing import Protocol


TARGET = "fuPDF/OpenAI"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_BLOB_BYTES = 2560


class CredentialError(Exception):
    """A classified error with no native message or credential contents."""


class CredentialStore(Protocol):
    def read(self) -> str | None: ...
    def write(self, value: str) -> None: ...
    def delete(self) -> None: ...


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_CredentialPointer = ctypes.POINTER(_Credential)


def _windows_api():
    try:
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    except (OSError, AttributeError):
        raise CredentialError("vault_unavailable") from None
    api.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(_CredentialPointer))
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = (_CredentialPointer, wintypes.DWORD)
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = (ctypes.c_void_p,)
    api.CredFree.restype = None
    return api


def validate_key(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[!-~]{1,2560}", value):
        raise CredentialError("invalid_key")
    return value


class WindowsCredentialStore:
    def __init__(self, *, target: str = TARGET, api_factory=_windows_api) -> None:
        self._target = target
        self._api_factory = api_factory

    def read(self) -> str | None:
        api = self._api_factory()
        pointer = _CredentialPointer()
        if not api.CredReadW(self._target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == ERROR_NOT_FOUND:
                return None
            raise CredentialError("vault_read_failed")
        try:
            blob = pointer.contents
            if not (0 < blob.CredentialBlobSize <= MAX_BLOB_BYTES and blob.CredentialBlob):
                raise CredentialError("invalid_key")
            return validate_key(ctypes.string_at(blob.CredentialBlob, blob.CredentialBlobSize).decode("ascii"))
        except UnicodeError:
            raise CredentialError("invalid_key") from None
        finally:
            api.CredFree(pointer)

    def write(self, value: str) -> None:
        data = validate_key(value).encode("ascii")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        try:
            record = _Credential(Type=CRED_TYPE_GENERIC, TargetName=self._target,
                                 CredentialBlobSize=len(data), CredentialBlob=buffer,
                                 Persist=CRED_PERSIST_LOCAL_MACHINE, UserName="OpenAI")
            if not self._api_factory().CredWriteW(ctypes.byref(record), 0):
                raise CredentialError("vault_write_failed")
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def delete(self) -> None:
        if (not self._api_factory().CredDeleteW(self._target, CRED_TYPE_GENERIC, 0)
                and ctypes.get_last_error() != ERROR_NOT_FOUND):
            raise CredentialError("vault_delete_failed")


def resolve_key(store: CredentialStore) -> tuple[str | None, str]:
    saved = store.read()
    if saved is not None:
        return validate_key(saved), "vault"
    development_key = os.environ.get("OPENAI_API_KEY", "").strip()
    return (validate_key(development_key), "environment") if development_key else (None, "missing")
