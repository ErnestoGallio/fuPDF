from pathlib import Path
from threading import Event, get_ident
import asyncio
import json

import pytest
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QMessageBox

from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.services import review_store, review_transport
from pdf_annotator.services.review_contract.metrics import SOL_MODEL, TERRA_MODEL
from pdf_annotator.services.review_contract.prompt_profiles import PROMPT_PROFILES, PromptProfile
from pdf_annotator.services.review_jobs import ReviewScheduler
from pdf_annotator.services.review_transport import classify_response
from pdf_annotator.services.review_worker import ReviewWorker
from pdf_annotator.viewer.sticky_notes import note_icon_rect
from pdf_annotator.worker.protocol import ErrorInfo
from .conftest import wait_until
from .test_review_jobs import FakeClock, MockTransport, success


DUMMY = "synthetic-offline-secret-371"


class FakeVault:
    def __init__(self, value):
        self.value = value
        self.qt_thread = get_ident()

    def read(self):
        assert get_ident() != self.qt_thread
        return self.value

    def write(self, value):
        assert get_ident() != self.qt_thread
        self.value = value

    def delete(self):
        assert get_ident() != self.qt_thread
        self.value = None


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected real HTTPS")

    monkeypatch.setattr(review_transport, "HTTPSConnection", no_network)
    instance = MainWindow()
    reviews = instance.reviews
    clock = FakeClock()
    reviews.credential_store = FakeVault(DUMMY)
    reviews._scheduler_factory = lambda: ReviewScheduler(tmp_path / "jobs", clock=clock, sleep=clock.sleep,
                                                         jitter=lambda: 0)
    reviews._transport_factory = lambda _key: MockTransport(lambda *_: success())
    instance.show()
    yield instance
    instance.manager.request_shutdown(discard_changes=True)
    instance.close()
    wait_until(lambda: instance.manager.is_shutdown and reviews.is_shutdown, timeout=35)
    assert not instance.isVisible()


def opened(window, path):
    assert window.manager.open_path(str(path))
    wait_until(lambda: any(session.lifecycle == Lifecycle.READY and session.canonical_path == str(path)
                           and session.document_id in window._widgets for session in window.manager.sessions.values()))
    return next(session for session in window.manager.sessions.values() if session.canonical_path == str(path))


def show_dialog(window):
    assert window.review_action in window.file_menu.actions() and window.review_action.isEnabled()
    window.review_action.trigger()
    dialog = window.review_dialog
    wait_until(lambda: not window.reviews.credential_busy)
    assert dialog.isVisible() and not dialog.isModal()
    return dialog


def click(button):
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def report_for(window, session):
    wait_until(lambda: session.document_id in window.reviews.entries
               and window.reviews.entries[session.document_id].report is not None, timeout=35)
    return window.reviews.entries[session.document_id]


