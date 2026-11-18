"""Opaque Qt chrome: widget input only, with system move/resize delegation."""

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QHBoxLayout, QMenuBar, QStackedWidget, QTabWidget, QToolButton, QWidget

from .ui_chrome import COLORS, icon


class WindowChrome(QObject):
    """Own caption controls and empty-background gestures without native overlap."""

    RESIZE_BORDER = 6

    def __init__(self, window: QWidget, tabs: QTabWidget, menu: QMenuBar) -> None:
        super().__init__(window)
        self.window, self.tabs, self.menu = window, tabs, menu
        self.tab_bar = tabs.tabBar()
        flags = window.windowFlags() & ~(Qt.WindowType.ExpandedClientAreaHint
                                        | Qt.WindowType.NoTitleBarBackgroundHint
                                        | Qt.WindowType.CustomizeWindowHint)
        window.setWindowFlags(flags | Qt.WindowType.FramelessWindowHint)
        palette = window.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(COLORS.app))
        window.setPalette(palette)
        window.setAutoFillBackground(True)
        window.setMouseTracking(True)
        self.controls = QWidget(tabs)
        self.controls.setObjectName("windowControls")
        layout = QHBoxLayout(self.controls)
        # Keep an empty 32 px drag strip even when many tabs fill the row.
        layout.setContentsMargins(36, 0, 0, 0)
        layout.setSpacing(2)
        self.minimize_button = self._button("minimize", "Riduci a icona")
        self.maximize_button = self._button("maximize", "Ingrandisci")
        self.close_button = self._button("close", "Chiudi")
        self.close_button.setObjectName("windowCloseButton")
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            layout.addWidget(button)
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(window.close)
        # QTabWidget otherwise collapses a corner widget when no tabs exist.
        self.controls.setFixedHeight(menu.height())
        tabs.setCornerWidget(self.controls, Qt.Corner.TopRightCorner)
        # The tab bar can cover free caption space. Its actual tabs and
        # interactive children still keep their own input.
        tabs.installEventFilter(self)
        self.tab_bar.installEventFilter(self)
        self.controls.installEventFilter(self)
        window.installEventFilter(self)
        self._sync_window_state()

    def _button(self, glyph: str, name: str) -> QToolButton:
        button = QToolButton(self.controls)
        button.setObjectName("windowCaptionButton")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setIcon(icon(glyph))
        button.setIconSize(QSize(20, 20))
        button.setFixedSize(40, self.menu.height())
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(name)
        button.setAccessibleName(name)
        return button

    def _sync_window_state(self) -> None:
        maximized = self.window.isMaximized()
        self.window.unsetCursor()
        self.maximize_button.setIcon(icon("restore" if maximized else "maximize"))
        name = "Ripristina" if maximized else "Ingrandisci"
        self.maximize_button.setToolTip(name)
        self.maximize_button.setAccessibleName(name)
        border = 0 if maximized or self.window.isFullScreen() else self.RESIZE_BORDER
        self.window.setContentsMargins(border, border, border, border)

    def is_title_area(self, surface: QWidget, position: QPoint) -> bool:
        if (surface not in (self.tabs, self.tab_bar, self.controls)
                or not surface.rect().contains(position) or position.y() >= self.menu.height()):
            return False
        child = surface.childAt(position)
        if surface is self.tab_bar:
            return child is None and self.tab_bar.tabAt(position) == -1
        # Without tabs, Qt extends the empty page container into the caption.
        # It propagates its clicks to QTabWidget and has no page to interact with.
        return child is None or (surface is self.tabs and not self.tabs.count()
                                 and isinstance(child, QStackedWidget) and child.parentWidget() is self.tabs)

    def resize_edges(self, position: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if self.window.isMaximized() or self.window.isFullScreen():
            return edges
        border = self.RESIZE_BORDER
        if position.x() < border:
            edges |= Qt.Edge.LeftEdge
        elif position.x() >= self.window.width() - border:
            edges |= Qt.Edge.RightEdge
        if position.y() < border:
            edges |= Qt.Edge.TopEdge
        elif position.y() >= self.window.height() - border:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _resize_cursor(self, edges: Qt.Edge) -> None:
        if edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
            self.window.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif edges in (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
            self.window.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
            self.window.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edges:
            self.window.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.window.unsetCursor()

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if watched is self.window:
            if kind == QEvent.Type.WindowStateChange:
                self._sync_window_state()
            elif kind == QEvent.Type.MouseMove:
                self._resize_cursor(self.resize_edges(event.position().toPoint()))
            elif (kind == QEvent.Type.MouseButtonPress
                  and event.button() == Qt.MouseButton.LeftButton):
                edges = self.resize_edges(event.position().toPoint())
                if edges:
                    return self.window.windowHandle().startSystemResize(edges)
            elif kind == QEvent.Type.Leave:
                self.window.unsetCursor()
        elif (watched in (self.tabs, self.tab_bar, self.controls)
              and kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
              and event.button() == Qt.MouseButton.LeftButton
              and self.is_title_area(watched, event.position().toPoint())):
            if kind == QEvent.Type.MouseButtonDblClick:
                self.toggle_maximized()
                return True
            return self.window.windowHandle().startSystemMove()
        return super().eventFilter(watched, event)

    def toggle_maximized(self) -> None:
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()
