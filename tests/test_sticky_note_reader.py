"""Sticky Note reading through the real inventory/worker, without GUI PDF access."""

from dataclasses import replace
from math import ceil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QImage, QKeySequence, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QToolButton, QWidget

from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CommandContext, Undo
from pdf_annotator.domain.geometry import PdfRect
from pdf_annotator.domain.styles import FreeTextStyle
from pdf_annotator.domain.targets import RectTarget
from pdf_annotator.viewer.sticky_notes import note_icon_rect
from pdf_annotator.worker.protocol import ErrorInfo, Operation
from tests.conftest import close_window_discard, wait_until
from tests.test_save_m15 import inspect, ink, save, submit

PLAIN = "<b>città 日本語 😀</b>\nseconda riga & tè"
LONG = "Testo lungo: città 日本語 😀 & <ok>\n" * 2400


@pytest.fixture(scope="module")
def note_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("sticky-notes") / "notes.pdf"
    subprocess.run([sys.executable, "-m", "tests.m12_fixtures", "sticky-notes", str(path)],
                   check=True, capture_output=True, timeout=30)
    return path


@pytest.fixture
def opened_notes(app, note_pdf, tmp_path):
    path = tmp_path / "notes.pdf"
    path.write_bytes(note_pdf.read_bytes())
    window = MainWindow()
    window.resize(1050, 800)
    window.show()
    window.open_paths([str(path)])
    wait_until(lambda: bool(window.manager.sessions))
    session = next(iter(window.manager.sessions.values()))
    wait_until(lambda: session.inventory_complete)
    view = window._widgets[session.document_id]
    yield window, session, view, path
    close_window_discard(window)


def note(session, text=PLAIN, page=0):
    return next(item for item in session.annotation_inventory.values()
                if item.kind == "sticky_note" and item.page_index == page
                and item.preview == (text if len(text) <= 240 else text[:240] + "…"))


def click_note(view, summary):
    point = note_icon_rect(view.coordinate_mapper(summary.page_index), summary.geometry.bbox).center()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point.toPoint())
    return view.sticky_note_reader


def test_note_with_reply_keeps_parent_reader_and_groups_child_in_sidebar(opened_notes):
    window, session, view, _path = opened_notes
    parent = note(session, "Nota con risposta")
    child = note(session, "Risposta già inventariata")
    assert child.reply.parent_logical_id == parent.logical_id
    reader = click_note(view, parent)
    wait_until(lambda: reader.text.toPlainText() == "Nota con risposta")
    wait_until(lambda: "Risposta già inventariata" in window.annotation_sidebar.detail.toPlainText())
    assert window.annotation_sidebar.detail.toPlainText().startswith("Contenuto annotazione\n")
    assert reader.logical_id == parent.logical_id
    ids = [window.annotation_sidebar.list.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(window.annotation_sidebar.list.count())]
    assert parent.logical_id in ids and child.logical_id not in ids


def test_sidebar_cards_wrap_expand_copy_navigate_and_collapse(opened_notes):
    window, session, view, _path = opened_notes
    sidebar = window.annotation_sidebar
    sidebar.show()
    rows = {sidebar.list.item(i).data(Qt.ItemDataRole.UserRole): sidebar.list.item(i)
            for i in range(sidebar.list.count())}
    short = rows[note(session).logical_id]
    target = note(session, LONG)
    long = rows[target.logical_id]
    sidebar.list.scrollToItem(long, sidebar.list.ScrollHint.PositionAtTop)
    compressed = sidebar.list.visualItemRect(long)
    assert 50 <= compressed.height() <= 160
    assert sidebar.list.visualItemRect(short).height() < compressed.height()
    activated = []
    sidebar.annotation_activated.connect(activated.append)
    QTest.mouseClick(sidebar.list.viewport(), Qt.MouseButton.LeftButton, pos=compressed.center())
    wait_until(lambda: sidebar.detail.toPlainText() == LONG and sidebar.detail.isVisible())
    assert view.selected_inventory_id == target.logical_id
    expanded = sidebar.list.visualItemRect(long)
    assert expanded.height() == 360 and expanded.top() == compressed.top()
    wait_until(lambda: sidebar.detail.verticalScrollBar().maximum() > 0)
    card = sidebar.list.itemWidget(long)
    assert target.author is None and card.findChild(QLabel, "annotation_author") is None
    card.findChild(QPushButton).click()
    assert activated == [target.logical_id, target.logical_id]
    sidebar.detail.selectAll()
    QTest.keySequence(sidebar.detail, QKeySequence.StandardKey.Copy)
    assert QApplication.clipboard().text() == LONG
    card.findChild(QToolButton).click()
    assert not sidebar.detail.isVisible()
    assert sidebar.list.visualItemRect(long).height() == compressed.height()
    assert view.selected_inventory_id == target.logical_id


