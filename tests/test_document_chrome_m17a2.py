"""Focused structural coverage for the consolidated M17A.2 document chrome."""

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QLabel, QMenuBar, QTabBar, QToolBar, QWidgetAction

from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.app.ui_chrome import COLORS
from pdf_annotator.viewer.layout_engine import LayoutMode
from tests.conftest import close_window_discard, wait_until
from tests.test_m6_viewer import _open
from tests.test_save_m15 import ink, submit


@pytest.fixture
def chrome_window(tmp_path: Path, app, pdf_fixture_copy):
    first, second = tmp_path / "chrome-a.pdf", tmp_path / "chrome-b.pdf"
    pdf_fixture_copy(first)
    pdf_fixture_copy(second)
    window = MainWindow()
    window.resize(1000, 720)
    window.show()
    yield window, first, second
    close_window_discard(window)


def test_document_chrome_uses_one_menu_and_tab_row_without_legacy_toolbar(chrome_window):
    window, first, _second = chrome_window
    menu = window.chrome_menu_bar
    assert window.findChildren(QMenuBar) == [menu]
    assert window.findChildren(QTabBar) == [window.tabs.tabBar()]
    assert window.tabs.tabBar().usesScrollButtons()
    assert menu.parentWidget() is window.tabs
    assert menu.isVisible()
    assert menu.height() > 0
    assert menu.minimumHeight() > 0
    assert menu.cornerWidget(Qt.Corner.TopLeftCorner) is window.chrome_brand
    assert window.chrome_brand_name.text() == "fuPDF"
    assert window.chrome_brand.isVisible()
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    menu_actions = {action.text(): action for action in menu.actions()}
    for title in ("File", "Modifica", "Visualizza"):
        geometry = menu.actionGeometry(menu_actions[title])
        assert geometry.width() > 0 and geometry.height() > 0
        assert menu.rect().contains(geometry.center())
    assert window.chrome_brand.geometry().right() < menu.actionGeometry(menu_actions["File"]).left()
    assert [action.text() for action in menu.actions()] == ["File", "Modifica", "Visualizza"]

    # Exercise the visible File menu rather than merely inspecting QAction
    # ownership: it must still reach the existing asynchronous open dialog.
    QTest.mouseClick(menu, Qt.MouseButton.LeftButton,
                     pos=menu.actionGeometry(menu_actions["File"]).center())
    wait_until(window.file_menu.isVisible)
    open_geometry = window.file_menu.actionGeometry(window.open_action)
    assert open_geometry.width() > 0 and open_geometry.height() > 0
    QTest.mouseClick(window.file_menu, Qt.MouseButton.LeftButton, pos=open_geometry.center())
    wait_until(lambda: window.file_dialog is not None and window.file_dialog.isVisible())
    window.file_dialog.reject()
    wait_until(lambda: window.file_dialog is None)

    _session, view = _open(window, first)
    tab_bar = window.tabs.tabBar()
    assert not any(bar.windowTitle() == "Zoom" for bar in window.findChildren(QToolBar))
    assert 0 <= tab_bar.x() - (menu.x() + menu.width()) <= 4
    assert menu.isVisible() and tab_bar.isVisible()
    assert menu.height() > 0 and tab_bar.height() > 0
    menu_center = menu.mapTo(window, QPoint()).y() + menu.height() // 2
    tab_center = tab_bar.mapTo(window, QPoint()).y() + tab_bar.height() // 2
    assert abs(menu_center - tab_center) <= 2
    assert view.mapTo(window, QPoint()).y() >= tab_bar.mapTo(window, QPoint()).y() + tab_bar.height()
    assert window.annotation_palette.isVisible()


