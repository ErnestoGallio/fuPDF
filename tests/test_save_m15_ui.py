"""M15 actions/dialog cancellation, view retention and save-before-close."""

import json
import subprocess
import sys

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.styles import FreeTextStyle
from pdf_annotator.domain.targets import CalloutTarget, RectTarget
from pdf_annotator.viewer.layout_engine import LayoutMode
from pdf_annotator.worker.protocol import ErrorInfo, Operation
from tests.conftest import wait_until
from tests.test_m6_service_persistence import _opened
from tests.test_save_m15 import ink, submit


def _inspect_freetext(path) -> list[dict[str, str]]:
    """Keep PyMuPDF inspection outside the pytest / GUI process."""
    result = subprocess.run([sys.executable, "-m", "tests.m15_helpers",
                             "inspect-freetext", str(path)], capture_output=True,
                            check=True, timeout=30)
    return json.loads(result.stdout)


def test_m15_ui_collection_does_not_import_pymupdf() -> None:
    assert "pymupdf" not in sys.modules and "fitz" not in sys.modules


@pytest.fixture
def window(app):
    window = MainWindow()
    window.show()
    yield window
    wait_until(lambda: not any(s.saving for s in window.manager.sessions.values()), timeout=180)
    assert window.manager.request_shutdown(discard_changes=True)
    wait_until(lambda: window.manager.is_shutdown)
    window.close()


def test_native_save_as_cancel_has_no_state_changes(tmp_path, window, pdf_fixture_copy):
    source = pdf_fixture_copy(tmp_path / "cancel.pdf")
    session = _opened(window.manager, source)
    before = (source.read_bytes(), session.identity, session.document_generation, session.history_state_id)
    assert window.save_action.shortcut().toString() == "Ctrl+S"
    assert window.save_as_action.shortcut().toString() == "Ctrl+Shift+S"
    window.save_as_action.trigger()
    dialog = window.save_dialog
    assert dialog is not None
    assert dialog.acceptMode() == QFileDialog.AcceptMode.AcceptSave
    assert not dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    assert dialog.nameFilters() == ["PDF (*.pdf)"]
    assert dialog.defaultSuffix() == "pdf"
    assert dialog.fileMode() == QFileDialog.FileMode.AnyFile
    assert dialog.testOption(QFileDialog.Option.DontConfirmOverwrite)
    dialog.reject()
    assert window.save_dialog is None
    assert not session.saving and not window.manager.saves.pending
    assert (source.read_bytes(), session.identity, session.document_generation, session.history_state_id) == before


def test_open_picker_native_options_async_selection_and_cancel(window, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(window.manager, "open_path", opened.append)
    assert not QApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    window.open_action.trigger()
    dialog = window.file_dialog
    assert dialog is not None
    assert not dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    assert dialog.fileMode() == QFileDialog.FileMode.ExistingFiles
    assert dialog.nameFilters() == ["PDF (*.pdf)"]
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    wait_until(lambda: bool(ticks))
    window.open_action.trigger()
    assert window.file_dialog is dialog
    dialog.reject()
    assert window.file_dialog is None and opened == []
    window.open_action.trigger()
    paths = [str(tmp_path / "first.pdf"), str(tmp_path / "second.pdf")]
    window.file_dialog.filesSelected.emit(paths)
    assert opened == paths  # Through the real open_paths -> manager boundary.
    window.file_dialog.reject()


def test_save_picker_selection_uses_existing_save_service(window, tmp_path, pdf_fixture_copy, monkeypatch):
    session = _opened(window.manager, pdf_fixture_copy(tmp_path / "source.pdf"))
    requested = []
    monkeypatch.setattr(window.manager.saves, "request", lambda *args: requested.append(args) or True)
    window.save_as_action.trigger()
    dialog = window.save_dialog
    assert dialog.directory().absolutePath() == str(tmp_path).replace("\\", "/")
    assert dialog.selectedFiles()[0].replace("\\", "/") == session.canonical_path.replace("\\", "/")
    destination = str(tmp_path / "saved.pdf")
    dialog.fileSelected.emit(destination)
    assert requested == [(session.document_id, destination)]
    dialog.reject()


def test_save_preserves_same_view_zoom_layout_scroll_search_sidebar(tmp_path, window, pdf_fixture_copy):
    session = _opened(window.manager, pdf_fixture_copy(tmp_path / "view.pdf"))
    view = window._widgets[session.document_id]
    submit(window.manager, ink(session))
    view.set_zoom(1.5)
    view.set_layout(LayoutMode.DOUBLE)
    view.verticalScrollBar().setValue(200)
    view.set_search_query("testo")
    wait_until(lambda: session.ui.search.complete, timeout=30)
    session.ui.sidebar_expanded = False
    before = (session.ui.zoom, session.ui.layout, view.verticalScrollBar().value(),
              session.ui.search.query, list(session.ui.search.results), session.ui.search.current_result,
              session.ui.sidebar_expanded)
    window.save_action.trigger()
    assert session.saving and not window.save_action.isEnabled() and not window.undo_action.isEnabled()
    wait_until(lambda: not session.saving, timeout=180)
    assert not session.dirty and session.document_generation == 2
    assert window._widgets[session.document_id] is view
    assert before == (session.ui.zoom, session.ui.layout, view.verticalScrollBar().value(),
                      session.ui.search.query, list(session.ui.search.results), session.ui.search.current_result,
                      session.ui.sidebar_expanded)
    wait_until(lambda: view.scheduler.settled, timeout=30)
    assert all(key.generation == 2 for key in window.manager.resident_budget.keys
               if key.document_id == session.document_id)
    assert window.undo_action.isEnabled()


def test_save_as_updates_tab_and_saves_close_before_worker_shutdown(tmp_path, window, pdf_fixture_copy, monkeypatch):
    source = pdf_fixture_copy(tmp_path / "original.pdf")
    session = _opened(window.manager, source)
    original = source.read_bytes()
    submit(window.manager, ink(session))
    destination = tmp_path / "new-name.pdf"
    assert window._save_document(session.document_id, str(destination))
    wait_until(lambda: not session.saving, timeout=180)
    assert source.read_bytes() == original
    view = window._widgets[session.document_id]
    assert window.tabs.tabText(window.tabs.indexOf(view)) == "new-name.pdf"
    submit(window.manager, ink(session))
    monkeypatch.setattr(window, "_close_choice", lambda session: QMessageBox.StandardButton.Save)
    worker = session.worker
    window._tab_closed(window.tabs.indexOf(view))
    assert session.saving and not worker.stopped.is_set()
    wait_until(lambda: session.document_id not in window.manager.sessions, timeout=180)
    assert worker.stopped.is_set() and not worker.forced_termination
    assert destination.is_file()


@pytest.mark.parametrize(("target", "text"), (
    (RectTarget(0, PdfRect(35, 45, 285, 155)), "Text Box incluso nel salvataggio"),
    (CalloutTarget(0, PdfRect(220, 120, 460, 250), PdfPoint(55, 80), PdfPoint(55, 120)),
     "Callout incluso nel salvataggio"),
), ids=("text-box", "callout"))
def test_save_confirms_active_freetext_editor_once_before_barrier(
        tmp_path, window, pdf_fixture_copy, target, text):
    session = _opened(window.manager, pdf_fixture_copy(tmp_path / "editor-save.pdf"))
    view = window._widgets[session.document_id]
    view._begin_text_box_editor(target, None, FreeTextStyle())
    view._text_editor.setPlainText(text)
    before = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]

    window.save_action.trigger()

    assert session.saving
    assert view._text_editor is None
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == before + 1
    wait_until(lambda: not session.saving, timeout=180)
    assert session.document_revision == session.saved_revision == 1
    assert len(session.annotations) == 1
    assert next(iter(session.annotations.values())).text == text
    assert _inspect_freetext(session.canonical_path) == [{"type": "FreeText", "content": text}]