def test_short_expanded_sidebar_card_fits_its_wrapped_document(opened_notes):
    window, session, view, _path = opened_notes
    sidebar = window.annotation_sidebar
    sidebar.show()
    window.resizeDocks([sidebar], [150], Qt.Orientation.Horizontal)
    view.select_inventory_annotation(note(session).logical_id, reveal=True)
    assert note(session).author == "<Autore>"
    wait_until(lambda: sidebar.detail.toPlainText() == PLAIN)
    row = sidebar.list.currentItem()
    for font_size in (12, 16):
        font = sidebar.detail.font()
        font.setPointSize(font_size)
        sidebar.detail.setFont(font)
        QTest.keyClick(sidebar.list, Qt.Key.Key_Return)
        editor = sidebar.detail
        QTest.qWait(120)
        assert editor.isVisible()
        author = sidebar.list.itemWidget(row).findChild(QLabel, "annotation_author")
        assert author.text() == "<Autore>" and author.textFormat() == Qt.TextFormat.PlainText
        assert author.geometry().bottom() < editor.geometry().top()
        geometry = (
            editor.size(), editor.viewport().size(), editor.document().size(), sidebar.list.visualItemRect(row))
        assert editor.verticalScrollBar().maximum() == 0, geometry
        assert 0 <= editor.viewport().height() - editor.document().size().height() <= 1, geometry
        assert 0 < sidebar.list.visualItemRect(row).height() < 360
        editor.selectAll()
        QTest.keySequence(editor, QKeySequence.StandardKey.Copy)
        assert QApplication.clipboard().text() == PLAIN
        sidebar.list.itemWidget(row).findChild(QToolButton).click()
        assert not editor.isVisible()


def test_collapsed_sidebar_layout_only_draws_five_positioned_lines(app):
    from pdf_annotator.app.annotation_sidebar import _wrapped

    font = app.font()
    text = ("città 日本語 😀 parole molto lunghe\n" * 150)
    for width in (90, 145):
        for font_size in (12, 16):
            font.setPointSize(font_size)
            layout, height, more = _wrapped(text, font, width)
            assert more and layout.lineCount() == 5
            lines = [layout.lineAt(index) for index in range(layout.lineCount())]
            assert lines[0].y() == 0
            assert all(left.y() + left.height() <= right.y() for left, right in zip(lines, lines[1:]))
            assert all(line.naturalTextWidth() <= width for line in lines)
            assert 0 <= height - lines[-1].y() - lines[-1].height() < 1
            short, short_height, more = _wrapped("nota\nbreve", font, width)
            assert not more and short.lineCount() == 2 and short_height < height


def test_sidebar_viewport_composites_only_against_its_dock_backdrop(app):
    from pdf_annotator.app.annotation_sidebar import AnnotationSidebar
    from pdf_annotator.app.ui_chrome import application_stylesheet

    window = QMainWindow()
    window.setObjectName("dockProbe")
    window.setStyleSheet(application_stylesheet() + "QMainWindow#dockProbe { background: #2850a0; }")
    document = QWidget(window)
    document.setStyleSheet("background: #dead42;")
    window.setCentralWidget(document)
    dock = AnnotationSidebar(window)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    window.resize(600, 380)
    window.show()
    try:
        app.processEvents()
        image = window.grab().toImage()
        dpr = image.devicePixelRatio()

        def pixel(widget):
            point = widget.mapTo(window, widget.rect().center())
            return image.pixelColor(round(point.x() * dpr), round(point.y() * dpr)).getRgb()[:3]

        assert pixel(document) == (222, 173, 66)
        expected = tuple(round((238 * front + 17 * back) / 255)
                         for front, back in zip((37, 39, 42), (40, 80, 160)))
        assert pixel(dock.list.viewport()) == pytest.approx(expected, abs=1)
    finally:
        window.close()


