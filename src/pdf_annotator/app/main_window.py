"""Document tabs, continuous viewer and the minimal M7 Pen surface."""

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QSize, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMenuBar, QMessageBox, QPushButton, QSpinBox,
                               QGraphicsDropShadowEffect, QTabWidget, QToolBar, QToolButton,
                               QVBoxLayout, QWidget, QWidgetAction)

from ..worker.protocol import ErrorInfo
from ..domain.geometry import PdfRect
from ..domain.styles import (PEN_COLOR_PRESETS, HIGHLIGHT_COLOR_PRESETS, RgbColor, TEXT_BOX_UI_FONT_FAMILIES,
                             TEXT_BOX_BORDER_PRESETS, TEXT_BOX_FILL_PRESETS)
from ..viewer.pdf_view import PdfView
from ..viewer.layout_engine import LayoutMode
from .document_manager import DocumentManager
from .document_session import Lifecycle
from .review_controller import ReviewController
from .review_dialog import ReviewDialog
from .text_export import TextExportController
from .annotation_sidebar import AnnotationSidebar
from .canonical_fonts import ensure_canonical_editor_fonts
from .branding import APPLICATION_NAME, ICON_PATH
from .ui_chrome import (COLORS, CenteredMenuButton, DocumentTabBar, application_stylesheet,
                        border_swatch_icon, configure_swatch_menu,
                        fill_swatch_icon, icon, letter_icon, stroke_swatch_icon)
from .window_chrome import WindowChrome