def test_view_menu_owns_existing_layout_and_zoom_actions_per_active_tab(chrome_window):
    window, first, second = chrome_window
    first_session, first_view = _open(window, first)
    layout_actions = tuple(window.layout_actions.values())
    assert window.layout_group.isExclusive()
    assert all(action.isCheckable() for action in layout_actions)
    assert all(action in window.view_menu.actions() for action in layout_actions)
    assert any(isinstance(action, QWidgetAction) for action in window.view_menu.actions())

    window.layout_actions[LayoutMode.DOUBLE.value].trigger()
    assert first_view.session.ui.layout == LayoutMode.DOUBLE.value
    window.view_zoom_in_button.click()
    assert first_view.session.ui.zoom == pytest.approx(1.1)
    assert window.zoom_label.text() == "110%"

    second_session, second_view = _open(window, second)
    assert window.chrome_menu_bar.isVisible()
    assert all(window.chrome_menu_bar.actionGeometry(action).height() > 0
               for action in window.chrome_menu_bar.actions())
    assert second_view.session.ui.layout == LayoutMode.SINGLE.value
    assert window.layout_actions[LayoutMode.SINGLE.value].isChecked()
    assert window.zoom_label.text() == "100%"
    window.manager.activate(first_session.document_id)
    wait_until(lambda: window.manager.active_id == first_session.document_id)
    assert window.layout_actions[LayoutMode.DOUBLE.value].isChecked()
    assert window.zoom_label.text() == "110%"
    assert window.chrome_menu_bar.isVisible()
    assert second_session.document_revision == first_session.document_revision == 0


def test_compact_chrome_keeps_tab_switch_close_and_overflow_controls(chrome_window):
    window, first, second = chrome_window
    first_session, _first_view = _open(window, first)
    second_session, _second_view = _open(window, second)
    bar = window.tabs.tabBar()
    assert bar.tabsClosable() and bar.usesScrollButtons()
    window.resize(760, 460)
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(0).center())
    wait_until(lambda: window.manager.active_id == first_session.document_id)
    assert window.tabs.currentIndex() == 0
    assert bar.tabRect(0).left() >= 0
    assert window.chrome_menu_bar.isVisible()

    close_button = bar.tabButton(1, QTabBar.ButtonPosition.RightSide)
    if close_button is None:
        close_button = bar.tabButton(1, QTabBar.ButtonPosition.LeftSide)
    assert close_button is not None
    QTest.mouseClick(close_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: second_session.document_id not in window.manager.sessions)
    assert window.manager.active_id == first_session.document_id
    assert window.tabs.count() == 1