@pytest.mark.parametrize(("interval", "context"), [
    (None, {}),
    ((2, 3), {"indicazioni_aggiuntive": "Conserva è e Ω\nMantieni le note"}),
    ((1, 3), {"opera": "Titolo è", "destinatari": "Classe Ω", "indicazioni_aggiuntive": "Riga 1\nRiga 2"}),
])
def test_configuration_and_explicit_launch(window, pdf_fixture_copy, tmp_path, interval, context):
    assert not window.review_action.isEnabled()
    session = opened(window, pdf_fixture_copy(tmp_path / "test.pdf", pages=3))
    transport = MockTransport(lambda *_: success())
    window.reviews._transport_factory = lambda _key: transport
    dialog = show_dialog(window)
    assert window.reviews._scheduler is None and not transport.calls
    assert dialog.profile.currentData() == "schoolbook"
    assert dialog.profile.currentText() == "Libri scolastici"
    assert dialog.model.currentData() == TERRA_MODEL
    assert dialog.page_mode.currentText() == "Intero documento"
    if interval:
        dialog.page_mode.setCurrentIndex(1)
        dialog.first_page.setValue(3)
        dialog.last_page.setValue(1)
        assert (dialog.first_page.value(), dialog.last_page.value()) == (1, 1)
        assert dialog.start_button.isEnabled()
        dialog.first_page.setValue(interval[0])
        dialog.last_page.setValue(interval[1])
    for key, value in context.items():
        dialog.context[key].setPlainText(value)
    start, stop = interval or (1, 3)
    click(dialog.start_button)
    assert not window.reviews.start(session.document_id, profile="schoolbook", first=0, last=0, context={})
    entry = report_for(window, session)
    assert len(transport.calls) == 1
    first, last, request = transport.calls[0]
    assert (first, last) == (start - 1, stop - 1)
    assert request["model"] == TERRA_MODEL and request["reasoning"] == {"effort": "medium"}
    messages = request["input"][0]["content"]
    if context:
        assert json.loads(messages[0]["text"].split("\n", 1)[1]) == context
    else:
        assert len(messages) == 2 and messages[0]["type"] == "input_text"
    assert entry.report["status"] == "completed"
    assert f"Pagine analizzate {stop - start + 1}/{stop - start + 1}" in dialog.progress.text()
    assert "USD" in dialog.progress.text() and "Applicate 0" in dialog.summary.text()
    assert not session.dirty
    assert all(DUMMY not in path.read_text(encoding="utf-8") for path in entry.job.store.directory.rglob("*.json"))


def test_unknown_model_rejected_before_preparing_transport(window, pdf_fixture_copy, tmp_path):
    session = opened(window, pdf_fixture_copy(tmp_path / "unknown.pdf"))
    dialog = show_dialog(window)
    dialog.model.addItem("Sconosciuto", "unknown-model")
    dialog.model.setCurrentIndex(dialog.model.count() - 1)
    click(dialog.start_button)
    assert not window.reviews.credential_busy and not window.reviews.busy(session.document_id)
    assert window.reviews._scheduler is None
    assert "non valido" in dialog.state.text()


def test_credential_ui_replaces_removes_and_clears_password(window, pdf_fixture_copy, tmp_path, monkeypatch, caplog):
    opened(window, pdf_fixture_copy(tmp_path / "credential.pdf"))
    window.reviews.credential_store.value = None
    dialog = show_dialog(window)
    assert not dialog.start_button.isEnabled() and "Configura" in dialog.credential_status.text()
    click(dialog.credentials_button)
    assert dialog.key.echoMode() == QLineEdit.EchoMode.Password
    for value in (DUMMY, DUMMY + "-new"):
        dialog.key.setText(value)
        assert value not in dialog.key.displayText()
        click(dialog.store_key)
        assert not dialog.key.text()
        wait_until(lambda: window.reviews.credential_state == "vault" and not window.reviews.credential_busy)
        assert window.reviews.credential_store.value == value
        assert value not in caplog.text
        assert all(value not in label.text() for label in dialog.findChildren(QLabel))
    assert window.reviews._scheduler is None
    monkeypatch.setenv("OPENAI_API_KEY", "offline-development-value")
    click(dialog.delete_key)
    wait_until(lambda: window.reviews.credential_state == "environment")
    assert window.reviews.credential_store.value is None and dialog.start_button.isEnabled()
    assert "OPENAI_API_KEY" in dialog.credential_status.text()
    assert "offline-development-value" not in dialog.credential_status.text()
    dialog.key.setText(DUMMY)
    dialog.close()
    assert not dialog.key.text()


def test_profile_selector_is_populated_from_registry(window, pdf_fixture_copy, tmp_path, monkeypatch):
    opened(window, pdf_fixture_copy(tmp_path / "profile.pdf"))
    monkeypatch.setitem(PROMPT_PROFILES, "offline-test-entry",
                        PromptProfile("Profilo presente nel registro", PROMPT_PROFILES["schoolbook"].path))
    dialog = show_dialog(window)
    assert dialog.profile.currentData() == "schoolbook" and dialog.profile.findData("offline-test-entry") >= 0