def test_sidebar_author_adds_a_separate_line_and_joins_use_one_physical_pixel(app):
    from pdf_annotator.app.annotation_sidebar import AnnotationSidebar, _author_font, _wrapped
    from pdf_annotator.app.ui_chrome import COLORS, application_stylesheet

    dock = AnnotationSidebar()
    dock.setStyleSheet(application_stylesheet())
    dock.resize(220, 720)
    rows = [dock._make_item(SimpleNamespace(kind="replace_text", subtype="Caret", page_index=0, logical_id=str(index),
                                          author=author, preview="Correzione lunga\n" * 12))
            for index, author in enumerate(("Revisione IA", None, "  "))]
    for row in rows:
        dock.list.addItem(row)
    dock.show()
    try:
        app.processEvents()
        viewport = dock.list.viewport()
        font = dock.list.font()
        plain, authored = dock.list.visualItemRect(rows[1]), dock.list.visualItemRect(rows[0])
        assert authored.height() - plain.height() == QFontMetrics(_author_font(font)).height()
        assert dock.list.visualItemRect(rows[2]).height() == plain.height()
        layout, text_height, more = _wrapped("Correzione lunga\n" * 12, font, viewport.width() - 16)
        assert layout.lineCount() == 5 and more
        assert authored.height() >= text_height + QFontMetrics(font).height() + QFontMetrics(_author_font(font)).height()
        preview = viewport.grab().toImage()
        scale = preview.devicePixelRatio()
        author_top = authored.top() + 9 + QFontMetrics(font).height()
        author_bottom = author_top + QFontMetrics(_author_font(font)).height()
        secondary = QColor(COLORS.secondary).getRgb()[:3]
        assert any(preview.pixelColor(x, y).getRgb()[:3] == pytest.approx(secondary, abs=8)
                   for y in range(ceil(author_top * scale), int(author_bottom * scale))
                   for x in range(ceil(8 * scale), int(100 * scale)))

        dock.list.setCurrentItem(rows[0])
        for expand in (False, True, False):
            if bool(dock.list.itemWidget(rows[0])) != expand:
                QTest.keyClick(dock.list, Qt.Key.Key_Return)
            app.processEvents()
            image = viewport.grab().toImage()
            dpr = image.devicePixelRatio()
            x = image.width() // 2
            edges = [ceil((rect.y() + rect.height()) * dpr) - 1
                     for rect in (dock.list.visualItemRect(row) for row in rows)]
            base = image.pixelColor(x, edges[-1] + 3).getRgb()[:3]
            primary = QColor(COLORS.primary).getRgb()[:3]
            color = tuple(round((26 * front + 229 * back) / 255) for front, back in zip(primary, base))

            def separator_at(col, y):
                return image.pixelColor(col, y).getRgb()[:3] == pytest.approx(color, abs=2)

            for y in edges[:-1]:
                assert sum(separator_at(x, y + delta) for delta in range(-2, 3)) == 1
                assert not separator_at(2, y)
            assert not any(separator_at(x, edges[-1] + delta) for delta in range(-2, 3))
            assert dock.list.currentItem() is rows[0]
    finally:
        dock.close()


@pytest.mark.parametrize("text,author", [(PLAIN, "<Autore>"), ("", None),
                                        (LONG, None)],
                         ids=["unicode", "empty", "multichunk"])
