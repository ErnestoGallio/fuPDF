"""Application-private fonts used by canonical external FreeText editing."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase, QFontInfo


_FONT_DIR = Path(__file__).parents[1] / "assets" / "fonts"
_FILES = ("SourceSans3-Regular.ttf", "SourceSans3-Bold.ttf", "SourceSans3-Italic.ttf",
          "SourceSans3-BoldItalic.ttf", "CourierPrime-Regular.ttf", "CourierPrime-Bold.ttf",
          "CourierPrime-Italic.ttf", "CourierPrime-BoldItalic.ttf")
_FAMILIES = ("Source Sans 3", "Courier Prime")
_loaded = False


def ensure_canonical_editor_fonts() -> None:
    """Register the shipped files and reject platform fallback deterministically."""
    global _loaded
    if _loaded:
        return
    for filename in _FILES:
        path = _FONT_DIR / filename
        if not path.is_file() or QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError("I font editoriali integrati non sono disponibili.")
    for family in _FAMILIES:
        info = QFontInfo(QFont(family))
        if info.family() != family:
            raise RuntimeError("Qt non ha risolto il font editoriale richiesto.")
    _loaded = True