def test_annotation_sidebar_keeps_author_and_native_close_without_floating(chrome_window, monkeypatch):
    window, first, _second = chrome_window
    author = "Utente di prova 日本語"
    monkeypatch.setattr(window.manager, "_manual_author", author)
    session, view = _open(window, first)
    annotation = submit(window.manager, ink(session)).annotation
    dock = window.annotation_sidebar
    toggle = dock.toggleViewAction()
    toggle.trigger()
    wait_until(dock.isVisible)
    view.select_inventory_annotation(annotation.annotation_id.value, reveal=True)
    assert session.annotation_inventory[annotation.annotation_id.value].author == author
    QTest.keyClick(dock.list, Qt.Key.Key_Return)
    card = dock.list.itemWidget(dock.list.currentItem())
    assert card.findChild(QLabel, "annotation_author").text() == author

    assert dock.features() == dock.DockWidgetFeature.DockWidgetClosable
    float_button = dock.findChild(QAbstractButton, "qt_dockwidget_floatbutton")
    assert float_button is None or not float_button.isVisible()
    point = QPoint(30, dock.list.geometry().top() // 2)
    QTest.mouseDClick(dock, Qt.MouseButton.LeftButton, pos=point)
    QTest.mousePress(dock, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(dock, point + QPoint(-80, 45))
    QTest.mouseRelease(dock, Qt.MouseButton.LeftButton, pos=point + QPoint(-80, 45))
    assert not dock.isFloating() and window.dockWidgetArea(dock) == Qt.DockWidgetArea.RightDockWidgetArea

    close = dock.findChild(QAbstractButton, "qt_dockwidget_closebutton")
    assert close.isVisible()
    QTest.mouseClick(close, Qt.MouseButton.LeftButton)
    assert not dock.isVisible()
    toggle.trigger()
    wait_until(dock.isVisible)
    assert not dock.isFloating()


def test_qt_chrome_owns_caption_controls_and_background_move(chrome_window, monkeypatch):
    window, _first, _second = chrome_window
    chrome = window.window_chrome
    handle = window.windowHandle()
    flags = handle.flags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert not flags & (Qt.WindowType.ExpandedClientAreaHint | Qt.WindowType.NoTitleBarBackgroundHint)
    assert flags & Qt.WindowType.WindowType_Mask == Qt.WindowType.Window
    assert handle.title() == "fuPDF"
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    background = window.grab().toImage().pixelColor(0, 0)
    assert background.alpha() == 255 and background.name().upper() == COLORS.app.upper()
    assert window.tabs.mapTo(window, QPoint()).y() == chrome.RESIZE_BORDER
    assert window.tabs.cornerWidget(Qt.Corner.TopRightCorner) is chrome.controls
    assert chrome.controls.isVisible()
    for button in (chrome.minimize_button, chrome.maximize_button, chrome.close_button):
        assert button.isVisible() and button.width() == 40
        assert button.height() == window.chrome_menu_bar.height()
        assert not button.icon().isNull()
        assert not chrome.is_title_area(button, button.rect().center())
        assert not chrome.is_title_area(chrome.controls, button.geometry().center())
    calls = []
    monkeypatch.setattr(handle, "startSystemMove", lambda: calls.append("move") or True)
    point = QPoint(15, chrome.controls.height() // 2)
    assert chrome.is_title_area(chrome.controls, point)
    QTest.mouseClick(chrome.controls, Qt.MouseButton.LeftButton, pos=point)
    assert calls == ["move"]
    QTest.mouseDClick(chrome.controls, Qt.MouseButton.LeftButton, pos=point)
    wait_until(window.isMaximized)
    assert chrome.maximize_button.accessibleName() == "Ripristina"
    assert window.contentsMargins().isNull()
    QTest.mouseClick(chrome.maximize_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: not window.isMaximized())
    assert window.contentsMargins().left() == chrome.RESIZE_BORDER
    QTest.mouseClick(chrome.maximize_button, Qt.MouseButton.LeftButton)
    wait_until(window.isMaximized)
    chrome.toggle_maximized()
    QTest.mouseClick(chrome.minimize_button, Qt.MouseButton.LeftButton)
    wait_until(window.isMinimized)
    handle.showNormal()
    wait_until(lambda: not window.isMinimized())
    QTest.mouseClick(chrome.close_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.manager.is_shutdown and not window.isVisible())


def test_repeated_menu_clicks_never_start_a_window_move(chrome_window, monkeypatch):
    window, _first, _second = chrome_window
    menu = window.chrome_menu_bar
    moves = []
    monkeypatch.setattr(window.windowHandle(), "startSystemMove", lambda: moves.append(True) or True)
    actions = menu.actions()
    for _ in range(8):
        for action in actions:
            point = menu.actionGeometry(action).center()
            assert not window.window_chrome.is_title_area(menu, point)
            assert not window.window_chrome.is_title_area(window.tabs, menu.mapTo(window.tabs, point))
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=point)
            wait_until(action.menu().isVisible)
            action.menu().hide()
    # Double-clicking interactive menu and caption children must not toggle
    # window state through propagated parent events.
    QTest.mouseDClick(menu, Qt.MouseButton.LeftButton,
                     pos=menu.actionGeometry(actions[0]).center())
    actions[0].menu().hide()
    assert not window.isMaximized() and not moves


def test_center_caption_delegates_drag_before_during_and_after_documents(chrome_window, monkeypatch):
    window, first, second = chrome_window
    handle = window.windowHandle()
    moves = []
    monkeypatch.setattr(handle, "startSystemMove", lambda: moves.append(window.isMaximized()) or True)

    def center():
        tabs = window.tabs
        point = QPoint(tabs.width() // 2, window.chrome_menu_bar.height() // 2)
        bar = tabs.tabBar()
        if bar.count():
            point.setX(max(point.x(), bar.mapTo(tabs, bar.tabRect(bar.count() - 1).topRight()).x() + 8))
        return tabs.mapTo(window, point)

    def drag_center(expected_count):
        assert window.tabs.count() == expected_count
        point = center()
        recent = len(moves)
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(handle, point + QPoint(24, 4))
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=point + QPoint(24, 4))
        assert moves[recent:] == [window.isMaximized()]

    drag_center(0)
    QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=center() + QPoint(0, 90))
    assert moves == [False]  # The empty page area below the caption is not draggable.
    QTest.mouseDClick(handle, Qt.MouseButton.LeftButton, pos=center())
    wait_until(window.isMaximized)
    drag_center(0)
    QTest.mouseDClick(handle, Qt.MouseButton.LeftButton, pos=center())
    wait_until(lambda: not window.isMaximized())

    one, _view = _open(window, first)
    drag_center(1)
    two, _view = _open(window, second)
    drag_center(2)
    assert window.manager.request_close(two.document_id)
    wait_until(lambda: two.document_id not in window.manager.sessions)
    assert window.manager.request_close(one.document_id)
    wait_until(lambda: not window.manager.sessions)
    drag_center(0)


def test_tabs_and_close_clicks_never_start_a_window_move(chrome_window, monkeypatch):
    window, first, second = chrome_window
    _open(window, first)
    second_session, _view = _open(window, second)
    bar = window.tabs.tabBar()
    moves = []
    monkeypatch.setattr(window.windowHandle(), "startSystemMove", lambda: moves.append(True) or True)
    for index in (0, 1, 0, 1):
        point = bar.tabRect(index).center()
        assert not window.window_chrome.is_title_area(bar, point)
        assert not window.window_chrome.is_title_area(window.tabs, bar.mapTo(window.tabs, point))
        QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=point)
        assert window.tabs.currentIndex() == index
    QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(0).center())
    assert not window.isMaximized() and not moves
    close_button = bar.tabButton(1, QTabBar.ButtonPosition.RightSide)
    assert not window.window_chrome.is_title_area(bar, close_button.geometry().center())
    QTest.mouseClick(close_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: second_session.document_id not in window.manager.sessions)
    assert not moves