class _FloatingPalette(QFrame):
    """A movable child widget, intentionally never a native top-level window."""

    def __init__(self, parent: QWidget, moved) -> None:
        super().__init__(parent)
        self._moved = moved
        self._drag_offset: QPoint | None = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("floatingAnnotationPalette")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            parent = self.parentWidget()
            if parent is not None:
                position = self.mapToParent(event.position().toPoint() - self._drag_offset)
                x = max(0, min(parent.width() - self.width(), position.x()))
                y = max(0, min(parent.height() - self.height(), position.y()))
                self.move(x, y)
                self._moved(self.pos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        # Tests and alternate launchers construct MainWindow directly. Keep the
        # same controlled private-font gate as the normal executable path.
        ensure_canonical_editor_fonts()
        self.setWindowTitle(APPLICATION_NAME)
        self.setFont(QFont("Segoe UI", 10))
        self.setStyleSheet(application_stylesheet())
        self.resize(760, 460)
        self.manager = DocumentManager(self)
        self.reviews = ReviewController(self.manager, self)
        self.text_exports = TextExportController(self.manager, self)
        self.review_dialog: ReviewDialog | None = None
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("documentTabs")
        self.tabs.setTabBar(DocumentTabBar(self.tabs))
        self.tabs.setTabsClosable(True)
        self.setCentralWidget(self.tabs)
        # Keep Qt's existing tab bar authoritative for lifecycle, close
        # controls and keyboard navigation. A corner menu bar makes it share
        # the same compact chrome row instead of manufacturing a second tab UI.
        self.chrome_menu_bar = QMenuBar(self.tabs)
        self.chrome_menu_bar.setObjectName("documentChromeMenu")
        self.chrome_brand = QWidget(self.chrome_menu_bar)
        self.chrome_brand.setObjectName("chromeBrand")
        brand_layout = QHBoxLayout(self.chrome_brand)
        brand_layout.setContentsMargins(8, 0, 14, 0)
        brand_layout.setSpacing(7)
        brand_icon = QLabel(self.chrome_brand)
        # Retain enough source pixels when the native window moves to a denser screen.
        brand_icon.setPixmap(QIcon(str(ICON_PATH)).pixmap(QSize(22, 22), 3.0))
        brand_icon.setFixedSize(22, 22)
        brand_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.chrome_brand_name = QLabel(APPLICATION_NAME, self.chrome_brand)
        self.chrome_brand_name.setObjectName("chromeBrandName")
        brand_layout.addWidget(brand_icon)
        brand_layout.addWidget(self.chrome_brand_name)
        self.chrome_menu_bar.setCornerWidget(self.chrome_brand, Qt.Corner.TopLeftCorner)
        self.tabs.setCornerWidget(self.chrome_menu_bar, Qt.Corner.TopLeftCorner)
        self._widgets: dict[str, PdfView] = {}
        self.file_dialog: QFileDialog | None = None
        self.save_dialog: QFileDialog | None = None
        self.text_export_dialog: QFileDialog | None = None
        self._close_after_save: set[str] = set()
        self._closing_application = False
        self.open_action = QAction("Apri…", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.choose_files)
        self.file_menu = self.chrome_menu_bar.addMenu("File")
        self.file_menu.addAction(self.open_action)
        self.save_action = QAction("Salva", self)
        self.save_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_action.triggered.connect(lambda: self._save_active(False))
        self.save_as_action = QAction("Salva con nome…", self)
        self.save_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.save_as_action.triggered.connect(lambda: self._save_active(True))
        self.file_menu.addActions([self.save_action, self.save_as_action])
        self.file_menu.addSeparator()
        self.review_action = QAction("Revisione con IA…", self)
        self.review_action.triggered.connect(lambda: self._open_review(self.manager.active_id))
        self.file_menu.addAction(self.review_action)
        self.export_text_action = QAction("Esporta testo strutturato…", self)
        self.export_text_action.triggered.connect(self._choose_text_export)
        self.file_menu.addAction(self.export_text_action)
        self.text_exports.changed.connect(self._update_zoom_controls)
        self.text_exports.finished.connect(lambda _document, path: self.statusBar().showMessage(
            f"Testo strutturato esportato: {path}", 12000))
        self.text_exports.failed.connect(self.manager.error_occurred.emit)
        self.edit_menu = self.chrome_menu_bar.addMenu("Modifica")
        self.undo_action = QAction("Annulla", self)
        self.undo_action.setShortcuts([QKeySequence("Ctrl+Z")])
        self.undo_action.triggered.connect(self.manager.undo_active)
        self.redo_action = QAction("Ripeti", self)
        self.redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.redo_action.triggered.connect(self.manager.redo_active)
        self.addAction(self.undo_action)
        self.addAction(self.redo_action)
        self.edit_menu.addAction(self.undo_action)
        self.edit_menu.addAction(self.redo_action)
        self.find_action = QAction("Trova", self)
        self.find_action.setShortcut(QKeySequence.StandardKey.Find)
        self.find_action.triggered.connect(self._open_find_bar)
        self.addAction(self.find_action)
        self.view_menu = self.chrome_menu_bar.addMenu("Visualizza")
        # With no document tabs Qt gives QTabBar a zero height and propagates
        # that height to corner widgets. Calculate the menu hint only after
        # its entries exist, then reserve one compact row for File -> Apri.
        chrome_height = max(32, self.chrome_menu_bar.sizeHint().height())
        self.chrome_menu_bar.setFixedHeight(chrome_height)
        self.tabs.tabBar().setMinimumHeight(chrome_height)
        self.tabs.currentChanged.connect(self._tab_activated)
        self.tabs.tabCloseRequested.connect(self._tab_closed)
        self.manager.session_changed.connect(self._session_changed)
        self.manager.session_removed.connect(self._remove_tab)
        self.manager.active_changed.connect(self._activate_tab)
        self.manager.error_occurred.connect(self._show_error)
        self.manager.annotation_result.connect(self._annotation_result)
        self.manager.inventory_changed.connect(self._inventory_changed)
        self.manager.history_delta_changed.connect(self._history_delta_changed)
        self.manager.annotation_detail_changed.connect(self._annotation_detail_changed)
        self.manager.annotation_detail_failed.connect(self._annotation_detail_failed)
        self.manager.save_overwrite_required.connect(self._confirm_overwrite)
        self.manager.save_finished.connect(self._save_finished)
        self.manager.document_reopened.connect(self._document_reopened)
        self.manager.shutdown_finished.connect(self.close)
        self.manager.session_tick.connect(self._render_tick)
        self.page_count_label = QLabel(self)
        self.review_indicator = QToolButton(self)
        self.review_indicator.setText("Revisione IA")
        self.review_indicator.setToolTip("Apri il monitor della revisione IA")
        self.review_indicator.clicked.connect(lambda: self._open_review(self.reviews.monitor_document()))
        self.statusBar().addPermanentWidget(self.review_indicator)
        self.review_indicator.hide()
        self.reviews.changed.connect(self._update_review_indicator)
        self.reviews.shutdown_finished.connect(self.close)
        self.statusBar().addPermanentWidget(self.page_count_label)
        self.statusBar().showMessage("Apri un PDF.")
        self.annotation_sidebar = AnnotationSidebar(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.annotation_sidebar)
        # M4's narrow-window layout contract predates M12.  Start the optional
        # dock collapsed so adding the inventory does not reduce the document
        # canvas or manufacture a horizontal scrollbar at a fitting zoom.
        self.annotation_sidebar.hide()
        self.view_menu.addAction(self.annotation_sidebar.toggleViewAction())
        self.annotation_sidebar.annotation_activated.connect(self._sidebar_annotation_activated)
        self.annotation_sidebar.detail_requested.connect(self._sidebar_detail_requested)
        self.find_bar = QToolBar("Trova", self)
        self.find_bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.find_bar)
        self.search_field = QLineEdit(self.find_bar)
        self.search_field.setPlaceholderText("Trova nel documento")
        self.search_field.setClearButtonEnabled(True)
        self.search_field.setMaximumWidth(300)
        self.search_field.textChanged.connect(self._search_changed)
        self.search_field.installEventFilter(self)
        self.find_bar.addWidget(self.search_field)
        self.search_count = QLabel("0 / 0", self.find_bar)
        self.find_bar.addWidget(self.search_count)
        previous = QPushButton("Precedente", self.find_bar)
        previous.clicked.connect(lambda: self._navigate_search(False))
        self.find_bar.addWidget(previous)
        following = QPushButton("Successivo", self.find_bar)
        following.clicked.connect(lambda: self._navigate_search(True))
        self.find_bar.addWidget(following)
        close_find = QPushButton("Chiudi", self.find_bar)
        close_find.clicked.connect(self._close_find_bar)
        self.find_bar.addWidget(close_find)
        self.find_bar.hide()
        self.zoom_out_action = QAction("Riduci zoom", self)
        self.zoom_out_action.triggered.connect(lambda: self._zoom(False))
        self.zoom_in_action = QAction("Aumenta zoom", self)
        self.zoom_in_action.triggered.connect(lambda: self._zoom(True))
        self.view_menu.addSeparator()
        self.layout_group = QActionGroup(self.view_menu)
        self.layout_button = QToolButton(self)
        # Compatibility state holder for callers/tests of the previous control;
        # it is deliberately outside every layout and never visible.
        self.layout_button.setAccessibleName("Layout pagine")
        self.layout_button.hide()
        self.layout_actions: dict[str, QAction] = {}
        for label, mode in (("Singola", LayoutMode.SINGLE), ("Doppia", LayoutMode.DOUBLE),
                            ("Doppia con copertina", LayoutMode.DOUBLE_COVER)):
            action = self.view_menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode.value)
            self.layout_group.addAction(action)
            self.layout_actions[mode.value] = action
        self.layout_group.triggered.connect(self._layout_selected)
        self.view_menu.addSeparator()
        self._build_view_zoom_row()

        self.pen_action = QAction("Penna", self)
        self.pen_action.setCheckable(True)
        self.pen_action.toggled.connect(self._pen_toggled)
        self.pen_color_button = QToolButton(self)
        self.pen_color_button.setAccessibleName("Colore penna")
        self.pen_color_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        color_menu = QMenu(self.pen_color_button)
        color_group = QActionGroup(color_menu)
        self.pen_color_actions: dict[str, QAction] = {}
        for name, _color in PEN_COLOR_PRESETS:
            action = QAction(name.capitalize(), color_group)
            action.setCheckable(True)
            action.setData(name)
            color_group.addAction(action)
            self.pen_color_actions[name] = action
        color_group.triggered.connect(self._pen_color_selected)
        configure_swatch_menu(color_menu, tuple((name, color, self.pen_color_actions[name])
                                                for name, color in PEN_COLOR_PRESETS), role="stroke")
        self.pen_color_button.setMenu(color_menu)
        self.pen_color_button.setText("Colore")
        self.text_box_action = QAction("Testo", self)
        self.text_box_action.setCheckable(True)
        self.text_box_action.toggled.connect(self._text_box_toggled)
        self.callout_action = QAction("Callout", self)
        self.callout_action.setCheckable(True)
        self.callout_action.toggled.connect(self._callout_toggled)
        self.delete_text_action = QAction("Elimina testo", self)
        self.delete_text_action.setCheckable(True)
        self.delete_text_action.toggled.connect(self._delete_text_toggled)
        self.insert_text_action = QAction("Inserisci testo", self)
        self.insert_text_action.setCheckable(True)
        self.insert_text_action.toggled.connect(self._insert_text_toggled)
        self.replace_text_action = QAction("Sostituisci testo", self)
        self.replace_text_action.setCheckable(True)
        self.replace_text_action.toggled.connect(self._replace_text_toggled)
        self.text_fill_button = QToolButton(self)
        self.text_fill_button.setAccessibleName("Riempimento riquadro testo")
        self.text_fill_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        fill_menu, fill_group = QMenu(self.text_fill_button), QActionGroup(self.text_fill_button)
        self.text_fill_actions: dict[str, QAction] = {}
        for name, _color in TEXT_BOX_FILL_PRESETS:
            action = QAction(name.replace("-", " ").capitalize(), fill_group)
            action.setCheckable(True); action.setData(name); fill_group.addAction(action)
            self.text_fill_actions[name] = action
        fill_group.triggered.connect(self._text_fill_selected)
        configure_swatch_menu(fill_menu, tuple((name, color, self.text_fill_actions[name])
                                               for name, color in TEXT_BOX_FILL_PRESETS), role="fill")
        self.text_fill_button.setMenu(fill_menu); self.text_fill_button.setText("Riemp.")
        self.text_border_button = QToolButton(self)
        self.text_border_button.setAccessibleName("Bordo riquadro testo")
        self.text_border_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        border_menu, border_group = QMenu(self.text_border_button), QActionGroup(self.text_border_button)
        self.text_border_actions: dict[str, QAction] = {}
        for name, _color in TEXT_BOX_BORDER_PRESETS:
            action = QAction(name.capitalize(), border_group)
            action.setCheckable(True); action.setData(name); border_group.addAction(action)
            self.text_border_actions[name] = action
        border_group.triggered.connect(self._text_border_selected)
        configure_swatch_menu(border_menu, tuple((name, color, self.text_border_actions[name])
                                                 for name, color in TEXT_BOX_BORDER_PRESETS), role="border")
        self.text_border_button.setMenu(border_menu); self.text_border_button.setText("Bordo")
        self.text_color_button = QToolButton(self)
        self.text_color_button.setAccessibleName("Colore testo riquadro")
        self.text_color_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        text_color_menu = QMenu(self.text_color_button)
        text_color_group = QActionGroup(self.text_color_button)
        self.text_color_actions: dict[str, QAction] = {}
        for name, _color in PEN_COLOR_PRESETS:
            action = QAction(name.capitalize(), text_color_group)
            action.setCheckable(True)
            action.setData(name)
            text_color_group.addAction(action)
            self.text_color_actions[name] = action
        text_color_group.triggered.connect(self._text_color_selected)
        configure_swatch_menu(text_color_menu, tuple((name, color, self.text_color_actions[name])
                                                     for name, color in PEN_COLOR_PRESETS), role="text")
        self.text_color_button.setMenu(text_color_menu)
        self.text_color_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.text_color_button.setIcon(letter_icon("A", dict(PEN_COLOR_PRESETS)["black"], bold=True))
        self.text_color_button.setIconSize(QSize(20, 20))
        self.text_color_button.setText("Testo")
        self.text_font_button = QToolButton(self)
        self.text_font_button.setAccessibleName("Font riquadro testo")
        self.text_font_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        font_menu = QMenu(self.text_font_button)
        font_group = QActionGroup(font_menu)
        self.text_font_actions: dict[str, QAction] = {}
        for family in TEXT_BOX_UI_FONT_FAMILIES:
            action = font_menu.addAction(family)
            action.setCheckable(True)
            action.setData(family)
            font_group.addAction(action)
            self.text_font_actions[family] = action
        font_group.triggered.connect(self._text_font_selected)
        self.text_font_button.setMenu(font_menu)
        self.text_font_button.setText("Font")
        self.text_size_spin = QSpinBox(self)
        self.text_size_spin.setAccessibleName("Dimensione testo riquadro in punti")
        self.text_size_spin.setRange(6, 72)
        self.text_size_spin.setSuffix(" pt")
        self.text_size_spin.valueChanged.connect(self._text_size_selected)
        self.text_bold_action = QAction("Grassetto", self)
        self.text_bold_action.setCheckable(True)
        self.text_bold_action.triggered.connect(self._text_bold_toggled)
        self.text_italic_action = QAction("Corsivo", self)
        self.text_italic_action.setCheckable(True)
        self.text_italic_action.triggered.connect(self._text_italic_toggled)
        self._text_format_controls = tuple(control for control in (
            self.text_color_button, self.text_font_button, self.text_size_spin,
            self.text_size_spin.lineEdit()) if control is not None)
        for control in self._text_format_controls:
            control.installEventFilter(self)
        self._text_format_menus = (text_color_menu, font_menu)
        for menu in self._text_format_menus:
            menu.aboutToShow.connect(self._preserve_text_box_formatting_selection)
        self.highlight_action = QAction("Evidenzia", self)
        self.highlight_action.triggered.connect(self._highlight)
        self.highlight_action.setShortcut("Ctrl+H")
        self.addAction(self.highlight_action)
        self._retired_toolbar_controls = (self.pen_color_button, self.text_fill_button,
                                          self.text_border_button, self.text_color_button,
                                          self.text_font_button, self.text_size_spin)
        for control in self._retired_toolbar_controls:
            control.hide()
        self._palette_position = QPoint(16, 42)
        self._build_contextual_annotation_ui()
        self._update_zoom_controls()
        self.window_chrome = WindowChrome(self, self.tabs, self.chrome_menu_bar)

    def _build_view_zoom_row(self) -> None:
        """Embed the existing zoom actions in Visualizza without new state."""
        host = QWidget(self.view_menu)
        host.setObjectName("viewZoomRow")
        layout = QHBoxLayout(host)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(5)
        self.view_zoom_out_button = QToolButton(host)
        self.view_zoom_out_button.setObjectName("viewZoomButton")
        self.view_zoom_out_button.setDefaultAction(self.zoom_out_action)
        self.view_zoom_out_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.view_zoom_out_button.setText("−")
        self.view_zoom_out_button.setToolTip("Riduci zoom")
        self.view_zoom_out_button.setFixedSize(24, 24)
        self.zoom_label = QLabel("100%", host)
        self.zoom_label.setObjectName("viewZoomLabel")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view_zoom_in_button = QToolButton(host)
        self.view_zoom_in_button.setObjectName("viewZoomButton")
        self.view_zoom_in_button.setDefaultAction(self.zoom_in_action)
        self.view_zoom_in_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.view_zoom_in_button.setText("+")
        self.view_zoom_in_button.setToolTip("Aumenta zoom")
        self.view_zoom_in_button.setFixedSize(24, 24)
        for control in (self.view_zoom_out_button, self.zoom_label, self.view_zoom_in_button):
            layout.addWidget(control)
        self.view_zoom_action = QWidgetAction(self.view_menu)
        self.view_zoom_action.setDefaultWidget(host)
        self.view_menu.addAction(self.view_zoom_action)

    def _build_contextual_annotation_ui(self) -> None:
        """Build M17A.1's lightweight GUI-only contextual controls.

        All buttons reuse the existing QAction handlers or view setters. They
        consequently remain presentation-only and cannot create a second PDF
        command path.
        """
        self.annotation_palette = _FloatingPalette(self.tabs, self._remember_palette_position)
        self._apply_surface_shadow(self.annotation_palette)
        palette_layout = QVBoxLayout(self.annotation_palette)
        palette_layout.setContentsMargins(7, 8, 7, 8)
        palette_layout.setSpacing(4)
        drag_handle = QFrame(self.annotation_palette)
        drag_handle.setObjectName("paletteDragHandle")
        drag_handle.setFixedSize(20, 3)
        drag_handle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        palette_layout.addWidget(drag_handle, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.palette_pen_button = self._compact_action_button(
            self.pen_action, self.annotation_palette, "pen", "Penna", "paletteToolButton", 38)
        self.palette_text_box_button = self._compact_action_button(
            self.text_box_action, self.annotation_palette, "textbox", "Riquadro testo",
            "paletteToolButton", 38)
        self.palette_callout_button = self._compact_action_button(
            self.callout_action, self.annotation_palette, "callout", "Callout", "paletteToolButton", 38)
        for button in (self.palette_pen_button, self.palette_text_box_button,
                       self.palette_callout_button):
            palette_layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.annotation_palette.adjustSize()
        self.annotation_palette.move(self._palette_position)
        self.annotation_palette.show()

        self.creation_properties = QFrame(self.tabs)
        self.creation_properties.setObjectName("contextualCreationProperties")
        self.creation_properties.setFrameShape(QFrame.Shape.StyledPanel)
        self._apply_surface_shadow(self.creation_properties)
        property_layout = QHBoxLayout(self.creation_properties)
        property_layout.setContentsMargins(6, 4, 6, 4)
        property_layout.setSpacing(4)
        self.context_property_label = QLabel(self.creation_properties)
        self.context_property_label.hide()
        self.context_pen_color_button = self._menu_button("Colore", self.pen_color_button.menu(),
                                                           self.creation_properties)
        self.context_text_fill_button = self._menu_button("Riemp.", self.text_fill_button.menu(),
                                                           self.creation_properties)
        self.context_text_border_button = self._menu_button("Bordo", self.text_border_button.menu(),
                                                             self.creation_properties)
        self.context_text_color_button = self._menu_button("Testo", self.text_color_button.menu(),
                                                            self.creation_properties)
        self.context_text_font_button = self._menu_button("Font", self.text_font_button.menu(),
                                                           self.creation_properties)
        self.context_text_font_button.setObjectName("contextFontFamily")
        self.context_text_font_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.context_text_size_spin = QSpinBox(self.creation_properties)
        self.context_text_size_spin.setObjectName("contextFontSize")
        self.context_text_size_spin.setRange(6, 72)
        self.context_text_size_spin.setSuffix(" pt")
        self.context_text_size_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.context_text_size_spin.setFixedWidth(46)
        self.context_text_size_spin.setFixedHeight(32)
        self.context_text_size_spin.valueChanged.connect(self._text_size_selected)
        self.context_size_down_button = QToolButton(self.creation_properties)
        self.context_size_down_button.setObjectName("contextStepButton")
        self.context_size_down_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.context_size_down_button.setIcon(icon("chevron-down"))
        self.context_size_down_button.setIconSize(QSize(16, 16))
        self.context_size_down_button.setToolTip("Riduci dimensione testo")
        self.context_size_down_button.setAccessibleName("Riduci dimensione testo")
        self.context_size_down_button.setFixedWidth(25)
        self.context_size_down_button.setFixedHeight(23)
        self.context_size_down_button.clicked.connect(lambda: self.context_text_size_spin.stepBy(-1))
        self.context_size_up_button = QToolButton(self.creation_properties)
        self.context_size_up_button.setObjectName("contextStepButton")
        self.context_size_up_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.context_size_up_button.setIcon(icon("chevron-up"))
        self.context_size_up_button.setIconSize(QSize(16, 16))
        self.context_size_up_button.setToolTip("Aumenta dimensione testo")
        self.context_size_up_button.setAccessibleName("Aumenta dimensione testo")
        self.context_size_up_button.setFixedWidth(25)
        self.context_size_up_button.setFixedHeight(23)
        self.context_size_up_button.clicked.connect(lambda: self.context_text_size_spin.stepBy(1))
        self.context_bold_button = self._action_button("Grassetto", self.text_bold_action,
                                                        self.creation_properties)
        self.context_italic_button = self._action_button("Corsivo", self.text_italic_action,
                                                          self.creation_properties)
        self._context_text_controls = (self.context_text_fill_button, self.context_text_border_button,
                                       self.context_text_color_button, self.context_bold_button,
                                       self.context_italic_button, self.context_text_font_button,
                                       self.context_text_size_spin, self.context_size_down_button,
                                       self.context_size_up_button)
        for button in (self.context_pen_color_button, self.context_text_fill_button,
                       self.context_text_border_button, self.context_text_color_button):
            button.setObjectName("colorControl")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setFixedSize(32, 32)
            button.setIconSize(QSize(20, 20))
        self.context_pen_color_button.setProperty("color_role", "stroke")
        self.context_text_fill_button.setProperty("color_role", "fill")
        self.context_text_border_button.setProperty("color_role", "border")
        self.context_text_color_button.setProperty("color_role", "text")
        self.context_text_color_button.setProperty("color_renderer", "letter")
        for button in (self.context_bold_button, self.context_italic_button):
            button.setObjectName("formatToggle")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setFixedSize(38, 38)
        bold_font = self.context_bold_button.font()
        bold_font.setBold(True)
        self.context_bold_button.setFont(bold_font)
        italic_font = self.context_italic_button.font()
        italic_font.setItalic(True)
        self.context_italic_button.setFont(italic_font)
        for widget in (self.context_pen_color_button, *self._context_text_controls):
            property_layout.addWidget(widget)
            widget.installEventFilter(self)
        self.creation_properties.hide()

        self.pdf_text_bar = QFrame(self.tabs)
        self.pdf_text_bar.setObjectName("pdfTextSelectionBar")
        self.pdf_text_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self._apply_surface_shadow(self.pdf_text_bar)
        pdf_layout = QHBoxLayout(self.pdf_text_bar)
        pdf_layout.setContentsMargins(6, 4, 6, 4)
        pdf_layout.setSpacing(4)
        self.context_highlight_button = self._compact_action_button(
            self.highlight_action, self.pdf_text_bar, "highlight", "Evidenzia", "contextIconButton", 32)
        pdf_layout.addWidget(self.context_highlight_button)
        self.highlight_color_menu = QMenu(self.context_highlight_button)
        self.highlight_color_menu.setStyleSheet("QMenu { padding: 2px; min-width: 0px; }")
        self.highlight_color_actions = {}
        for name, color in HIGHLIGHT_COLOR_PRESETS:
            action = QAction(fill_swatch_icon(color), name, self.highlight_color_menu)
            action.setCheckable(True)
            action.triggered.connect(lambda checked=False, preset=name: self._choose_highlight_color(preset))
            self.highlight_color_actions[name] = action
            swatch = QToolButton(self.highlight_color_menu)
            swatch.setDefaultAction(action)
            swatch.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            swatch.setObjectName("colorControl")
            swatch.setFixedSize(34, 34)
            swatch.setIconSize(QSize(20, 20))
            swatch.setToolTip(name)
            swatch.setAccessibleName(name)
            swatch.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            action.triggered.connect(self.highlight_color_menu.hide)
            row = QWidgetAction(self.highlight_color_menu)
            row.setDefaultWidget(swatch)
            self.highlight_color_menu.addAction(row)
        self.context_highlight_button.setMenu(self.highlight_color_menu)
        self.context_highlight_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.context_highlight_button.setFixedWidth(46)
        self.context_highlight_button.setStyleSheet("max-width: 46px; min-width: 46px;")
        self.context_highlight_button.setAccessibleName("Evidenzia; freccia per scegliere il colore")
        self.context_delete_button = QToolButton(self.pdf_text_bar)
        self.context_delete_button.setObjectName("contextIconButton")
        self.context_delete_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.context_delete_button.setIcon(icon("delete"))
        self.context_delete_button.setToolTip("Elimina testo")
        self.context_delete_button.setText("Elimina testo")
        self.context_delete_button.setFixedSize(32, 32)
        self.context_delete_button.clicked.connect(self._delete_from_context)
        self.context_replace_button = QToolButton(self.pdf_text_bar)
        self.context_replace_button.setObjectName("contextIconButton")
        self.context_replace_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.context_replace_button.setIcon(icon("replace"))
        self.context_replace_button.setToolTip("Sostituisci testo")
        self.context_replace_button.setText("Sostituisci testo")
        self.context_replace_button.setFixedSize(32, 32)
        self.context_replace_button.clicked.connect(self._replace_from_context)
        for widget in (self.context_delete_button,
                       self.context_replace_button):
            pdf_layout.addWidget(widget)
        self.pdf_text_bar.hide()

    @staticmethod
    def _action_button(text: str, action: QAction, parent: QWidget) -> QToolButton:
        button = QToolButton(parent)
        button.setText(text)
        button.setDefaultAction(action)
        return button

    @staticmethod
    def _apply_surface_shadow(widget: QWidget) -> None:
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(14)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 78))
        widget.setGraphicsEffect(shadow)

    @staticmethod
    def _compact_action_button(action: QAction, parent: QWidget, icon_name: str,
                               tooltip: str, object_name: str, size: int) -> QToolButton:
        button = QToolButton(parent)
        button.setObjectName(object_name)
        icon_size = 24 if object_name == "paletteToolButton" else 20
        def refresh_icon(_checked: bool = False) -> None:
            action.setIcon(icon(icon_name, color=COLORS.accent if action.isChecked() else COLORS.primary,
                                size=icon_size))

        refresh_icon()
        if action.isCheckable():
            action.toggled.connect(refresh_icon)
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(size, size)
        button.setIconSize(QSize(icon_size, icon_size))
        return button

    @staticmethod
    def _menu_button(text: str, menu: QMenu | None, parent: QWidget) -> QToolButton:
        button = CenteredMenuButton(parent)
        button.setText(text)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        if menu is not None:
            button.setMenu(menu)
        return button

    def _remember_palette_position(self, position: QPoint) -> None:
        self._palette_position = QPoint(position)
        view = self._widgets.get(self.manager.active_id)
        if (view is not None and self.creation_properties.isVisible()
                and (view.pen_enabled or view.text_box_enabled or view.callout_enabled)):
            self._place_attached_to_palette(self.creation_properties)

    def _delete_from_context(self) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.activate_delete_text()

    def _replace_from_context(self) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.activate_replace_text()

    def eventFilter(self, watched, event):
        if watched is self.search_field and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._close_find_bar()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._navigate_search(not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                return True
        if (watched in getattr(self, "_text_format_controls", ())
                and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn)):
            self._preserve_text_box_formatting_selection()
        return super().eventFilter(watched, event)

    def _open_find_bar(self) -> None:
        self.find_bar.show()
        view = self._widgets.get(self.manager.active_id)
        with QSignalBlocker(self.search_field):
            self.search_field.setText(view.session.ui.search.query if view is not None else "")
        self._update_search_bar()
        self.search_field.setFocus()

    def _close_find_bar(self) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.close_search()
        self.find_bar.hide()
        self._update_search_bar()

    def _search_changed(self, query: str) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_search_query(query)
        self._update_search_bar()

    def _navigate_search(self, forward: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.navigate_search(forward)
        self._update_search_bar()

    def _update_search_bar(self) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is None:
            self.search_count.setText("0 / 0")
            return
        state = view.session.ui.search
        known = len(state.results)
        current = state.current_result + 1 if state.current_result is not None else 0
        total = str(known) if state.complete else f"{known}+"
        self.search_count.setText(f"{current} / {total}")

    def _preserve_text_box_formatting_selection(self) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.preserve_text_box_formatting_selection()

    def _layout_selected(self, action: QAction) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_layout(action.data())
            self._update_zoom_controls()

    def _zoom(self, increase: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.zoom_in() if increase else view.zoom_out()

    def _pen_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_pen_enabled(enabled)
        self._update_zoom_controls()

    def _pen_color_selected(self, action: QAction) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_pen_color(action.data())
        self._update_zoom_controls()

    def _text_box_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_enabled(enabled)
        self._update_zoom_controls()

    def _callout_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_callout_enabled(enabled)
        self._update_zoom_controls()

    def _delete_text_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.activate_delete_text() if enabled else view.set_delete_text_enabled(False)
        self._update_zoom_controls()

    def _insert_text_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_insert_text_enabled(enabled)
        self._update_zoom_controls()

    def _replace_text_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.activate_replace_text() if enabled else view.set_replace_text_enabled(False)
        self._update_zoom_controls()

    def _text_fill_selected(self, action: QAction) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_fill(action.data())
        self._update_zoom_controls()

    def _text_border_selected(self, action: QAction) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_border(action.data())
        self._update_zoom_controls()

    def _text_color_selected(self, action: QAction) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_text_color(action.data())
        self._update_zoom_controls()

    def _text_font_selected(self, action: QAction) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_font_family(action.data())
        self._update_zoom_controls()

    def _text_size_selected(self, value: int) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_font_size(value)
        self._update_zoom_controls()

    def _text_bold_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_bold(enabled)
        self._update_zoom_controls()

    def _text_italic_toggled(self, enabled: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.set_text_box_italic(enabled)
        self._update_zoom_controls()

    def _update_zoom_controls(self) -> None:
        view = self._widgets.get(self.manager.active_id)
        enabled = (view is not None and view.session.lifecycle == Lifecycle.READY
                   and not view.session.saving)
        self.save_action.setEnabled(enabled)
        self.export_text_action.setEnabled(enabled and not self.text_exports.busy
                                          and self.text_export_dialog is None
                                          and not view.session.pending_mutations
                                          and not self.manager.shutting_down)
        self.review_action.setEnabled(view is not None and view.session.lifecycle == Lifecycle.READY
                                      and not self.manager.shutting_down)
        self.save_as_action.setEnabled(enabled and self.save_dialog is None)
        self.undo_action.setEnabled(enabled and view.session.can_undo)
        self.redo_action.setEnabled(enabled and view.session.can_redo)
        self.zoom_in_action.setEnabled(enabled and view.session.ui.zoom < 8.0)
        self.zoom_out_action.setEnabled(enabled and view.session.ui.zoom > .25)
        self.zoom_label.setText(f"{view.session.ui.zoom:.0%}" if enabled else "—")
        self.layout_button.setEnabled(enabled)
        mode = view.session.ui.layout if enabled else LayoutMode.SINGLE.value
        self.layout_actions[mode].setChecked(True)
        self.layout_button.setText(self.layout_actions[mode].text())
        for action in self.layout_actions.values():
            action.setEnabled(enabled)
        self.highlight_action.setEnabled(
            enabled and view.text_layer.highlight_ready
            and view.pending_highlight_id is None and not view.text_box_enabled)
        self.pen_action.setEnabled(enabled)
        with QSignalBlocker(self.pen_action):
            self.pen_action.setChecked(enabled and view.pen_enabled)
        self.pen_action.setIcon(icon("pen", color=COLORS.accent if self.pen_action.isChecked() else COLORS.primary,
                                     size=24))
        color_name = view.session.ui.pen_color if enabled else "black"
        self.pen_color_actions[color_name].setChecked(True)
        self.text_box_action.setEnabled(enabled)
        with QSignalBlocker(self.text_box_action):
            self.text_box_action.setChecked(enabled and view.text_box_enabled)
        self.text_box_action.setIcon(
            icon("textbox", color=COLORS.accent if self.text_box_action.isChecked() else COLORS.primary, size=24))
        self.callout_action.setEnabled(enabled)
        with QSignalBlocker(self.callout_action):
            self.callout_action.setChecked(enabled and view.callout_enabled)
        self.callout_action.setIcon(
            icon("callout", color=COLORS.accent if self.callout_action.isChecked() else COLORS.primary, size=24))
        self.delete_text_action.setEnabled(enabled)
        with QSignalBlocker(self.delete_text_action):
            self.delete_text_action.setChecked(enabled and view.delete_text_enabled)
        self.insert_text_action.setEnabled(enabled)
        with QSignalBlocker(self.insert_text_action):
            self.insert_text_action.setChecked(enabled and view.insert_text_enabled)
        self.replace_text_action.setEnabled(enabled)
        with QSignalBlocker(self.replace_text_action):
            self.replace_text_action.setChecked(enabled and view.replace_text_enabled)
        if enabled:
            self.text_fill_actions[view.session.ui.text_box_fill].setChecked(True)
            self.text_border_actions[view.session.ui.text_box_border].setChecked(True)
            self.text_color_actions[view.session.ui.text_box_text_color].setChecked(True)
            font_action = self.text_font_actions.get(view.session.ui.text_box_font_family)
            if font_action is not None:
                font_action.setChecked(True)
            self.text_font_button.setText(view.session.ui.text_box_font_family)
            with QSignalBlocker(self.text_size_spin):
                self.text_size_spin.setValue(round(view.session.ui.text_box_font_size))
            with QSignalBlocker(self.text_bold_action):
                self.text_bold_action.setChecked(view.session.ui.text_box_bold)
            with QSignalBlocker(self.text_italic_action):
                self.text_italic_action.setChecked(view.session.ui.text_box_italic)
        # These compatibility controls retain action/menu ownership only; they
        # have no layout and must never reclaim the removed toolbar row.
        for control in self._retired_toolbar_controls:
            control.hide()
        self._update_contextual_annotation_ui(view, enabled)

    def _update_contextual_annotation_ui(self, view: PdfView | None, enabled: bool) -> None:
        self.annotation_palette.setVisible(enabled)
        if not enabled or view is None:
            self.creation_properties.hide()
            self.pdf_text_bar.hide()
            return
        self._clamp_palette_position()
        self.palette_pen_button.setEnabled(enabled)
        self.palette_text_box_button.setEnabled(enabled)
        self.palette_callout_button.setEnabled(enabled)
        self._sync_context_text_controls(view)

        editor = view._text_editor
        text_run_selected = editor is not None and editor.textCursor().hasSelection()
        creation_tool = "pen" if view.pen_enabled else (
            "text" if view.text_box_enabled or view.callout_enabled else None)
        object_context = self._selected_object_context(view)
        pdf_target = view.text_layer.confirmed
        selected_highlight = self._selected_highlight(view)

        # One contextual subject at a time: run formatting wins over object
        # selection, then a creation preset, then the native PDF text target.
        self.pdf_text_bar.setVisible((pdf_target is not None or selected_highlight is not None) and not text_run_selected
                                     and creation_tool is None and object_context is None)
        if self.pdf_text_bar.isVisible():
            self.context_highlight_button.setEnabled(view.text_layer.highlight_ready
                                                     and view.pending_highlight_id is None)
            self.context_delete_button.setEnabled(True)
            self.context_replace_button.setEnabled(True)
            editing_highlight = selected_highlight is not None
            self.context_highlight_button.setVisible(True)
            self.context_highlight_button.setEnabled(editing_highlight or view.text_layer.highlight_ready)
            for control in (self.context_delete_button, self.context_replace_button):
                control.setVisible(True)
                control.setEnabled(not editing_highlight)
            color = (selected_highlight[2] if editing_highlight else
                     dict(HIGHLIGHT_COLOR_PRESETS)[view.session.ui.highlight_color])
            matched = False
            for name, preset in HIGHLIGHT_COLOR_PRESETS:
                checked = color is not None and all(abs(a-b) < .0001 for a, b in zip(
                    (color.red, color.green, color.blue), (preset.red, preset.green, preset.blue)))
                self.highlight_color_actions[name].setChecked(checked)
                self.highlight_color_actions[name].setEnabled(view.session.pending_mutations == 0)
                matched |= checked
            if color is not None:
                self.context_highlight_button.setIcon(fill_swatch_icon(color))
            self.context_highlight_button.setToolTip(
                "Evidenzia — colore personalizzato" if editing_highlight and not matched else "Evidenzia — scegli colore dalla freccia")
            if editing_highlight:
                page, rect = selected_highlight[1]
                self._place_context_bar(self.pdf_text_bar, self._canonical_to_tabs(view, page, rect.x0, rect.y0))
            else:
                self._place_for_pdf_target(self.pdf_text_bar, view, pdf_target)

        if text_run_selected:
            self._show_properties("Formato testo", text_only=True, anchor=self._editor_anchor(view))
        elif editor is not None:
            # Editing is distinct from object selection. With no selected run
            # there is intentionally no competing object/PDF-text bar.
            self.creation_properties.hide()
        elif creation_tool == "pen":
            self._show_properties("Penna", pen_only=True, anchor=self.annotation_palette.pos(),
                                  attached=True)
        elif creation_tool == "text":
            self._show_properties("Nuovo testo", anchor=self.annotation_palette.pos(), attached=True)
        elif object_context is not None:
            label, anchor = object_context
            self._show_properties(label, anchor=anchor)
        else:
            self.creation_properties.hide()

    @staticmethod
    def _selected_highlight(view: PdfView) -> tuple[str, tuple[int, PdfRect], RgbColor | None] | None:
        model = view.session.annotations.get(view.selected_text_markup_id)
        if model is not None and model.kind.value == "highlight" and model.backing is not None:
            points = [p for q in model.target.quads for p in q.points]
            rect = PdfRect(min(p.x for p in points), min(p.y for p in points),
                           max(p.x for p in points), max(p.y for p in points))
            return (f"native:{model.page_index}:{model.backing.xref}",
                    (model.page_index, rect), model.style.color)
        summary = view.session.annotation_inventory.get(view.selected_inventory_id)
        if summary is not None and summary.kind == "highlight" and summary.capabilities.editable_style:
            values = summary.style.get("stroke", [])
            if len(values) == 3:
                color = RgbColor(*values)
            elif len(values) == 1:
                color = RgbColor(*values * 3)
            elif len(values) == 4:
                c, m, y, k = values
                color = RgbColor(1-min(1, c+k), 1-min(1, m+k), 1-min(1, y+k))
            else:
                color = None
            return summary.logical_id, (summary.page_index, summary.geometry.bbox), color
        return None

    def _choose_highlight_color(self, name: str) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is None:
            return
        color = dict(HIGHLIGHT_COLOR_PRESETS)[name]
        selected = self._selected_highlight(view)
        view.session.ui.highlight_color = name
        same_color = (selected is not None and selected[2] is not None and all(
            abs(a-b) < .0001 for a, b in zip(
                (selected[2].red, selected[2].green, selected[2].blue),
                (color.red, color.green, color.blue))))
        if selected is not None and not same_color and view.session.pending_mutations == 0:
            from ..domain.annotation_commands import CommandContext
            from ..domain.annotation_inventory import external_command_annotation_id
            from ..domain.external_edits import EditExternalAnnotation, ExternalEditKind
            logical_id, (page, _rect), _old = selected
            command = EditExternalAnnotation(
                CommandContext.create(view.session.document_id, view.session.document_generation),
                external_command_annotation_id(logical_id), logical_id, page,
                ExternalEditKind.COLOR, color=color)
            self.manager.submit_annotation(command)
        self._update_zoom_controls()

    def _sync_context_text_controls(self, view: PdfView) -> None:
        pen = dict(PEN_COLOR_PRESETS)[view.session.ui.pen_color]
        fill = dict(TEXT_BOX_FILL_PRESETS)[view.session.ui.text_box_fill]
        border = dict(TEXT_BOX_BORDER_PRESETS)[view.session.ui.text_box_border]
        text = dict(PEN_COLOR_PRESETS)[view.session.ui.text_box_text_color]
        self.context_pen_color_button.setIcon(stroke_swatch_icon(pen))
        self.context_pen_color_button.setToolTip("Colore penna")
        self.context_text_fill_button.setIcon(fill_swatch_icon(fill))
        self.context_text_fill_button.setToolTip("Riempimento")
        self.context_text_border_button.setIcon(border_swatch_icon(border))
        self.context_text_border_button.setToolTip("Bordo")
        self.context_text_color_button.setIcon(letter_icon("A", text, bold=True))
        self.context_text_color_button.setToolTip("Colore testo")
        self.text_color_button.setIcon(letter_icon("A", text, bold=True))
        self.context_bold_button.setText("B")
        self.context_bold_button.setToolTip("Grassetto")
        self.context_italic_button.setText("I")
        self.context_italic_button.setToolTip("Corsivo")
        self.context_text_font_button.setText(view.session.ui.text_box_font_family)
        self.context_text_font_button.setToolTip("Famiglia carattere")
        with QSignalBlocker(self.context_text_size_spin):
            self.context_text_size_spin.setValue(round(view.session.ui.text_box_font_size))

    def _show_properties(self, label: str, *, pen_only: bool = False,
                         text_only: bool = False, anchor: QPoint, attached: bool = False) -> None:
        layout = self.creation_properties.layout()
        # Redistribute existing capsule space around the larger B/I targets.
        layout.setContentsMargins(*((6, 4, 6, 4) if pen_only else (5, 1, 5, 1)))
        layout.setSpacing(4 if pen_only else 3)
        self.context_property_label.setText(label)
        self.context_property_label.hide()
        self.context_pen_color_button.setVisible(pen_only)
        for control in self._context_text_controls:
            control.setVisible(not pen_only)
        if text_only:
            self.context_text_fill_button.hide()
            self.context_text_border_button.hide()
        self.creation_properties.adjustSize()
        self.creation_properties.show()
        if attached:
            self._place_attached_to_palette(self.creation_properties)
        else:
            self._place_context_bar(self.creation_properties, anchor)

    def _selected_object_context(self, view: PdfView) -> tuple[str, QPoint] | None:
        annotation_id = view.selected_text_box_id or view.selected_callout_id
        if annotation_id is not None:
            model = view.session.annotations.get(annotation_id)
            if model is not None:
                return "Proprietà oggetto", self._model_anchor(view, model.target)
        # Existing external profiles may permit move/resize or opening the
        # existing text editor, but there is no standalone external-style
        # command. Do not present style controls that would silently do nothing
        # (or imply an unsupported mutation); the profile remains authoritative.
        return None

    def _model_anchor(self, view: PdfView, target) -> QPoint:
        rect = target.rect if hasattr(target, "rect") else target.text_rect
        return self._canonical_to_tabs(view, target.page_index, rect.x0, rect.y0)

    def _editor_anchor(self, view: PdfView) -> QPoint:
        editor = view._text_editor
        if editor is None:
            return self.annotation_palette.pos()
        return view.viewport().mapTo(self.tabs, editor.geometry().topLeft())

    def _place_for_pdf_target(self, bar: QWidget, view: PdfView, target) -> None:
        points = [point for quad in target.quads for point in quad.points]
        x = min(point.x for point in points)
        y = min(point.y for point in points)
        self._place_context_bar(bar, self._canonical_to_tabs(view, target.page_index, x, y))

    def _canonical_to_tabs(self, view: PdfView, page_index: int, x: float, y: float) -> QPoint:
        vx, vy = view.coordinate_mapper(page_index).canonical_to_viewport((x, y))
        return view.viewport().mapTo(self.tabs, QPoint(round(vx), round(vy)))

    def _place_context_bar(self, bar: QWidget, target: QPoint) -> None:
        """Place above a target when possible, below otherwise, without covering it."""
        bar.adjustSize()
        x = max(0, min(self.tabs.width() - bar.width(), target.x()))
        above = target.y() - bar.height() - 8
        y = above if above >= 0 else min(self.tabs.height() - bar.height(), target.y() + 18)
        bar.move(x, max(0, y))

    def _place_attached_to_palette(self, bar: QWidget) -> None:
        """Keep creation presets beside the movable palette rather than over content."""
        bar.adjustSize()
        palette = self.annotation_palette
        view = self._widgets.get(self.manager.active_id)
        tool_button = None
        if view is not None:
            if view.pen_enabled:
                tool_button = self.palette_pen_button
            elif view.text_box_enabled:
                tool_button = self.palette_text_box_button
            elif view.callout_enabled:
                tool_button = self.palette_callout_button
        gap = 2
        right = palette.x() + palette.width() + gap
        x = right if right + bar.width() <= self.tabs.width() else max(0, palette.x() - bar.width() - gap)
        center_y = (palette.y() + tool_button.y() + tool_button.height() // 2
                    if tool_button is not None else palette.y() + palette.height() // 2)
        y = max(0, min(self.tabs.height() - bar.height(), center_y - bar.height() // 2))
        bar.move(x, y)

    def _clamp_palette_position(self) -> None:
        x = max(0, min(self.tabs.width() - self.annotation_palette.width(), self._palette_position.x()))
        y = max(0, min(self.tabs.height() - self.annotation_palette.height(), self._palette_position.y()))
        self.annotation_palette.move(x, y)

    def _highlight(self) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.apply_highlight()
            self._update_zoom_controls()

    def choose_files(self) -> None:
        if self.file_dialog is not None:
            self.file_dialog.raise_()
            return
        dialog = QFileDialog(self, "Apri PDF")
        # Keep the native picker and use open(), not a blocking static/exec call,
        # so Qt timers and document IPC continue while Windows owns its styling.
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setNameFilter("PDF (*.pdf)")
        dialog.filesSelected.connect(self.open_paths)
        dialog.finished.connect(self._dialog_finished)
        self.file_dialog = dialog
        dialog.open()

    def _choose_text_export(self) -> None:
        session = self.manager.sessions.get(self.manager.active_id)
        if session is None or self.text_export_dialog is not None or self.text_exports.busy:
            return
        document_id = session.document_id
        path = str(Path(session.canonical_path).with_suffix(".text.json"))
        dialog = QFileDialog(self, "Esporta testo strutturato", path, "JSON (*.json)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setDefaultSuffix("json")
        self.text_export_dialog = dialog

        def accepted():
            selected = dialog.selectedFiles()
            if selected and self.text_exports.start(document_id, selected[0]):
                self.statusBar().showMessage("Estrazione del testo in corso…")

        def finished(_result):
            self.text_export_dialog = None
            dialog.deleteLater()
            self._update_zoom_controls()

        dialog.accepted.connect(accepted)
        dialog.finished.connect(finished)
        dialog.open()
        self._update_zoom_controls()

    def _open_review(self, document_id: str | None) -> None:
        if document_id is None or self.reviews.closing or self.manager.shutting_down:
            return
        session = self.manager.sessions.get(document_id)
        if ((session is None or session.lifecycle != Lifecycle.READY)
                and document_id not in self.reviews.entries):
            return
        if self.review_dialog is None:
            self.review_dialog = ReviewDialog(self.reviews, self)
        self.review_dialog.show_document(document_id)

    def _update_review_indicator(self) -> None:
        document_id = self.reviews.monitor_document()
        self.review_indicator.setVisible(document_id is not None)
        self.review_indicator.setText("Revisione IA in corso…" if self.reviews.busy(document_id) else "Revisione IA")

    def _save_active(self, save_as: bool) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is None or view.session.saving:
            return
        if not save_as:
            self._save_document(view.session.document_id)
            return
        if self.save_dialog is not None:
            self.save_dialog.raise_()
            return
        document_id = view.session.document_id
        if view._text_editor is not None:
            view._text_editor.retain_next_focus_loss()
        dialog = QFileDialog(self, "Salva PDF con nome", view.session.canonical_path, "PDF (*.pdf)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setDefaultSuffix("pdf")
        # Native asynchronous dialog. Confirmation happens after asynchronous
        # identity resolution, so it covers precisely the reserved destination.
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        dialog.fileSelected.connect(lambda path: self._save_document(document_id, path))
        dialog.finished.connect(self._save_dialog_finished)
        self.save_dialog = dialog
        dialog.open()
        self._update_zoom_controls()

    def _save_document(self, document_id: str, path: str | None = None) -> bool:
        view = self._widgets.get(document_id)
        if view is None or view.session.saving:
            return False
        # The same existing confirmation path commits an active text editor
        # before the barrier. Cancelling Save As never reaches this method.
        if not view.try_confirm_active_editor():
            return False
        view.cancel_pen_gesture()
        view.cancel_ink_move()
        return self.manager.saves.request(document_id, path)

    def _save_dialog_finished(self, result: int) -> None:
        if self.save_dialog is not None:
            self.save_dialog.deleteLater()
            self.save_dialog = None
        self._update_zoom_controls()

    def _confirm_overwrite(self, document_id: str, path: str) -> None:
        dialog = QMessageBox(QMessageBox.Icon.Warning, "Sovrascrivi PDF",
                             f"Sostituire il file esistente?\n{path}",
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.finished.connect(lambda result: self.manager.saves.confirm_overwrite(
            document_id, result == QMessageBox.StandardButton.Yes))
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()

    def _document_reopened(self, document_id: str) -> None:
        view = self._widgets.get(document_id)
        if view is not None:
            view.document_reopened()

    def _save_finished(self, document_id: str, success: bool) -> None:
        if success:
            self.statusBar().showMessage("PDF salvato.", 5000)
        close_requested = document_id in self._close_after_save
        self._close_after_save.discard(document_id)
        if close_requested and success:
            self.manager.request_close(document_id)
            if self._closing_application:
                QTimer.singleShot(0, self.close)
        elif close_requested:
            self._closing_application = False

    def _dialog_finished(self, result: int) -> None:
        if self.file_dialog is not None:
            self.file_dialog.deleteLater()
            self.file_dialog = None

    def open_paths(self, paths: list[str]) -> None:
        self.statusBar().clearMessage()
        for path in paths:
            self.manager.open_path(path)

    def _session_changed(self, document_id: str) -> None:
        session = self.manager.sessions[document_id]
        if session.lifecycle == Lifecycle.CLOSING:
            self._remove_tab(document_id)
            return
        if session.lifecycle not in (Lifecycle.READY, Lifecycle.FAILED):
            return
        widget = self._widgets.get(document_id)
        if widget is None:
            try:
                widget = PdfView(session, self.manager.resident_budget, self.tabs,
                                 text_cache=self.manager.text_cache)
            except (ValueError, OverflowError):
                self._show_error(document_id, ErrorInfo("VIEW_GEOMETRY",
                                 "Le dimensioni del documento superano i limiti del viewer."))
                self.manager.request_close(document_id)
                return
            self._widgets[document_id] = widget
            widget.zoom_changed.connect(self._update_zoom_controls)
            widget.layout_changed.connect(self._update_zoom_controls)
            widget.selection_changed.connect(self._update_zoom_controls)
            widget.highlight_requested.connect(self.manager.submit_annotation)
            widget.ink_requested.connect(self.manager.submit_annotation)
            widget.text_box_requested.connect(self.manager.submit_annotation)
            widget.editorial_requested.connect(self.manager.submit_annotation)
            widget.pen_state_changed.connect(self._update_zoom_controls)
            widget.search_changed.connect(self._update_search_bar)
            widget.inventory_annotation_selected.connect(
                lambda logical_id, key=document_id: self._viewer_annotation_selected(key, logical_id))
            widget.sticky_note_reader.detail_requested.connect(
                lambda logical_id, key=document_id: self.manager.request_annotation_detail(key, logical_id))
            widget.text_error.connect(lambda error, key=document_id:
                                      self._show_error(key, error))
            with QSignalBlocker(self.tabs):
                self.tabs.addTab(widget, "")
            self.manager.start_annotation_inventory(document_id)
        name = Path(session.canonical_path).name
        widget.setEnabled(not session.saving)
        if session.saving:
            widget.sticky_note_reader.dismiss()
        self.tabs.setTabText(self.tabs.indexOf(widget), name + (" *" if session.dirty else ""))
        if session.lifecycle == Lifecycle.FAILED:
            widget.show_failure(session.error.message)
        self._activate_tab(self.manager.active_id)

    def _inventory_changed(self, document_id: str) -> None:
        if document_id == self.manager.active_id:
            self.annotation_sidebar.set_session(self.manager.sessions.get(document_id))
        widget = self._widgets.get(document_id)
        if widget is not None:
            widget.viewport().update()

    def _history_delta_changed(self, document_id: str, delta) -> None:
        session = self.manager.sessions.get(document_id)
        if session is None:
            return
        if document_id == self.manager.active_id:
            self.annotation_sidebar.apply_history_delta(session, delta)
        widget = self._widgets.get(document_id)
        if widget is not None:
            widget.apply_history_delta(delta)
            widget.invalidate_annotation_appearances(delta.appearance_invalidated_ids)
            widget.viewport().update()

    def _annotation_detail_changed(self, document_id: str, logical_id: str) -> None:
        view = self._widgets.get(document_id)
        if view is not None:
            view.external_interaction.detail_ready(logical_id)
            # A text edit can be requested while the sidebar is still loading
            # detail for the previously selected external annotation.  The
            # manager intentionally permits one detail IPC request at a time;
            # once that stale request completes, enqueue the editor's own
            # target rather than leaving its interaction waiting indefinitely.
            waiting = view.external_interaction.waiting_editor
            if (waiting is not None and waiting[1] == view.session.document_generation
                    and waiting[0] != logical_id):
                self.manager.request_annotation_detail(document_id, waiting[0])
        if document_id == self.manager.active_id:
            session = self.manager.sessions.get(document_id)
            if session is not None:
                self.annotation_sidebar.update_detail(session, logical_id)

    def _annotation_detail_failed(self, document_id: str, logical_id: str) -> None:
        view = self._widgets.get(document_id)
        if view is not None:
            view.sticky_note_reader.detail_failed(logical_id)
        if document_id == self.manager.active_id:
            session = self.manager.sessions.get(document_id)
            if session is not None:
                self.annotation_sidebar.show_detail_error(session, logical_id)

    def _sidebar_annotation_activated(self, logical_id: str) -> None:
        view = self._widgets.get(self.manager.active_id)
        if view is not None:
            view.select_inventory_annotation(logical_id, reveal=True)

    def _viewer_annotation_selected(self, document_id: str, logical_id: str) -> None:
        if document_id != self.manager.active_id:
            return
        session = self.manager.sessions.get(document_id)
        if session is not None:
            self.annotation_sidebar.select_annotation(session, logical_id)

    def _sidebar_detail_requested(self, logical_id: str) -> None:
        if self.manager.active_id is not None:
            self.manager.request_annotation_detail(self.manager.active_id, logical_id)

    def _remove_tab(self, document_id: str) -> None:
        widget = self._widgets.pop(document_id, None)
        if widget is not None:
            widget.set_active(False)
            widget.text_layer.clear()
            widget.cancel_pen_gesture()
            widget.cancel_ink_move()
            widget.cancel_text_box_edit()
            widget._clear_editorial_interaction()
            widget.pending_inks.clear()
            widget.pending_ink_moves.clear()
            widget.pending_highlight_id = None
            widget.scheduler.clear()
            with QSignalBlocker(self.tabs):
                self.tabs.removeTab(self.tabs.indexOf(widget))
            widget.deleteLater()

    def _activate_tab(self, document_id: str | None) -> None:
        # Release the old tab's owned rasters before activating the new tab.
        for key, view in self._widgets.items():
            if key != document_id:
                view.set_active(False)
        widget = self._widgets.get(document_id)
        self.page_count_label.clear()
        if widget is not None:
            with QSignalBlocker(self.tabs):
                self.tabs.setCurrentWidget(widget)
            widget.set_active(True)
            self.annotation_sidebar.set_session(widget.session)
            if widget.session.lifecycle == Lifecycle.READY:
                self.page_count_label.setText(f"{widget.session.page_count} pagine")
        else:
            self.annotation_sidebar.set_session(None)
        if self.find_bar.isVisible():
            with QSignalBlocker(self.search_field):
                self.search_field.setText(widget.session.ui.search.query if widget is not None else "")
        self._update_search_bar()
        self._update_zoom_controls()

    def _render_tick(self, document_id: str) -> None:
        widget = self._widgets.get(document_id)
        if widget is not None:
            widget.tick()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "annotation_palette"):
            self._clamp_palette_position()

    def _tab_activated(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is not None:
            self.manager.activate(widget.property("document_id"))

    def _tab_closed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is not None:
            document_id = widget.property("document_id")
            session = self.manager.sessions.get(document_id)
            if session is None:
                return
            if session.saving:
                self.statusBar().showMessage("Attendi la fine del salvataggio prima di chiudere.")
                return
            self._resolve_close(document_id)

    def _resolve_close(self, document_id: str) -> bool:
        session = self.manager.sessions[document_id]
        if self.reviews.busy(document_id) and not self._confirm_review_close(session):
            self._closing_application = False
            return False
        view = self._widgets.get(document_id)
        if view is not None and not view.try_confirm_active_editor():
            return False
        if not session.dirty:
            return self.manager.request_close(document_id)
        choice = self._close_choice(session)
        if choice == QMessageBox.StandardButton.Save:
            self._close_after_save.add(document_id)
            if not self._save_document(document_id):
                self._close_after_save.discard(document_id)
            elif self.reviews.busy(document_id):
                self.reviews.stop(document_id)
            return False
        if choice == QMessageBox.StandardButton.Discard:
            return self.manager.request_close(document_id, discard_changes=True)
        self._closing_application = False
        return False

    def _close_choice(self, session) -> QMessageBox.StandardButton:
        dialog = QMessageBox(QMessageBox.Icon.Warning, "Modifiche non salvate",
                             f"Salvare le modifiche a {Path(session.canonical_path).name}?",
                             QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                             | QMessageBox.StandardButton.Cancel, self)
        dialog.setDefaultButton(QMessageBox.StandardButton.Save)
        return dialog.exec()

    def _confirm_review_close(self, session) -> bool:
        dialog = QMessageBox(QMessageBox.Icon.Warning, "Revisione in corso",
                             "Chiudere il PDF interromperà la revisione di "
                             f"{Path(session.canonical_path).name}. I risultati già acquisiti resteranno nell’archivio.",
                             QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel, self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return dialog.exec() == QMessageBox.StandardButton.Close

    def _show_error(self, document_id: str, error: ErrorInfo) -> None:
        self.statusBar().showMessage(error.message)

    def _annotation_result(self, result) -> None:
        widget = self._widgets.get(result.document_id)
        if widget is not None:
            widget.handle_annotation_result(result)
        if not result.success and result.error is not None:
            self.statusBar().showMessage(result.error.message)
        self._update_zoom_controls()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.manager.is_shutdown:
            self.reviews.shutdown()
            if self.review_dialog is not None:
                self.review_dialog.close()
            if self.reviews.is_shutdown:
                event.accept()
            else:
                event.ignore()
            return
        event.ignore()
        if self.manager.shutting_down:
            return
        if any(session.saving for session in self.manager.sessions.values()):
            self.statusBar().showMessage("Attendi la fine del salvataggio prima di chiudere.")
            return
        self._closing_application = True
        for document_id, session in tuple(self.manager.sessions.items()):
            if session.lifecycle not in (Lifecycle.CLOSING, Lifecycle.CLOSED):
                if not self._resolve_close(document_id):
                    return
        if self.manager.request_shutdown():
            self.reviews.shutdown()
            self.open_action.setEnabled(False)
            self.review_action.setEnabled(False)
            if self.review_dialog is not None:
                self.review_dialog.close()
            if self.file_dialog is not None:
                self.file_dialog.reject()
            if self.save_dialog is not None:
                self.save_dialog.reject()
