from hashlib import sha256
import multiprocessing as mp
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.document_session import Lifecycle
from pdf_annotator.app.file_resolver import MAX_PENDING_OPENS
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CommandContext, CreateInk
from pdf_annotator.domain.geometry import PdfPoint
from pdf_annotator.domain.styles import InkStyle
from pdf_annotator.domain.targets import InkTarget
from pdf_annotator.util.paths import identify_file
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import MAX_GEOMETRY_PAGES, Operation
from .conftest import request, wait_until
from .m1_fixtures import audited_worker, delayed_worker


@pytest.fixture(scope="module")
def pdf_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp("m1-pdfs")
    # Test setup only: separate owner generates each fixture then closes it. No
    # PyMuPDF import or file generation in the application/Qt process.
    subprocess.run([sys.executable, "-m", "tests.m1_fixtures", str(directory)],
                   check=True, timeout=30, capture_output=True)
    return directory


@pytest.fixture
def manager(app):
    instance = DocumentManager()
    yield instance
    # Lifecycle assertions drive dirty state explicitly; teardown always uses
    # the test-safe discard path instead of forging a clean revision.
    instance.request_shutdown(discard_changes=True)
    wait_until(lambda: instance.is_shutdown)
    assert instance.budget.bytes == 0
    assert not instance.sessions


def opened(manager, path):
    assert manager.open_path(str(path)) is not None
    identity = identify_file(str(path))
    wait_until(lambda: any(s.identity.file_key == identity.file_key and s.lifecycle == Lifecycle.READY
                           for s in manager.sessions.values()))
    return next(s for s in manager.sessions.values() if s.identity.file_key == identity.file_key)


def make_history_dirty(manager, session):
    """Create a confirmed mutation instead of fabricating a revision number."""
    command = CreateInk(
        CommandContext.create(session.document_id, session.document_generation),
        InkTarget(0, ((PdfPoint(12, 18), PdfPoint(48, 36)),)),
        InkStyle(),
    )
    assert manager.submit_annotation(command).request_id is not None
    wait_until(lambda: session.pending_mutations == 0 and session.history_state_id != 0)
    assert session.dirty
    return command


def test_active_session_ticked_first_with_bounded_background_fairness(manager):
    from types import SimpleNamespace

    seen = []
    manager.session_tick.connect(seen.append)
    for index in range(12):
        document_id = str(index)
        manager.sessions[document_id] = SimpleNamespace(
            document_id=document_id, lifecycle=Lifecycle.READY,
            worker=SimpleNamespace(failure=None))
    manager.active_id = "11"
    try:
        manager._poll()
        assert len(seen) == 8 and seen[0] == "11"
        manager._poll()
        assert len(seen) == 16 and seen[8] == "11"
        assert set(seen) == set(manager.sessions)
    finally:
        manager.sessions.clear()
        manager.active_id = None


@pytest.fixture
def window(app):
    instance = MainWindow()
    instance.show()
    yield instance
    for session in instance.manager.sessions.values():
        session.pending_mutations = 0
        session.confirmed_revision = session.saved_revision
    instance.close()
    wait_until(lambda: instance.manager.is_shutdown)
    assert not instance.isVisible()


@pytest.mark.parametrize("name,count", [("normal", 5), ("long", 300), ("geometry", 8)])
def test_metadata_only_open_and_canonical_geometry(pdf_files, manager, monkeypatch, name, count):
    monkeypatch.setattr(document_service, "document_worker_main", audited_worker)
    source = pdf_files / f"{name}.pdf"
    original = sha256(source.read_bytes()).digest()
    session = opened(manager, source)
    assert session.page_count == count and len(session.pages) == count
    assert session.document_generation == 1 and not session.dirty
    assert session.canonical_path == str(source.resolve())
    assert session.identity.size == source.stat().st_size
    assert session.identity.mtime_ns == source.stat().st_mtime_ns
    assert session.worker.submitted_counts[Operation.RENDER] == 0
    assert session.worker.submitted_counts[Operation.OPEN_DOCUMENT] == 1
    assert session.worker.submitted_counts[Operation.PAGE_GEOMETRY] == (count + MAX_GEOMETRY_PAGES - 1) // MAX_GEOMETRY_PAGES
    assert sha256(source.read_bytes()).digest() == original
    assert "pymupdf" not in sys.modules
    if name == "geometry":
        for index, page in enumerate(session.pages):
            assert page.rotation == index % 4 * 90
            assert page.mediabox == (0, 0, 600, 800)
            assert page.cropbox == ((50, 70, 550, 730) if index >= 4 else (0, 0, 600, 800))
            assert (page.width_pt, page.height_pt) == ((500, 660) if index >= 4 else (600, 800))