def test_single_click_full_plain_readonly_copy_and_metadata(opened_notes, text, author):
    window, session, view, path = opened_notes
    original = path.read_bytes()
    summary = note(session, text)
    before = (session.document_revision, session.history_length, session.dirty)
    commands = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
    details = session.worker.submitted_counts[Operation.ANNOTATION_DETAIL]
    reader = click_note(view, summary)
    assert reader.isVisible() and reader.logical_id == summary.logical_id
    wait_until(lambda: reader.text.toPlainText() == text)
    assert reader.author.isVisible() == bool(author)
    assert reader.author.text() == (author or "")
    assert reader.text.isReadOnly()
    QApplication.clipboard().clear()
    reader.text.selectAll()
    QTest.keySequence(reader.text, QKeySequence.StandardKey.Copy)
    assert QApplication.clipboard().text() == text
    QTest.keyClicks(reader.text, "should not be inserted")
    QTest.keyClick(reader.text, Qt.Key.Key_Delete)
    QTest.keyClick(reader.text, Qt.Key.Key_Backspace)
    assert reader.text.toPlainText() == text
    assert summary.logical_id in session.annotation_inventory
    assert (session.document_revision, session.history_length, session.dirty) == before
    assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == commands
    assert path.read_bytes() == original
    if text == LONG:
        assert session.worker.submitted_counts[Operation.ANNOTATION_DETAIL] - details > 1
        assert reader.text.verticalScrollBar().maximum() > 0
    assert reader.width() <= 360 and reader.height() <= 280


@pytest.mark.parametrize("page", range(4))
@pytest.mark.parametrize("layout", ["single", "double", "double_cover"])
@pytest.mark.parametrize("zoom", [.25, 1.5, 4.0])
def test_icon_uses_same_viewport_hit_box_across_transforms(opened_notes, page, layout, zoom):
    _window, session, view, _path = opened_notes
    view.set_layout(layout)
    view.set_zoom(zoom)
    summary = note(session, page=page)
    before = summary.geometry
    view.select_inventory_annotation(summary.logical_id, reveal=True)
    mapper = view.coordinate_mapper(page)
    rect = note_icon_rect(mapper, summary.geometry.bbox)
    assert rect.width() == rect.height() == 22
    assert view._hit_test_inventory(rect.center(), page) == summary.logical_id
    assert view._hit_test_inventory(rect.topLeft() + QPointF(1, 1), page) == summary.logical_id
    assert view._hit_test_inventory(rect.bottomRight() + QPointF(1, 1), page) != summary.logical_id
    expected = mapper.canonical_to_viewport(((before.bbox.x0 + before.bbox.x1) / 2,
                                             (before.bbox.y0 + before.bbox.y1) / 2))
    assert rect.center() == QPointF(*expected)
    image = QImage(40, 40, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("transparent"))
    painter = QPainter(image)
    painter.translate(20 - rect.center().x(), 20 - rect.center().y())
    view._paint_inventory_annotation(painter, mapper, summary, selected=True)
    painter.end()
    assert image.pixelColor(20, 20).alpha() > 0
    assert summary.geometry == before


@pytest.mark.parametrize("close", ["x", "escape", "outside", "scroll", "zoom", "layout", "reopen"])
def test_dismissal_and_internal_scrolling(opened_notes, close):
    _window, session, view, _path = opened_notes
    reader = click_note(view, note(session, LONG))
    wait_until(lambda: reader.text.toPlainText() == LONG)
    reader.text.verticalScrollBar().setValue(100)
    assert reader.logical_id is not None and reader.isVisible()
    if close == "x":
        QTest.mouseClick(reader.close_button, Qt.MouseButton.LeftButton)
    elif close == "escape":
        QTest.keyClick(reader.text, Qt.Key.Key_Escape)
    elif close == "outside":
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
    elif close == "scroll":
        view.verticalScrollBar().setValue(view.verticalScrollBar().value() + 50)
    elif close == "zoom":
        view.set_zoom(1.5)
    elif close == "layout":
        view.set_layout("double")
    else:
        view.document_reopened()
    assert reader.logical_id is None and not reader.isVisible()
    reader.tick()
    assert not reader.isVisible()


