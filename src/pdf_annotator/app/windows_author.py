"""Resolve the current Windows identity for new manual PDF annotations."""

import ctypes
from ctypes import wintypes

from ..domain.editorial_metadata import EditorialMetadata


def _display_name() -> str:
    lookup = ctypes.WinDLL("Secur32.dll").GetUserNameExW
    lookup.argtypes = (ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG))
    lookup.restype = ctypes.c_ubyte  # Win32 BOOLEAN
    buffer = ctypes.create_unicode_buffer(257)
    size = wintypes.ULONG(len(buffer))
    return buffer.value if lookup(3, buffer, ctypes.byref(size)) else ""  # NameDisplay


def _login_name() -> str:
    lookup = ctypes.WinDLL("Advapi32.dll").GetUserNameW
    lookup.argtypes = (wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    lookup.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(257)  # UNLEN plus the terminating NUL
    size = wintypes.DWORD(len(buffer))
    return buffer.value if lookup(buffer, ctypes.byref(size)) else ""


def manual_annotation_author() -> str:
    for lookup in (_display_name, _login_name):
        try:
            name = lookup().strip()
            EditorialMetadata(author=name)
        except (AttributeError, OSError, ValueError):
            continue
        return name
    return "fuPDF"