def test_physical_page_steppers_mouse_keyboard_and_bounds(window, pdf_fixture_copy, tmp_path):
    opened(window, pdf_fixture_copy(tmp_path / "pages.pdf"))
    dialog = show_dialog(window)
    dialog.page_mode.setCurrentIndex(1)
    first, last = dialog.first_page, dialog.last_page
    first_down, first_up = dialog._steps[first]
    last_down, last_up = dialog._steps[last]
    assert (first.value(), last.value()) == (1, 6)
    assert not first_down.isEnabled() and not last_up.isEnabled()
    click(first_up)
    click(last_down)
    assert (first.value(), last.value()) == (2, 5)
    QTest.keyClick(first, Qt.Key.Key_Up)
    QTest.keyClick(last, Qt.Key.Key_Down)
    assert (first.value(), last.value()) == (3, 4)
    first.lineEdit().selectAll()
    QTest.keyClicks(first.lineEdit(), "6")
    QTest.keyClick(first.lineEdit(), Qt.Key.Key_Return)
    assert (first.value(), last.value()) == (6, 6) and not first_up.isEnabled()
    last.lineEdit().selectAll()
    QTest.keyClicks(last.lineEdit(), "1")
    QTest.keyClick(last.lineEdit(), Qt.Key.Key_Return)
    assert (first.value(), last.value()) == (1, 1) and not last_down.isEnabled()
    dialog.resize(dialog.width() + 120, dialog.height())
    click(last_up)
    assert (first.value(), last.value()) == (1, 2)


def test_monitor_minimizes_independently_and_restores_the_origin_job(window, app, pdf_fixture_copy, tmp_path):
    origin = opened(window, pdf_fixture_copy(tmp_path / "monitor.pdf"))
    entered, release = Event(), Event()

    def post(*_):
        entered.set()
        assert release.wait(18)
        return success()

    window.reviews._transport_factory = lambda _key: MockTransport(post)
    dialog = show_dialog(window)
    setup_height = dialog.height()
    assert dialog.isWindow() and dialog.parentWidget() is None
    assert dialog.windowHandle().transientParent() is None
    assert dialog.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    assert not dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    click(dialog.start_button)
    try:
        wait_until(entered.is_set, timeout=20)
        wait_until(lambda: dialog.height() < setup_height)
        window.activateWindow()
        wait_until(lambda: app.activeWindow() is window)
        before = origin.ui.zoom
        window.zoom_in_action.trigger()
        assert origin.ui.zoom > before
        dialog.showMinimized()
        wait_until(dialog.isMinimized)
        assert window.reviews.busy(origin.document_id)
    finally:
        release.set()
    entry = report_for(window, origin)
    assert entry.report["status"] == "completed" and dialog.isMinimized()
    assert not entry.job._cancel.is_set()
    click(window.review_indicator)
    wait_until(lambda: dialog.isVisible() and not dialog.isMinimized())
    assert dialog.document_id == origin.document_id and "Completata" in dialog.state.text()
    dialog.close()
    click(window.review_indicator)
    assert window.review_dialog is dialog and dialog.document_id == origin.document_id


def _delete(page, text):
    return {"operation": "delete", "page_index": page, "original_text": text, "replacement_text": None,
            "insert_anchor": None, "reason": "Motivazione di prova", "context_before": None, "context_after": None,
            "category": "clarity"}