@pytest.mark.parametrize("name,code", [("missing.pdf", "NOT_FOUND"), ("not_pdf.txt", "NOT_PDF"),
                                     ("invalid.pdf", "INVALID_PDF"), ("image.pdf", "NOT_PDF"),
                                     ("repaired.pdf", "INVALID_PDF"), ("encrypted.pdf", "ENCRYPTED"),
                                     ("owner_only.pdf", "ENCRYPTED")])
def test_open_error_has_no_ghost_session_or_worker(pdf_files, manager, name, code):
    errors = []
    manager.error_occurred.connect(lambda doc, error: errors.append(error))
    assert manager.open_path(str(pdf_files / name))
    wait_until(lambda: errors and not manager.sessions and not manager.pending_open_count)
    assert errors[0].code == code
    assert "Traceback" not in errors[0].message
    assert not manager._by_file and not manager._by_path
    assert not [p for p in mp.active_children() if p.name.startswith("pdf-")]
    # A failed open must not poison subsequent valid opens.
    assert opened(manager, pdf_files / "normal.pdf").page_count == 5


def test_concurrent_duplicates_case_dotdot_and_hardlink(pdf_files, manager, tmp_path):
    source = pdf_files / "normal.pdf"
    alias = tmp_path / "alias.pdf"
    os.link(source, alias)
    intermediate = tmp_path / "folder"
    intermediate.mkdir()
    variants = [str(source), str(source.parent) + "/./normal.pdf",
                str(intermediate / ".." / "alias.pdf"), str(alias)]
    if sys.platform == "win32":
        variants.append(str(source).upper())
    for path in variants:
        assert manager.open_path(path)
    wait_until(lambda: not manager.pending_open_count and len(manager.sessions) == 1
               and next(iter(manager.sessions.values())).lifecycle == Lifecycle.READY)
    session = next(iter(manager.sessions.values()))
    assert manager.workers_started == 1
    pid = session.worker.pid
    other = opened(manager, pdf_files / "alternative.pdf")
    assert manager.active_id == other.document_id
    manager.open_path(str(alias))
    wait_until(lambda: manager.active_id == session.document_id)
    assert manager.workers_started == 2 and session.worker.pid == pid
    assert len(manager.sessions) == 2


def test_three_sessions_state_switch_and_middle_close(pdf_files, window):
    manager = window.manager
    sessions = [opened(manager, pdf_files / f"{name}.pdf") for name in ("normal", "alternative", "third")]
    a, b, c = sessions
    workers = [s.worker for s in sessions]
    assert len({w.pid for w in workers}) == 3
    assert len({s.document_id for s in sessions}) == 3
    a.ui.reading_anchor.page_index = 2
    a.ui.zoom = 2.0
    a.ui.layout = "double"
    a.ui.search.query = "future"
    a.ui.sidebar_expanded = False
    a.pending_mutations = 1
    manager.notify_state_changed(a.document_id)
    assert window.tabs.tabText(0).endswith(" *")
    assert b.ui.reading_anchor.page_index == 0 and b.ui.zoom == 1.0
    assert b.ui.layout == "single" and b.ui.search.query == "" and b.ui.sidebar_expanded
    assert not b.dirty and not c.dirty
    for index in (0, 2, 1, 0, 2):
        window.tabs.setCurrentIndex(index)
        assert manager.active_id == sessions[index].document_id
    assert [s.worker for s in sessions] == workers
    assert all(w.submitted_counts[Operation.OPEN_DOCUMENT] == 1 for w in workers)
    a.pending_mutations = 0
    window.tabs.tabCloseRequested.emit(1)
    assert window.tabs.count() == 2
    wait_until(lambda: b.document_id not in manager.sessions)
    assert b.lifecycle == Lifecycle.CLOSED and b.pages == [] and workers[1].stopped.is_set()
    assert not workers[0].stopped.is_set() and not workers[2].stopped.is_set()
    assert a.ui.zoom == 2.0 and a.ui.reading_anchor.page_index == 2
    assert window.tabs.currentWidget().property("document_id") == c.document_id