def test_latest_note_waits_for_busy_channel_and_late_answer_cannot_reopen(opened_notes, monkeypatch):
    window, session, view, _path = opened_notes
    poll = window.manager._poll_inventory
    monkeypatch.setattr(window.manager, "_poll_inventory", lambda _session: None)
    first, second = note(session), note(session, LONG)
    reader = click_note(view, first)
    first_request = session.inventory_request_id
    assert first_request is not None
    click_note(view, second)
    assert reader.logical_id == second.logical_id
    assert session.inventory_request_id == first_request
    monkeypatch.setattr(window.manager, "_poll_inventory", poll)
    wait_until(lambda: reader.text.toPlainText() == LONG)
    assert reader.logical_id == second.logical_id
    # A previously closed reader must not reappear when its requested text arrives.
    session.annotation_details.pop(first.logical_id, None)
    monkeypatch.setattr(window.manager, "_poll_inventory", lambda _session: None)
    click_note(view, first)
    reader.dismiss()
    monkeypatch.setattr(window.manager, "_poll_inventory", poll)
    wait_until(lambda: first.logical_id in session.annotation_details)
    assert reader.logical_id is None and not reader.isVisible()


@pytest.mark.parametrize("failure", ["worker_error", "malformed"])
def test_detail_errors_are_visible_without_retry_loop(opened_notes, monkeypatch, failure):
    _window, session, view, _path = opened_notes
    poll = session.worker.poll
    def failing_poll(request_id=None):
        delivery = poll(request_id)
        if (delivery is not None and delivery.response is not None
                and delivery.response.operation == Operation.ANNOTATION_DETAIL):
            delivery.response = (replace(delivery.response, payload={"logical_id": "wrong"})
                if failure == "malformed" else replace(delivery.response, success=False,
                    error=ErrorInfo("TEST_DETAIL", "Injected detail failure")))
        return delivery
    monkeypatch.setattr(session.worker, "poll", failing_poll)
    reader = click_note(view, note(session))
    wait_until(lambda: reader.text.toPlainText().startswith("Impossibile caricare"))
    count = session.worker.submitted_counts[Operation.ANNOTATION_DETAIL]
    for _ in range(4):
        reader.tick()
    assert session.worker.submitted_counts[Operation.ANNOTATION_DETAIL] == count
    monkeypatch.setattr(session.worker, "poll", poll)
    click_note(view, note(session))
    wait_until(lambda: reader.text.toPlainText() == PLAIN)


def test_tab_close_and_generation_invalidation_ignore_late_detail(opened_notes, tmp_path, monkeypatch):
    window, session, view, path = opened_notes
    other = tmp_path / "other.pdf"
    other.write_bytes(path.read_bytes())
    window.open_paths([str(other)])
    wait_until(lambda: len(window.manager.sessions) == 2 and window.manager.active_id != session.document_id)
    window.manager.activate(session.document_id)
    reader = click_note(view, note(session))
    window.manager.activate(next(key for key in window.manager.sessions if key != session.document_id))
    assert reader.logical_id is None and not reader.isVisible()
    wait_until(lambda: note(session).logical_id in session.annotation_details)
    window.manager.activate(session.document_id)
    assert not reader.isVisible()
    click_note(view, note(session))
    session.document_generation += 1
    reader.tick()
    assert not reader.isVisible()
    session.document_generation -= 1
    click_note(view, note(session))
    assert window.manager.request_close(session.document_id)
    assert not reader.isVisible()
    wait_until(lambda: session.document_id not in window.manager.sessions)


