import asyncio
import json
from threading import Event

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QLineEdit

from pdf_annotator.app import main_window
from pdf_annotator.domain.structured_text import ExtractionIdentity
from pdf_annotator.services.structured_text_export import StructuredTextWorker
from .conftest import wait_until
from .test_review_dialog import opened, window
from .test_save_m15 import save
from .test_structured_text import source


def test_file_export_binds_original_tab_and_keeps_qt_responsive(window, source, tmp_path, monkeypatch):
    assert not window.export_text_action.isEnabled()
    first = opened(window, source)
    assert window.export_text_action in window.file_menu.actions()
    assert window.export_text_action.isEnabled()

    class LocalDialog(QFileDialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.setOption(QFileDialog.Option.DontUseNativeDialog, True)

    monkeypatch.setattr(main_window, "QFileDialog", LocalDialog)
    output = tmp_path / "text.json"
    done, pulses = [], []
    window.text_exports.finished.connect(lambda *args: done.append(args))
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: pulses.append(1))
    timer.start()
    window.export_text_action.trigger()
    dialog = window.text_export_dialog
    assert dialog is not None
    # The dialog remains bound to the tab from which it was opened.
    other = tmp_path / "other.pdf"
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "tests.m6_fixtures", "text", str(other), "3", "0"], check=True, capture_output=True, timeout=30)
    second = opened(window, other)
    assert window.manager.active_id == second.document_id
    dialog.findChild(QLineEdit, "fileNameEdit").setText(str(output))
    dialog.accept()
    wait_until(lambda: bool(done), timeout=30)
    timer.stop()
    assert done == [(first.document_id, str(output))]
    data = json.loads(output.read_text(encoding="utf-8"))
    assert set(data) == {"pages"}
    assert len(data["pages"]) == 8
    assert len(pulses) >= 3
    assert not first.dirty and not second.dirty
    assert first.document_revision == first.history_length == 0
    assert window.reviews._scheduler is None


def test_save_as_waits_for_file_response_and_invalidates_old_references(window, source, tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(source.read_bytes())
    session = opened(window, path)
    captured, release = Event(), Event()
    original = StructuredTextWorker.page
    old_identity = ExtractionIdentity(session.document_id, session.document_generation, 0)

    async def hold(worker, *args, **kwargs):
        result = await original(worker, *args, **kwargs)
        captured.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return result

    monkeypatch.setattr(StructuredTextWorker, "page", hold)
    done = []
    window.text_exports.finished.connect(lambda *args: done.append(args))
    output = tmp_path / "text.json"
    assert window.text_exports.start(session.document_id, str(output))
    wait_until(captured.is_set)
    saved = tmp_path / "saved.pdf"
    try:
        save(window.manager, session, saved)
    finally:
        release.set()
    assert session.document_generation > old_identity.document_generation
    wait_until(lambda: not window.text_exports.busy)
    assert not done and not output.exists()
    assert not list(tmp_path.glob(".fupdf-text-*"))


def test_close_cancels_export_without_late_success(window, source, tmp_path, monkeypatch):
    session = opened(window, source)
    captured, release = Event(), Event()
    original = StructuredTextWorker.page

    async def hold(worker, *args, **kwargs):
        result = await original(worker, *args, **kwargs)
        captured.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return result

    monkeypatch.setattr(StructuredTextWorker, "page", hold)
    done = []
    window.text_exports.finished.connect(lambda *args: done.append(args))
    output = tmp_path / "text.json"
    assert window.text_exports.start(session.document_id, str(output))
    wait_until(captured.is_set)
    try:
        assert window.manager.request_close(session.document_id)
        wait_until(lambda: session.document_id not in window.manager.sessions)
    finally:
        release.set()
    wait_until(lambda: not window.text_exports.busy)
    assert not done and not output.exists()