def test_dirty_guard_vetoes_tab_and_app_close_without_fake_save(pdf_files, manager):
    a = opened(manager, pdf_files / "normal.pdf")
    b = opened(manager, pdf_files / "alternative.pdf")
    errors = []
    manager.error_occurred.connect(lambda doc, error: errors.append((doc, error.code)))
    make_history_dirty(manager, a)
    assert a.dirty
    assert not manager.request_close(a.document_id)
    assert not manager.request_shutdown()
    assert not manager.shutting_down
    assert all(s.lifecycle == Lifecycle.READY for s in (a, b))
    assert a.saved_revision == 0 and a.saved_history_state_id == 0
    assert errors[-1] == (a.document_id, "DIRTY_CLOSE_BLOCKED")


def test_dirty_dialog_offers_save_discard_and_cancel(pdf_files, window):
    observed = {}
    session = opened(window.manager, pdf_files / "normal.pdf")

    def inspect_dialog():
        dialog = next((widget for widget in QApplication.topLevelWidgets()
                       if isinstance(widget, QMessageBox) and widget.isVisible()), None)
        if dialog is None:
            QTimer.singleShot(1, inspect_dialog)
            return
        observed["text"] = dialog.text()
        observed["buttons"] = dialog.standardButtons()
        dialog.button(QMessageBox.StandardButton.Cancel).click()

    QTimer.singleShot(0, inspect_dialog)
    assert window._close_choice(session) == QMessageBox.StandardButton.Cancel
    assert "normal.pdf" in observed["text"]
    assert observed["buttons"] == (QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                    | QMessageBox.StandardButton.Cancel)


def test_dirty_tab_cancel_then_explicit_discard_keeps_source_byte_identical(
        pdf_files, window, monkeypatch):
    source = pdf_files / "normal.pdf"
    original = sha256(source.read_bytes()).digest()
    session = opened(window.manager, source)
    worker = session.worker
    make_history_dirty(window.manager, session)
    decisions = iter((QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard))
    monkeypatch.setattr(window, "_close_choice", lambda session: next(decisions))

    index = window.tabs.indexOf(window._widgets[session.document_id])
    window.tabs.tabCloseRequested.emit(index)
    assert session.document_id in window.manager.sessions
    assert session.lifecycle == Lifecycle.READY
    assert session.history_state_id != session.saved_history_state_id
    assert session.document_revision == 1 and session.saved_revision == 0

    window.tabs.tabCloseRequested.emit(index)
    wait_until(lambda: session.document_id not in window.manager.sessions)
    assert worker.stopped.is_set()
    assert session.annotations == {} and session.pages == []
    assert sha256(source.read_bytes()).digest() == original


def test_application_close_resolves_each_dirty_document(
        pdf_files, window, monkeypatch):
    sources = (pdf_files / "normal.pdf", pdf_files / "alternative.pdf")
    originals = {path: sha256(path.read_bytes()).digest() for path in sources}
    sessions = [opened(window.manager, path) for path in sources]
    workers = [session.worker for session in sessions]
    make_history_dirty(window.manager, sessions[0])
    calls = []
    monkeypatch.setattr(window, "_close_choice",
                        lambda session: calls.append(session.document_id) or QMessageBox.StandardButton.Cancel)

    window.close()
    assert calls == [sessions[0].document_id]
    assert window.isVisible() and not window.manager.shutting_down
    assert [session.history_state_id for session in sessions] == [1, 0]

    make_history_dirty(window.manager, sessions[1])
    monkeypatch.setattr(window, "_close_choice",
                        lambda session: calls.append(session.document_id) or QMessageBox.StandardButton.Discard)
    window.close()
    wait_until(lambda: window.manager.is_shutdown)
    assert calls == [sessions[0].document_id, sessions[0].document_id, sessions[1].document_id]
    assert all(worker.stopped.is_set() for worker in workers)
    assert not [child for child in mp.active_children()
                if child.pid in {worker.pid for worker in workers}]
    assert all(sha256(path.read_bytes()).digest() == originals[path]
               for path in sources)


