"""Opt-in local logging with bounded disk usage and no PDF content."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import tempfile
import traceback


def windowed_log_path() -> Path:
    """Separate files avoid cross-process rotating-handler races."""
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    directory = root / "fuPDF/logs"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path(tempfile.gettempdir()) / "fuPDF/logs"
        directory.mkdir(parents=True, exist_ok=True)
    return directory / f"fuPDF-{os.getpid()}.log"


def configure_logging(path: Path | None = None) -> logging.Logger:
    windowed = sys.stderr is None
    if path is None and windowed:
        path = windowed_log_path()
    logger = logging.getLogger("pdf_annotator")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = (RotatingFileHandler(path, maxBytes=128 * 1024, backupCount=1,
                                       encoding="utf-8") if path else logging.StreamHandler())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    if windowed:
        def report_exception(kind, value, tb):
            # Exception messages may contain PDF text: retain type and frames only.
            frames = "\n".join(f"{frame.filename}:{frame.lineno} in {frame.name}"
                               for frame in traceback.extract_tb(tb))
            logger.error("Unhandled %s\n%s", kind.__name__, frames)
        sys.excepthook = report_exception
        # Native libraries may expect file-like standard streams even without a console.
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
    return logger