def test_failure_presentation_with_open_editor_does_not_reenter_or_submit(
        tmp_path, window, pdf_fixture_copy):
    session = _opened(window.manager, pdf_fixture_copy(tmp_path / "failed-editor.pdf"))
    view = window._widgets[session.document_id]
    view._begin_text_box_editor(RectTarget(0, PdfRect(35, 45, 285, 155)), None, FreeTextStyle())
    view._text_editor.setPlainText("Contenuto recuperabile")
    before = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
    results = []
    window.manager.annotation_result.connect(results.append)

    # This is the formerly recursive path: session_changed while the failed
    # view still owns a confirmable editor.
    session.lifecycle = Lifecycle.FAILED
    session.error = ErrorInfo("FORCED", "Errore forzato")
    window.manager.session_changed.emit(session.document_id)
    window.manager.session_changed.emit(session.document_id)

    assert view.error_message == "Errore forzato"
    assert view._text_editor is not None
    assert view._text_editor.toPlainText() == "Contenuto recuperabile"
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == before
    assert results == []


def test_rejected_editor_confirmation_preserves_content_and_blocks_save(
        tmp_path, window, pdf_fixture_copy, monkeypatch):
    session = _opened(window.manager, pdf_fixture_copy(tmp_path / "reject-editor.pdf"))
    view = window._widgets[session.document_id]
    view._begin_text_box_editor(RectTarget(0, PdfRect(35, 45, 285, 155)), None, FreeTextStyle())
    view._text_editor.setPlainText("Non perdere questo testo")
    results = []
    window.manager.annotation_result.connect(results.append)
    monkeypatch.setattr(session.worker, "submit", lambda *_args, **_kwargs: None)

    assert not window._save_document(session.document_id)
    assert not session.saving
    assert view._text_editor is not None
    assert view._text_editor.toPlainText() == "Non perdere questo testo"
    assert len(results) == 1 and not results[0].success


def test_mutation_attempt_during_save_is_rejected_once_without_recursion(
        tmp_path, window, pdf_fixture_copy):
    session = _opened(window.manager, pdf_fixture_copy(tmp_path / "save-in-progress.pdf"))
    view = window._widgets[session.document_id]
    results = []
    window.manager.annotation_result.connect(results.append)

    assert window._save_document(session.document_id)
    assert session.saving
    view._begin_text_box_editor(RectTarget(0, PdfRect(35, 45, 285, 155)), None, FreeTextStyle())
    view._text_editor.setPlainText("Mutazione bloccata dal salvataggio")
    assert not view.try_confirm_text_box_editor()
    assert len(results) == 1 and not results[0].success
    assert view._text_editor is not None
    wait_until(lambda: not session.saving, timeout=180)