def test_worker_failure_isolated_visible_and_discard_closable(
        pdf_files, window, monkeypatch):
    a = opened(window.manager, pdf_files / "normal.pdf")
    b = opened(window.manager, pdf_files / "alternative.pdf")
    worker_a, worker_b = a.worker, b.worker
    beats = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(1))
    timer.start()
    os.kill(worker_a.pid, signal.SIGTERM)
    wait_until(lambda: a.lifecycle == Lifecycle.FAILED)
    timer.stop()
    assert beats and worker_a.failure is not None
    assert "interrotto" in window._widgets[a.document_id].error_message
    assert "Traceback" not in window.statusBar().currentMessage()
    assert b.lifecycle == Lifecycle.READY and not worker_b.stopped.is_set()
    window.manager.open_path(b.canonical_path)
    wait_until(lambda: not window.manager.pending_open_count)
    assert window.manager.active_id == b.document_id
    a.confirmed_revision = 1
    monkeypatch.setattr(window, "_close_choice", lambda session: QMessageBox.StandardButton.Discard)
    index = window.tabs.indexOf(window._widgets[a.document_id])
    window.tabs.tabCloseRequested.emit(index)
    wait_until(lambda: a.document_id not in window.manager.sessions)
    assert worker_a.stopped.is_set()
    assert b.worker is worker_b