def test_monitor_rebuilds_unpublished_report_and_resumes_without_api(window, pdf_fixture_copy, tmp_path, monkeypatch):
    session = opened(window, pdf_fixture_copy(tmp_path / "recover.pdf", pages=2))
    transport = MockTransport(lambda *_: success([_delete(0, "Prima riga")]))
    window.reviews._transport_factory = lambda _key: transport
    write_json = review_store.write_json
    failures = 2

    def publish(path, value):
        nonlocal failures
        if path.name == "report.json" and failures:
            failures -= 1
            raise OSError("Simulated report publication failure")
        write_json(path, value)

    monkeypatch.setattr(review_store, "write_json", publish)
    dialog = show_dialog(window)
    click(dialog.start_button)
    entry = report_for(window, session)
    directory = entry.job.store.directory
    result, = (directory / "results").glob("*.json")
    initial_result = result.read_bytes()
    assert entry.report["error"] == "persistence_failed" and entry.report["completed_pages"] == 2
    assert entry.report["application"]["status"] == "deferred" and not session.annotations
    assert not (directory / "report.json").exists()
    assert dialog.resume_button.isVisible() and not dialog.retry_button.isVisible()
    assert "Applicazione non avviata" in dialog.state.text()

    window.reviews.delete_key()
    wait_until(lambda: window.reviews.credential_state == "missing")

    def unreadable_ledger():
        raise OSError("Simulated archive read failure")

    with monkeypatch.context() as patch:
        patch.setattr(entry.job.store, "collect", unreadable_ledger)
        click(dialog.resume_button)
        current = entry.job.future
        wait_until(lambda: entry.future is current)
        assert entry.report["error"] == "review_recovery_unavailable"
        assert "Impossibile ricostruire il report" in dialog.state.text()
        assert dialog.resume_button.isVisible() and not session.annotations

    for status in ("deferred", "completed"):
        click(dialog.resume_button)
        current = entry.job.future
        wait_until(lambda: entry.future is current)
        assert entry.report["application"]["status"] == status
        assert len(transport.calls) == 1 and result.read_bytes() == initial_result
        if status == "deferred":
            assert entry.report["error"] == "persistence_failed" and not session.annotations
            assert dialog.resume_button.isVisible()

    published = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert published["cycle"] == 1 and published["completed_pages"] == 2 and not published["error"]
    assert entry.report["application"]["applied"] == session.document_revision == len(session.annotations) == 1
    assert not dialog.resume_button.isVisible()
    current = window.reviews._scheduler.resume_application(entry.job)
    wait_until(current.done)
    assert current.result()["application"]["already_applied"] == 1
    assert len(transport.calls) == 1 and session.document_revision == len(session.annotations) == 1


@pytest.mark.parametrize("page_index", [0, 1, 2, 3])
def test_ai_note_is_painted_and_readable_at_page_edge_before_and_after_save(
        window, pdf_fixture_copy, tmp_path, page_index):
    source = pdf_fixture_copy(tmp_path / "note-source.pdf", geometry=True)
    session = opened(window, source)
    view = window._widgets[session.document_id]
    view.set_zoom(.25)
    text = "Nota nel margine · città\nSeconda riga"
    proposal = {"operation": "comment", "page_index": page_index, "original_text": None,
                "replacement_text": None, "insert_anchor": None, "reason": text,
                "context_before": None, "context_after": None, "category": "clarity"}
    window.reviews._transport_factory = lambda _key: MockTransport(lambda *_: success([proposal]))
    dialog = show_dialog(window)
    dialog.page_mode.setCurrentIndex(1)
    dialog.first_page.setValue(page_index + 1)
    dialog.last_page.setValue(page_index + 1)
    click(dialog.start_button)
    assert report_for(window, session).report["application"]["applied"] == 1
    dialog.close()
    note, = (item for item in session.annotation_inventory.values() if item.kind == "sticky_note")
    assert note.provenance == "app" and note.author == "Revisione IA"
    assert note.native_name == note.logical_id
    assert session.dirty and session.saved_revision == 0

    def check_icon(current_view, summary):
        current_view.select_inventory_annotation(summary.logical_id, reveal=True)
        box = note_icon_rect(current_view.coordinate_mapper(page_index), summary.geometry.bbox)
        page = current_view.page_viewport_rect(page_index)
        assert page.adjusted(-.001, -.001, .001, .001).contains(box)
        # Sample the actual overlay loop with a transparent backing so a
        # cached PDF page cannot mask a missing standalone icon.
        layer = QImage(current_view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(QColor("transparent"))
        point = box.topRight() + QPointF(-5, 5)
        painter = QPainter(layer)
        current_view._paint_overlays(painter, page_index)
        painter.end()
        assert layer.pixelColor(point.toPoint()).alpha() > 0
        assert current_view._hit_test_inventory(point, page_index) == summary.logical_id
        QTest.mouseClick(current_view.viewport(), Qt.MouseButton.LeftButton, pos=point.toPoint())
        reader = current_view.sticky_note_reader
        wait_until(lambda: reader.isVisible() and reader.text.toPlainText() == text)
        assert reader.text.isReadOnly() and reader.author.text() == "Revisione IA"
        reader.dismiss()

    check_icon(view, note)
    view.set_zoom(1.5)
    check_icon(view, note)
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Delete)
    wait_until(lambda: note.logical_id not in session.annotation_inventory)
    window.undo_action.trigger()
    wait_until(lambda: note.logical_id in session.annotation_inventory)
    assert session.annotation_inventory[note.logical_id].native_name == note.native_name
    output = tmp_path / "note-saved.pdf"
    assert window._save_document(session.document_id, str(output))
    wait_until(lambda: not session.saving and session.document_generation == 2 and session.inventory_complete)
    check_icon(view, session.annotation_inventory[note.logical_id])
    assert not session.dirty and window.manager.request_close(session.document_id)
    wait_until(lambda: session.document_id not in window.manager.sessions)
    reopened = opened(window, output)
    wait_until(lambda: reopened.inventory_complete)
    persisted, = (item for item in reopened.annotation_inventory.values() if item.native_name == note.native_name)
    assert persisted.provenance == "external" and persisted.kind == "sticky_note" and persisted.subtype == "Text"
    new_view = window._widgets[reopened.document_id]
    new_view.set_zoom(.25)
    check_icon(new_view, persisted)


