import builtins
import logging
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from pdf_annotator.util.logging import configure_logging


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "packaging/windows/launcher.py"


def test_launcher_import_does_not_start_gui(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name != "pdf_annotator.main"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    runpy.run_path(str(LAUNCHER), run_name="packaging_launcher_test")


def test_child_dispatch_happens_before_application_import(monkeypatch):
    import multiprocessing
    original = builtins.__import__

    def dispatched():
        raise SystemExit(37)

    def guarded(name, *args, **kwargs):
        assert name != "pdf_annotator.main"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(multiprocessing, "freeze_support", dispatched)
    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(LAUNCHER), run_name="__main__")
    assert result.value.code == 37


def test_parent_launcher_calls_existing_main_once(monkeypatch):
    import multiprocessing
    calls = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setitem(sys.modules, "pdf_annotator.main",
                        SimpleNamespace(main=lambda: calls.append("main") or 0))
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(LAUNCHER), run_name="__main__")
    assert result.value.code == 0
    assert calls == ["freeze", "main"]


def test_windowed_logging_without_standard_streams(tmp_path, monkeypatch):
    logger = logging.getLogger("pdf_annotator")
    old_handlers = logger.handlers[:]
    old_hook = sys.excepthook
    logger.handlers = []
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)
    try:
        runpy.run_path(str(ROOT / "packaging/windows/windowed_logging.py"))
        logger.error("startup diagnostic")
        configure_logging()
        assert len(logger.handlers) == 1
        try:
            raise RuntimeError("private document payload")
        except RuntimeError as error:
            sys.excepthook(type(error), error, error.__traceback__)
        for handler in logger.handlers:
            handler.flush()
        text = next((tmp_path / "fuPDF/logs").glob("*.log")).read_text(encoding="utf-8")
        assert "startup diagnostic" in text and "Unhandled RuntimeError" in text
        # Do not put exception values or source lines into the diagnostic record.
        assert "private document payload" not in text
        assert sys.stderr is not None and sys.stdout is not None
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = old_handlers
        sys.excepthook = old_hook
        if sys.stderr is not None:
            sys.stderr.close()
        if sys.stdout is not None:
            sys.stdout.close()


def test_console_logging_uses_existing_stream_without_disk(tmp_path, monkeypatch):
    import io
    logger = logging.getLogger("pdf_annotator")
    old_handlers = logger.handlers[:]
    logger.handlers = []
    stream = io.StringIO()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "stderr", stream)
    try:
        configure_logging().info("console diagnostic")
        assert "console diagnostic" in stream.getvalue()
        assert not (tmp_path / "fuPDF").exists()
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = old_handlers