def test_close_during_open_rejects_duplicate_until_worker_stops(pdf_files, manager, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", delayed_worker)
    source = pdf_files / "normal.pdf"
    manager.open_path(str(source))
    wait_until(lambda: len(manager.sessions) == 1)
    session = next(iter(manager.sessions.values()))
    worker = session.worker
    errors = []
    manager.error_occurred.connect(lambda doc, error: errors.append(error.code))
    assert manager.request_close(session.document_id)
    manager.open_path(str(source))
    wait_until(lambda: not manager.pending_open_count)
    assert manager.workers_started == 1
    assert "CLOSING" in errors
    wait_until(lambda: not manager.sessions)
    assert worker.stopped.is_set() and session.lifecycle == Lifecycle.CLOSED
    reopened = opened(manager, source)
    assert reopened.document_id != session.document_id


def test_application_close_reaps_all_owners_including_failed_one(pdf_files, window):
    sessions = [opened(window.manager, pdf_files / f"{name}.pdf") for name in ("normal", "alternative", "third")]
    workers = [s.worker for s in sessions]
    os.kill(workers[1].pid, signal.SIGTERM)
    wait_until(lambda: sessions[1].lifecycle == Lifecycle.FAILED)
    window.close()
    wait_until(lambda: window.manager.is_shutdown)
    assert all(w.stopped.is_set() and w.pending_count == 0 for w in workers)
    assert all(s.lifecycle == Lifecycle.CLOSED for s in sessions)
    assert not [p for p in mp.active_children() if p.pid in {w.pid for w in workers}]
    assert not window.isVisible()


def test_open_action_dialog_and_failed_open_leave_no_tab(pdf_files, window):
    window.open_action.trigger()
    assert window.file_dialog is not None
    window.file_dialog.filesSelected.emit([str(pdf_files / "normal.pdf")])
    window.file_dialog.reject()
    wait_until(lambda: window.tabs.count() == 1)
    assert window.tabs.tabText(0) == "normal.pdf"
    assert "5 pagine" in window.page_count_label.text()
    wait_until(lambda: 0 in window.tabs.widget(0).scheduler.rasters)
    window.open_paths([str(pdf_files / "invalid.pdf")])
    wait_until(lambda: "danneggiato" in window.statusBar().currentMessage()
               and len(window.manager.sessions) == 1)
    assert window.tabs.count() == 1
    assert 0 in window.tabs.widget(0).scheduler.rasters


def test_m0_mutation_probe_cannot_modify_user_document(pdf_files, manager):
    source = pdf_files / "normal.pdf"
    original = sha256(source.read_bytes()).digest()
    session = opened(manager, source)
    response = request(session.worker, Operation.ANNOTATION_SPIKE)
    assert not response.success
    assert sha256(source.read_bytes()).digest() == original
    assert not session.dirty


def test_file_resolution_never_runs_on_gui_thread_and_cancels_on_app_close(pdf_files, manager, monkeypatch):
    from pdf_annotator.app import file_resolver

    original = file_resolver.identify_file
    entered, release = threading.Event(), threading.Event()
    seen = []

    def delayed(path):
        seen.append(threading.current_thread() is threading.main_thread())
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(file_resolver, "identify_file", delayed)
    manager.open_path(str(pdf_files / "normal.pdf"))
    wait_until(entered.is_set)
    for _ in range(MAX_PENDING_OPENS - 1):
        assert manager.open_path(str(pdf_files / "normal.pdf"))
    assert manager.open_path(str(pdf_files / "normal.pdf")) is None
    assert manager.pending_open_count == MAX_PENDING_OPENS
    assert manager.request_shutdown()
    release.set()
    wait_until(lambda: manager.is_shutdown)
    assert seen == [False] and manager.workers_started == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows exclusive file handle")
def test_unreadable_file_error_and_cleanup(pdf_files, manager):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    source = pdf_files / "normal.pdf"
    handle = kernel.CreateFileW(str(source), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    errors = []
    manager.error_occurred.connect(lambda doc, error: errors.append(error))
    try:
        manager.open_path(str(source))
        wait_until(lambda: errors and not manager.sessions and not manager.pending_open_count)
        assert errors[0].code == "UNREADABLE"
    finally:
        assert kernel.CloseHandle(handle)


def test_file_changed_between_resolution_and_worker_open_is_rejected(pdf_files, manager, tmp_path, monkeypatch):
    from pdf_annotator.app import file_resolver

    source = tmp_path / "changing.pdf"
    shutil.copyfile(pdf_files / "normal.pdf", source)
    errors = []
    manager.error_occurred.connect(lambda doc, error: errors.append(error))

    def changing(path):
        identity = identify_file(path)
        with open(path, "ab") as output:
            output.write(b"\n")
        return identity

    monkeypatch.setattr(file_resolver, "identify_file", changing)
    manager.open_path(str(source))
    wait_until(lambda: errors and not manager.sessions and not manager.pending_open_count)
    assert errors[0].code == "SOURCE_CHANGED"


def test_transport_initialization_failure_is_cleaned_up(pdf_files, manager, monkeypatch):
    from types import SimpleNamespace
    from pdf_annotator.worker import client

    def failing_pipe(**kwargs):
        raise OSError("Injected pipe creation failure")

    monkeypatch.setattr(client.mp, "get_context", lambda method: SimpleNamespace(Pipe=failing_pipe))
    errors = []
    manager.error_occurred.connect(lambda doc, error: errors.append(error))
    manager.open_path(str(pdf_files / "normal.pdf"))
    wait_until(lambda: errors and not manager.sessions and not manager.pending_open_count)
    assert errors[0].code == "WORKER_FAILED"
    assert not manager._by_file and not manager._by_path


def test_normal_entrypoint_opens_two_tabs_and_exits_without_children(pdf_files):
    result = subprocess.run([sys.executable, "-m", "tests.m1_fixtures", "--smoke", str(pdf_files)],
                            timeout=25, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "M1_ENTRYPOINT_PASS workers=2 rendered_tabs=2 children_after_close=0" in result.stdout