def test_monitor_save_as_tab_switch_partial_retry_and_undo(window, pdf_fixture_copy, tmp_path):
    source = pdf_fixture_copy(tmp_path / "review.pdf")
    origin = opened(window, source)
    other = opened(window, pdf_fixture_copy(tmp_path / "other.pdf"))
    window.manager.activate(origin.document_id)
    waiting, release = Event(), Event()
    starts = []

    def responses(first, last):
        starts.append(first)
        if first == 0:
            return success([_delete(0, "Prima riga")])
        if starts.count(first) == 1:
            waiting.set()
            assert release.wait(18)
            return classify_response(413, {}, {})
        return success([_delete(4, "testo ordinario")])

    transport = MockTransport(responses)
    window.reviews._transport_factory = lambda _key: transport
    dialog = show_dialog(window)
    dialog.model.setCurrentIndex(dialog.model.findData(SOL_MODEL))
    click(dialog.start_button)
    try:
        wait_until(waiting.is_set, timeout=20)
        wait_until(lambda: "Pagine analizzate 4/6" in dialog.progress.text())
        assert "Costo stimato" in dialog.progress.text() and "USD" in dialog.progress.text()
        assert not origin.annotations and not origin.dirty
        click(dialog.close_button)
        assert not dialog.isVisible() and window.reviews.busy(origin.document_id)
        window.manager.activate(other.document_id)
        assert window._save_document(origin.document_id, str(tmp_path / "save-as.pdf"))
        wait_until(lambda: origin.document_generation == 2 and not origin.saving)
        click(window.review_indicator)
        assert dialog.document_id == origin.document_id and dialog.isVisible()
    finally:
        release.set()
    entry = report_for(window, origin)
    assert entry.report["status"] == "partial"
    assert "parzialmente" in dialog.state.text() and "Pagine 5–6" in dialog.batches.item(0).text()
    assert origin.dirty and len(origin.annotations) == 1 and not other.annotations
    assert window.manager.active_id == other.document_id
    # The ordinary sidebar follows the active document and receives the existing
    # history/inventory signals once the origin tab is selected.
    window.manager.activate(origin.document_id)
    assert len(origin.annotation_inventory) == 2
    assert any(widget.count() for widget in window.annotation_sidebar.findChildren(QListWidget))
    sidebar = window.annotation_sidebar
    sidebar.show()
    root = origin.annotation_inventory[next(iter(origin.annotations))]
    assert root.author == "Revisione IA"
    sidebar.select_annotation(origin, root.logical_id)
    QTest.keyClick(sidebar.list, Qt.Key.Key_Return)
    assert sidebar.detail.parentWidget().findChild(QLabel, "annotation_author").text() == root.author
    window.undo_action.trigger()
    wait_until(lambda: not origin.dirty and not origin.annotations)
    window.redo_action.trigger()
    wait_until(lambda: origin.dirty and len(origin.annotations) == 1)
    dialog.model.setCurrentIndex(dialog.model.findData(TERRA_MODEL))
    dialog.batches.item(0).setCheckState(Qt.CheckState.Checked)
    assert dialog.retry_button.isEnabled()
    click(dialog.retry_button)
    wait_until(lambda: starts.count(4) == 2)
    wait_until(lambda: entry.report is not None and entry.report["status"] == "completed")
    assert entry.report["application"]["already_applied"] == 1
    assert entry.report["application"]["applied"] == 1
    assert starts == [0, 4, 4] and len(origin.annotations) == 2 and not other.annotations
    assert {body["model"] for _, _, body in transport.calls} == {SOL_MODEL}
    assert all(body["reasoning"] == {"effort": "medium"} for _, _, body in transport.calls)
    directory = entry.job.store.directory
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["configuration"]["model"] == SOL_MODEL
    assert manifest["configuration"]["rates_per_million"] == {
        "input": "2", "cached_input": "0.10", "cache_write": "2.50", "output": "10"}
    assert all(batch["model"] == SOL_MODEL for batch in manifest["roots"])
    for name in ("report.json", "application_report.json"):
        report = json.loads((directory / name).read_text(encoding="utf-8"))
        assert report["model"] == SOL_MODEL
    for folder in ("batches", "results", "attempts"):
        for path in (directory / folder).glob("*.json"):
            assert json.loads(path.read_text(encoding="utf-8"))["batch"]["model"] == SOL_MODEL
    rates = window.reviews._scheduler._rates
    assert {family for _, family in rates.models} == {SOL_MODEL}
    assert not dialog.retry_button.isVisible()


