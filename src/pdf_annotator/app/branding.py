"""Shared source/frozen display identity and package-relative icon."""

from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


APPLICATION_NAME = "fuPDF"
ICON_PATH = Path(__file__).resolve().parents[1] / "assets/branding/fupdf.png"


def apply_branding(app: QApplication) -> None:
    app.setApplicationName(APPLICATION_NAME)
    icon = QIcon(str(ICON_PATH))
    if icon.isNull():
        raise RuntimeError("L'icona dell'applicazione non è disponibile.")
    app.setWindowIcon(icon)