def test_open_menus_switch_rapidly_without_caption_input(chrome_window, monkeypatch):
    window, _first, _second = chrome_window
    menu = window.chrome_menu_bar
    moves = []
    monkeypatch.setattr(window.windowHandle(), "startSystemMove", lambda: moves.append(True) or True)
    for _ in range(5):
        for action in menu.actions():
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton,
                             pos=menu.actionGeometry(action).center())
            wait_until(action.menu().isVisible)
    for action in menu.actions():
        action.menu().hide()
    assert not moves and not window.isMaximized()


@pytest.mark.parametrize("horizontal,vertical,edges", [
    (0, 0, Qt.Edge.LeftEdge | Qt.Edge.TopEdge),
    (1, 0, Qt.Edge.RightEdge | Qt.Edge.TopEdge),
    (0, 1, Qt.Edge.LeftEdge | Qt.Edge.BottomEdge),
    (1, 1, Qt.Edge.RightEdge | Qt.Edge.BottomEdge),
    (0, None, Qt.Edge.LeftEdge), (1, None, Qt.Edge.RightEdge),
    (None, 0, Qt.Edge.TopEdge), (None, 1, Qt.Edge.BottomEdge),
])
def test_frame_edges_delegate_resize_only_outside_interactive_content(
        chrome_window, monkeypatch, horizontal, vertical, edges):
    window, _first, _second = chrome_window
    chrome = window.window_chrome
    # QTest treats a null QPoint as "use widget center", so keep corner
    # coordinates one pixel inside the six-pixel frame gutter.
    point = QPoint(window.width() // 2 if horizontal is None else 1 + (window.width() - 3) * horizontal,
                   window.height() // 2 if vertical is None else 1 + (window.height() - 3) * vertical)
    assert chrome.resize_edges(point) == edges
    resize_calls = []
    monkeypatch.setattr(window.windowHandle(), "startSystemResize",
                        lambda requested: resize_calls.append(requested) or True)
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
    assert resize_calls == [edges]
    assert not chrome.resize_edges(window.rect().center())
    chrome.toggle_maximized()
    assert not chrome.resize_edges(point)


@pytest.mark.parametrize("filename", ["a.pdf", "Long proof filename with editorial comments and revisions.pdf"])
def test_tab_close_has_padded_unclipped_geometry(chrome_window, filename):
    window, first, _second = chrome_window
    _open(window, first)
    bar = window.tabs.tabBar()
    window.tabs.setTabText(0, filename)
    wait_until(lambda: bar.tabText(0) == filename)
    button = bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
    assert button.size().width() == button.size().height() == 22
    assert bar.tabRect(0).contains(button.geometry())
    glyph_bounds = button.geometry().adjusted(7, 7, -7, -7)
    assert bar.tabRect(0).right() - glyph_bounds.right() >= 8
    assert button.height() >= 7 + 2 * 6  # Full X plus comfortable internal padding.
    assert not window.window_chrome.is_title_area(bar, bar.tabRect(0).center())