def test_rate_limit_and_stop_preserve_terminal_report(window, pdf_fixture_copy, tmp_path):
    session = opened(window, pdf_fixture_copy(tmp_path / "limited.pdf", pages=2))
    window.reviews._scheduler_factory = lambda: ReviewScheduler(tmp_path / "jobs")
    transport = MockTransport(lambda *_: classify_response(429, {}, {"retry-after": "30"}))
    window.reviews._transport_factory = lambda _key: transport
    dialog = show_dialog(window)
    click(dialog.start_button)
    wait_until(lambda: "attesa per limiti API" in dialog.progress.text(), timeout=20)
    assert "non disponibile" in dialog.progress.text()
    click(dialog.stop_button)
    entry = report_for(window, session)
    assert entry.report["status"] == "interrupted" and len(transport.calls) == 1
    assert not session.dirty


def test_monitor_reports_application_phase(window, pdf_fixture_copy, tmp_path, monkeypatch):
    session = opened(window, pdf_fixture_copy(tmp_path / "applying.pdf", pages=2))
    entered, release = Event(), Event()
    confirm = ReviewWorker.confirm

    async def hold(worker, data, stopping):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await confirm(worker, data, stopping)

    monkeypatch.setattr(ReviewWorker, "confirm", hold)
    window.reviews._transport_factory = lambda _key: MockTransport(
        lambda *_: success([_delete(0, "Prima riga")]))
    dialog = show_dialog(window)
    click(dialog.start_button)
    try:
        wait_until(entered.is_set, timeout=20)
        wait_until(lambda: "Applicazione delle annotazioni" in dialog.state.text())
        assert "Pagine analizzate 2/2" in dialog.progress.text() and not session.dirty
    finally:
        release.set()
    assert report_for(window, session).report["application"]["applied"] == 1
    assert session.dirty