def test_delete_remains_available_and_reader_closes(opened_notes):
    window, session, view, _path = opened_notes
    summary = note(session)
    assert summary.capabilities.deletable
    assert not any((summary.capabilities.editable_text, summary.capabilities.movable,
                    summary.capabilities.resizable, summary.capabilities.editable_style))
    reader = click_note(view, summary)
    wait_until(lambda: reader.text.toPlainText() == PLAIN)
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Delete)
    wait_until(lambda: summary.logical_id not in session.annotation_inventory)
    wait_until(lambda: reader.logical_id is None)
    assert session.dirty
    submit(window.manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert summary.logical_id in session.annotation_inventory
    assert not reader.isVisible()


def test_reading_preserves_native_graph_through_save_as_and_save(opened_notes, tmp_path):
    window, session, view, path = opened_notes
    before = inspect(path)
    reader = click_note(view, note(session, LONG))
    wait_until(lambda: reader.text.toPlainText() == LONG)
    target = tmp_path / "saved.pdf"
    save(window.manager, session, target)
    assert not reader.isVisible()
    assert inspect(target) == before
    wait_until(lambda: session.inventory_complete)
    reader = click_note(view, note(session))
    wait_until(lambda: reader.text.toPlainText() == PLAIN)
    command = ink(session)
    submit(window.manager, command)
    save(window.manager, session)
    after = inspect(target)
    assert after["text"] == before["text"] and after["content"] == before["content"]
    # Existing native annotations retain their complete semantic graph; the
    # production validator additionally compares all original AP/metadata keys.
    assert after["annots"][0][:len(before["annots"][0])] == before["annots"][0]
    assert path.read_bytes() != b"" and inspect(path) == before


@pytest.mark.parametrize("tool", ["pen", "text_box", "callout", "insert_text", "delete_text", "replace_text", "pan"])
def test_existing_tool_and_pan_precedence(opened_notes, tool):
    _window, session, view, _path = opened_notes
    summary = note(session)
    point = note_icon_rect(view.coordinate_mapper(0), summary.geometry.bbox).center().toPoint()
    if tool != "pan":
        getattr(view, f"set_{tool}_enabled")(True)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier if tool == "pan" else Qt.KeyboardModifier.NoModifier,
                    point)
    assert not view.sticky_note_reader.isVisible()
    if tool == "pen":
        assert view.pen_gesture.active
    elif tool == "pan":
        assert view._pan_consumed
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point)


def test_other_annotation_selection_dismisses_reader(opened_notes):
    _window, session, view, _path = opened_notes
    reader = click_note(view, note(session))
    other = next(item for item in session.annotation_inventory.values()
                 if item.page_index == 0 and item.kind == "text_box")
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=view._display_rect(0, other.geometry.bbox).center().toPoint())
    assert view.selected_inventory_id == other.logical_id
    assert not reader.isVisible()


def test_existing_editor_gets_confirmation_before_note_click(opened_notes):
    _window, session, view, _path = opened_notes
    view._begin_text_box_editor(RectTarget(0, PdfRect(70, 500, 300, 580)),
                                 None, FreeTextStyle(), "Bozza")
    click_note(view, note(session))
    assert not view.sticky_note_reader.isVisible()
    wait_until(lambda: view._text_editor is None and session.document_revision == 1)


def test_high_dpi_keeps_logical_icon_size_and_hit_area(opened_notes, monkeypatch):
    _window, session, view, _path = opened_notes
    summary = note(session)
    rect = note_icon_rect(view.coordinate_mapper(0), summary.geometry.bbox)
    monkeypatch.setattr(view.viewport(), "devicePixelRatioF", lambda: 2.0)
    assert note_icon_rect(view.coordinate_mapper(0), summary.geometry.bbox) == rect
    reader = click_note(view, summary)
    wait_until(lambda: reader.text.toPlainText() == PLAIN)
    assert reader.isVisible()


@pytest.mark.parametrize("identity", ["document", "generation"])
def test_wrong_identity_detail_cannot_reach_reader(opened_notes, monkeypatch, identity):
    _window, session, view, _path = opened_notes
    poll = session.worker.poll
    changed = []
    def stale_once(request_id=None):
        delivery = poll(request_id)
        if (not changed and delivery is not None and delivery.response is not None
                and delivery.response.operation == Operation.ANNOTATION_DETAIL):
            changed.append(True)
            delivery.response = replace(delivery.response,
                **({"document_id": "other-document"} if identity == "document" else
                   {"document_generation": session.document_generation - 1}))
        return delivery
    monkeypatch.setattr(session.worker, "poll", stale_once)
    reader = click_note(view, note(session))
    wait_until(lambda: reader.text.toPlainText() == PLAIN)
    assert changed and reader.logical_id == note(session).logical_id