def test_shutdown_waits_for_vault_without_blocking_qt(window, pdf_fixture_copy, tmp_path):
    opened(window, pdf_fixture_copy(tmp_path / "vault-read.pdf"))
    entered, release = Event(), Event()

    class SlowVault(FakeVault):
        def read(self):
            entered.set()
            assert release.wait(12)
            return super().read()

    window.reviews.credential_store = SlowVault(DUMMY)
    window.review_action.trigger()
    try:
        wait_until(entered.is_set)
        window.close()
        wait_until(lambda: window.manager.is_shutdown)
        assert not window.reviews.is_shutdown and window.isVisible()
    finally:
        release.set()
    wait_until(lambda: window.reviews.is_shutdown and not window.isVisible())
    assert window.reviews._scheduler is None


def _answer_close(app, answer, seen):
    def choose():
        box = app.activeModalWidget()
        if not isinstance(box, QMessageBox):
            QTimer.singleShot(5, choose)
            return
        seen.append(box.text())
        click(box.button(answer))
    QTimer.singleShot(5, choose)


@pytest.mark.parametrize("close_app", [False, True])
def test_close_confirmation_drains_late_response_without_application(window, app, pdf_fixture_copy,
                                                                      tmp_path, close_app):
    source = pdf_fixture_copy(tmp_path / "closing.pdf", pages=2)
    original = source.read_bytes()
    session = opened(window, source)
    entered, release = Event(), Event()

    def post(*_):
        entered.set()
        assert release.wait(16)
        return success([_delete(0, "Prima riga")])

    window.reviews._transport_factory = lambda _key: MockTransport(post)
    dialog = show_dialog(window)
    click(dialog.start_button)
    try:
        wait_until(entered.is_set, timeout=20)
        job = window.reviews.entries[session.document_id].job
        workspace = Path(job.snapshot.path).parent
        seen = []
        _answer_close(app, QMessageBox.StandardButton.Cancel, seen)
        close = window.close if close_app else lambda: window._tab_closed(window.tabs.currentIndex())
        close()
        assert window.reviews.busy(session.document_id) and not window.manager.shutting_down
        _answer_close(app, QMessageBox.StandardButton.Close, seen)
        close()
        assert len(seen) == 2 and all("interromperà la revisione" in message for message in seen)
        assert not job.future.done()
        if close_app:
            assert window.manager.shutting_down and not window.reviews.is_shutdown
    finally:
        release.set()
    wait_until(job.future.done, timeout=20)
    assert job.future.result()["status"] == "interrupted"
    assert job.future.result()["application"]["applied"] == 0 and source.read_bytes() == original
    wait_until(lambda: session.document_id not in window.manager.sessions)
    assert not workspace.exists()
    if close_app:
        wait_until(lambda: not window.isVisible() and window.reviews.is_shutdown)


def test_worker_loss_and_credential_replacement_stop_new_work(window, pdf_fixture_copy, tmp_path):
    session = opened(window, pdf_fixture_copy(tmp_path / "retired.pdf"))
    entered, release = Event(), Event()
    transport = MockTransport(lambda *_: (entered.set(), release.wait(16), success())[2])
    window.reviews._transport_factory = lambda _key: transport
    dialog = show_dialog(window)
    click(dialog.start_button)
    try:
        wait_until(entered.is_set, timeout=20)
        click(dialog.credentials_button)
        dialog.key.setText(DUMMY + "-rotation")
        click(dialog.store_key)
        wait_until(lambda: not window.reviews.credential_busy and "Interruzione" in dialog.state.text())
        session.worker.failure = ErrorInfo("TEST_WORKER_LOST", "Worker unavailable")
        wait_until(lambda: session.lifecycle == Lifecycle.FAILED)
    finally:
        release.set()
    result = report_for(window, session).report
    assert result["status"] == "interrupted" and result["application"]["applied"] == 0
    assert len(transport.calls) == 1 and not dialog.retry_button.isVisible()
