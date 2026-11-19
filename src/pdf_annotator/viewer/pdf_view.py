"""Virtual PDF viewer with M6 text selection and M7 Ink overlays."""

from collections import deque
from dataclasses import dataclass, replace
from html import escape
from math import atan2, ceil, cos, floor, isfinite, sin
from time import perf_counter

from PySide6.QtCore import QEvent, QMimeData, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QPolygonF,
                           QTextCharFormat, QTextCursor, QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QMenu, QPlainTextEdit,
                               QSizePolicy, QToolTip, QWidget)

from ..app.document_session import DocumentSession, Lifecycle
from ..domain.annotation_commands import (CommandContext, CommandSource,
                                           CreateHighlight, CreateInk,
                                           CreateFreeText, DeleteAnnotation, MoveFreeText, MoveInk,
                                           ResizeFreeText, TransferFreeText, UpdateFreeText,
                                           CreateCallout, MoveCallout, MoveCalloutAnchor,
                                           ResizeCallout, TransferCallout, UpdateCallout,
                                           CreateDeleteText, CreateInsertText, CreateReplaceText,
                                           DeleteExternalAnnotation)
from ..domain.annotation_models import AnnotationOwnership
from ..domain.annotation_inventory import AnnotationSummary, external_command_annotation_id, is_attached_reply
from ..domain.callout_geometry import callout_line
from ..domain.annotation_clipboard import (ANNOTATION_CLIPBOARD_MIME,
                                            AnnotationClipboardPayload)
from ..domain.geometry import PageBounds, PdfPoint, PdfQuad, PdfRect
from ..domain.kinds import AnnotationKind
from ..domain.rich_text import (RichText, RichTextFormatDelta, RichTextRun,
                                apply_rich_text_format_delta)
from ..domain.results import MutationResult
from ..domain.styles import (PEN_COLOR_PRESETS, TEXT_BOX_BORDER_PRESETS,
                             TEXT_BOX_FILL_PRESETS, FREE_TEXT_FONT_FAMILIES,
                             FreeTextStyle, HighlightStyle, InkStyle, RgbColor, HIGHLIGHT_COLOR_PRESETS)
from .sticky_notes import StickyNoteReader, note_icon_rect, paint_note_icon

# These are application-private files, never Windows aliases. RichText keeps
# its compact Base-14 semantic roles while the external AP child embeds these
# exact physical families.
_PDF_TO_EDITOR_FONT = {"Helvetica": "Source Sans 3", "Times": "Courier Prime",
                       "Courier": "Courier Prime"}
_EDITOR_TO_PDF_FONT = {value.casefold(): key for key, value in _PDF_TO_EDITOR_FONT.items()}
_EDITOR_TO_PDF_FONT["courier prime"] = "Courier"


def _editor_font_family(pdf_family: str) -> str:
    return _PDF_TO_EDITOR_FONT.get(pdf_family, pdf_family)


def _pdf_font_family(editor_family: str) -> str:
    return _EDITOR_TO_PDF_FONT.get(editor_family.casefold(), editor_family)
from ..domain.targets import (CalloutTarget, InkTarget, RectTarget, TextInsertionTarget, TextRangeTarget, clamp_ink_translation,
                              translate_ink_target)
from ..services.text_cache import TextSnapshotCache
from ..worker.protocol import Operation
from .coordinate_mapper import CoordinateMapper
from .layout_engine import BASE_SCALE, LayoutMode, PageLayout
from .pen_tool import PenGestureController, PenGestureMetrics
from .render_scheduler import RenderScheduler, ResidentBudget, requested_raster_scale
from .raster_cache import RenderKey
from .raster_image import adopt_raster
from .text_selection import TextSelectionController
from .search import DocumentSearchController
from .formatting_text_edit import FormattingTextEdit

MIN_ZOOM = 0.25
MAX_ZOOM = 8.0
TEXT_BOX_MIN_SIZE = 12.0
TEXT_BOX_RESIZE_HANDLE_HIT_RADIUS = 16.0
CALLOUT_ANCHOR_HIT_RADIUS = 30.0
INSERT_CARET_HIT_RADIUS = 14.0
TEXT_BOX_CONTENT_PADDING = 3.0
CALLOUT_INITIAL_WIDTH = 70.0
CALLOUT_INITIAL_WIDTH_SCALE = 2.5
CALLOUT_ANCHOR_CLEARANCE = 24.0
# Text-markup comments intentionally share selection/delete semantics without
# gaining the move/resize behavior reserved for geometry annotations. Future
# editorial text-markup kinds can join this set without changing input flow.
NON_MOVABLE_TEXT_MARKUP_KINDS = frozenset((AnnotationKind.HIGHLIGHT,
                                            AnnotationKind.DELETE_TEXT,
                                            AnnotationKind.REPLACE_TEXT))
RICH_COLOR_PROPERTY = QTextCharFormat.Property.UserProperty + 1


def _uses_inventory_overlay(summary: AnnotationSummary) -> bool:
    # Both ownership classes use the M18 icon and reader for native /Text.
    return summary.provenance != "app" or summary.kind == "sticky_note"


@dataclass(frozen=True, slots=True)
class _PendingInk:
    target: InkTarget
    style: InkStyle
    submitted_at: float


@dataclass
class _InkMove:
    annotation_id: str
    target: InkTarget
    style: InkStyle
    start: PdfPoint
    delta_x: float = 0.0
    delta_y: float = 0.0
    dragging: bool = False

    def preview_target(self) -> InkTarget:
        return translate_ink_target(self.target, self.delta_x, self.delta_y)


@dataclass(frozen=True, slots=True)
class _PendingInkMove:
    move: _InkMove
    submitted_at: float


@dataclass
class _FreeTextMove:
    annotation_id: str
    target: RectTarget
    start: PdfPoint
    resizing_corner: str | None = None
    rect: PdfRect | None = None
    preview_page_index: int | None = None
    dragging: bool = False


@dataclass
class _CalloutMove:
    annotation_id: str
    target: CalloutTarget
    start: PdfPoint
    action: str
    rect: PdfRect | None = None
    anchor: PdfPoint | None = None
    preview_page_index: int | None = None
    dragging: bool = False


@dataclass(frozen=True, slots=True)
class _TextMarkupClick:
    annotation_id: str
    page_index: int
    start: PdfPoint
    viewport_start: QPointF


@dataclass(frozen=True, slots=True)
class _TextCaretClick:
    """A pressed native-text click that becomes a caret only on release."""
    page_index: int
    start: PdfPoint
    viewport_start: QPointF


@dataclass(frozen=True, slots=True)
class _PendingFreeText:
    target: RectTarget | CalloutTarget
    text: str
    rich_text: RichText
    style: FreeTextStyle
    annotation_id: str | None
    submitted_at: float
    restore_editor: bool


@dataclass(frozen=True, slots=True)
class _PendingEditorial:
    kind: AnnotationKind
    target: TextRangeTarget | TextInsertionTarget
    submitted_at: float
    text: str | None = None


@dataclass(frozen=True, slots=True)
class _AppearanceTicket:
    key: RenderKey
    logical_id: str
    request_id: int
    epoch: int
    max_bytes: int
    source_summary: AnnotationSummary


@dataclass(frozen=True, slots=True)
class _NativeAppearancePaint:
    """One inspectable native-AP paint decision for focused renderer tests."""
    annotation_id: str
    document_revision: int
    cache_key: RenderKey
    source_key: RenderKey
    source: str
    pixel_size: tuple[int, int]
    appearance_coverage: tuple[float, float, float, float]
    annotation_rect: PdfRect
    callout_line: tuple[PdfPoint, ...]
    clip_to: PdfRect | None
    destination: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class _CalloutLeaderPaint:
    """Inspectable bounds for the Qt-only Callout leader overlay."""
    arrow_tip: PdfPoint
    paint_bounds: tuple[float, float, float, float]
    arrow_bounds: tuple[float, float, float, float]
    overlay_bounds: tuple[float, float, float, float]


class _EditorialTextEditor(QPlainTextEdit):
    confirmed = Signal()
    cancelled = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.confirmed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _TextBoxEditor(FormattingTextEdit):
    confirmed = Signal()
    cancelled = Signal()
    auto_confirm_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # The product editor is plain text. QTextEdit otherwise imports hidden
        # HTML font metadata from the clipboard, which can put an unsupported
        # family into the document even though the visible text is valid.
        self.setAcceptRichText(False)
        self._retain_next_focus_loss = False

    def retain_next_focus_loss(self) -> None:
        """Keep an edit live while a toolbar control is invoked."""
        self._retain_next_focus_loss = True

    def clear_retained_focus_loss(self) -> None:
        self._retain_next_focus_loss = False

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            # A newly drawn FreeText/Callout is only a GUI draft until its
            # normal Ctrl+Enter confirmation creates a command. QTextEdit has
            # no local undo step for an empty draft, which otherwise lets this
            # shortcut fall through to MainWindow's document-history action.
            # Cancelling here is a real interaction transition, not disabling
            # or silently swallowing Undo. Nonempty text always retains the
            # editor's ordinary local Undo precedence.
            if not self.toPlainText():
                self.cancelled.emit()
            else:
                super().keyPressEvent(event)
            event.accept()
            return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.confirmed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if self._retain_next_focus_loss:
            self._retain_next_focus_loss = False
            return
        self.auto_confirm_requested.emit()


class PdfView(QAbstractScrollArea):
    zoom_changed = Signal(float)
    layout_changed = Signal(str)
    selection_changed = Signal()
    highlight_requested = Signal(object)
    ink_requested = Signal(object)
    text_box_requested = Signal(object)
    editorial_requested = Signal(object)
    pen_state_changed = Signal()
    text_error = Signal(object)
    inventory_annotation_selected = Signal(str)
    search_changed = Signal()

    def __init__(self, session: DocumentSession, budget: ResidentBudget,
                 parent=None, *, nearby_rows: int = 1,
                 text_cache: TextSnapshotCache | None = None) -> None:
        if type(nearby_rows) is not int or not 0 <= nearby_rows <= 4:
            raise ValueError("Invalid nearby row count")
        layout = PageLayout(session.pages, session.ui.zoom * BASE_SCALE, session.ui.layout)
        super().__init__(parent)
        self.session = session
        self.setProperty("document_id", session.document_id)
        self.page_layout = layout
        self.nearby_rows = nearby_rows
        self._relayout = False
        self._layout_anchor: tuple[int, tuple[float, float]] | None = None
        self._direction = 1
        self._last_scroll = perf_counter()
        self._last_y = 0
        self.error_message = ""
        self._active = False
        self.scheduler = RenderScheduler(session, budget, self.viewport().update)
        self._appearance_pending: _AppearanceTicket | None = None
        self._appearance_epoch = 0
        self._appearance_scheduler_epoch = self.scheduler.epoch
        self._appearance_errors: set[RenderKey] = set()
        # A monotonic document revision cannot identify a restored history
        # state on its own. Keep the worker snapshot that produced each AP.
        self._appearance_sources: dict[RenderKey, AnnotationSummary] = {}
        self._history_appearance_invalidations: set[str] = set()
        self._history_native_appearance_pending: set[str] = set()
        self._last_native_appearance_paint: _NativeAppearancePaint | None = None
        self._last_callout_leader_paint: _CalloutLeaderPaint | None = None
        self.text_layer = TextSelectionController(
            session, text_cache or TextSnapshotCache(),
            self._selection_changed, self._text_failed)
        self.search_layer = DocumentSearchController(session, self._search_changed,
                                                     self._text_failed)
        self.pending_highlight_id: str | None = None
        self.pen_gesture = PenGestureController()
        self.pending_inks: dict[str, _PendingInk] = {}
        self.pending_ink_moves: dict[str, _PendingInkMove] = {}
        self.selected_ink_id: str | None = None
        self.selected_text_markup_id: str | None = None
        self._text_markup_click: _TextMarkupClick | None = None
        self._text_caret_click: _TextCaretClick | None = None
        self._insert_context_menu: QMenu | None = None
        self._insert_context_menu_generation = 0
        self.pending_delete_id: str | None = None
        self.selected_text_box_id: str | None = None
        self.selected_callout_id: str | None = None
        self.selected_inventory_id: str | None = session.selected_annotation_id
        from .external_interaction import ExternalInteraction
        self.external_interaction = ExternalInteraction(self)
        self.sticky_note_reader = StickyNoteReader(self)
        self.pending_text_boxes: dict[str, _PendingFreeText] = {}
        self.pending_editorials: dict[str, _PendingEditorial] = {}
        self._editorial_editor: _EditorialTextEditor | None = None
        self._editing_editorial_target: TextRangeTarget | TextInsertionTarget | None = None
        self._editing_editorial_kind: AnnotationKind | None = None
        self._awaiting_insert_editor = False
        self._awaiting_range_action: AnnotationKind | None = None
        self._hover_editorial_id: str | None = None
        self._text_box_drag: tuple[int, PdfPoint, PdfPoint] | None = None
        self._text_box_move: _FreeTextMove | None = None
        self._callout_anchor: tuple[int, PdfPoint] | None = None
        self._callout_drag: tuple[int, PdfPoint, PdfPoint] | None = None
        self._callout_move: _CalloutMove | None = None
        self._text_editor: _TextBoxEditor | None = None
        self._editing_text_box_id: str | None = None
        self._editing_target: RectTarget | CalloutTarget | None = None
        self._editing_style: FreeTextStyle | None = None
        self._editing_rich_text: RichText | None = None
        self._formatting_cursor: QTextCursor | None = None
        self._use_saved_formatting_cursor = False
        self._clipboard_paste_bytes: bytes | None = None
        self._clipboard_paste_count = 0
        self.paste_target_page: int | None = None
        self._paste_target_candidate: tuple[int, QPointF] | None = None
        self._pending_paste_ids: dict[str, str] = {}
        self._ink_move: _InkMove | None = None
        # Ctrl+left-drag is a transient viewport-only override. It starts
        # before every PDF gesture, then uses the scroll bars after Qt's normal
        # drag threshold without changing the selected tool or PDF geometry.
        self._pan_start: QPointF | None = None
        self._pan_scroll_start: tuple[int, int] | None = None
        self._pan_active = False
        self._pan_consumed = False
        self._gesture_style: InkStyle | None = None
        self.last_pen_metrics = PenGestureMetrics(0, 0)
        self.pen_move_ms: deque[float] = deque(maxlen=2048)
        self.pen_release_enqueue_ms: deque[float] = deque(maxlen=256)
        self.pen_roundtrip_ms: deque[float] = deque(maxlen=256)
        self.ink_move_ms: deque[float] = deque(maxlen=2048)
        self.ink_move_release_enqueue_ms: deque[float] = deque(maxlen=256)
        self.ink_move_roundtrip_ms: deque[float] = deque(maxlen=256)
        self.text_box_move_ms: deque[float] = deque(maxlen=2048)
        self.text_box_release_enqueue_ms: deque[float] = deque(maxlen=256)
        self.text_box_roundtrip_ms: deque[float] = deque(maxlen=256)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # QAbstractScrollArea receives viewport mouse moves only when the
        # viewport itself tracks them; hover must not require a pressed button.
        self.viewport().setMouseTracking(True)
        # QAbstractScrollArea can route a platform context-menu event to either
        # itself or its viewport. Intercept the viewport event explicitly: the
        # CustomContextMenu signal alone was not reliably emitted for a native
        # right-click on this scroll-area surface.
        self.viewport().installEventFilter(self)
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setSingleStep(40)
        self._update_ranges()

    @property
    def pen_enabled(self) -> bool:
        return self.session.ui.pen_enabled

    @property
    def text_box_enabled(self) -> bool:
        return self.session.ui.text_box_enabled

    @property
    def callout_enabled(self) -> bool:
        return self.session.ui.callout_enabled

    @property
    def delete_text_enabled(self) -> bool:
        return self.session.ui.delete_text_enabled

    @property
    def insert_text_enabled(self) -> bool:
        return self.session.ui.insert_text_enabled

    @property
    def replace_text_enabled(self) -> bool:
        return self.session.ui.replace_text_enabled

    @property
    def text_box_style(self) -> FreeTextStyle:
        return FreeTextStyle(dict(TEXT_BOX_BORDER_PRESETS)[self.session.ui.text_box_border],
                             dict(TEXT_BOX_FILL_PRESETS)[self.session.ui.text_box_fill],
                             dict(PEN_COLOR_PRESETS)[self.session.ui.text_box_text_color],
                             font_size=self.session.ui.text_box_font_size,
                             font_family=self.session.ui.text_box_font_family)

    @property
    def pen_style(self) -> InkStyle:
        colors = dict(PEN_COLOR_PRESETS)
        return InkStyle(colors.get(self.session.ui.pen_color, colors["black"]))

    def set_pen_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled) and self.session.lifecycle == Lifecycle.READY
        if enabled == self.session.ui.pen_enabled:
            return
        self.external_interaction.cancel_gesture()
        if not enabled:
            self.cancel_pen_gesture()
        else:
            self._clear_editorial_interaction(preserve_confirmed_selection=True)
            self._disable_editorial_tools()
            self.cancel_ink_move()
            self.set_text_box_enabled(False)
        self.session.ui.pen_enabled = enabled
        if enabled:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.viewport().unsetCursor()
        self.pen_state_changed.emit()

    def set_text_box_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled) and self.session.lifecycle == Lifecycle.READY
        if enabled == self.session.ui.text_box_enabled:
            return True
        if self._text_editor is not None and not self.try_confirm_text_box_editor():
            return False
        self.external_interaction.cancel_gesture()
        if enabled:
            self._clear_editorial_interaction(preserve_confirmed_selection=True)
            self._disable_editorial_tools()
            if not self.set_callout_enabled(False):
                return False
            self.set_pen_enabled(False)
            self.cancel_ink_move()
        else:
            self._text_box_drag = None
        self.session.ui.text_box_enabled = enabled
        if enabled:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        elif not self.pen_enabled:
            self.viewport().unsetCursor()
        self.pen_state_changed.emit()
        self.viewport().update()
        return True

    def set_callout_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled) and self.session.lifecycle == Lifecycle.READY
        if enabled == self.session.ui.callout_enabled:
            return True
        if self._text_editor is not None and not self.try_confirm_text_box_editor():
            return False
        self.external_interaction.cancel_gesture()
        if enabled:
            self._clear_editorial_interaction(preserve_confirmed_selection=True)
            self._disable_editorial_tools()
            if not self.set_text_box_enabled(False):
                return False
            self.set_pen_enabled(False)
            self.cancel_ink_move()
        else:
            self._callout_anchor = None
            self._callout_drag = None
        self.session.ui.callout_enabled = enabled
        if enabled:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        elif not self.pen_enabled and not self.text_box_enabled:
            self.viewport().unsetCursor()
        self.pen_state_changed.emit()
        self.viewport().update()
        return True

    def _clear_editorial_interaction(self, *, preserve_confirmed_selection: bool = False) -> None:
        self._hide_editorial_tooltip()
        self._awaiting_insert_editor = False
        self._awaiting_range_action = None
        if self._editorial_editor is not None:
            self._editorial_editor.deleteLater()
        self._editorial_editor = None
        self._editing_editorial_target = None
        self._editing_editorial_kind = None
        # Legacy tools may cancel a transient editorial editor/gesture without
        # invalidating the independent confirmed M6 range that M7 preserves.
        # Editorial activation itself keeps the default destructive behavior.
        if not (preserve_confirmed_selection and self.text_layer.confirmed is not None):
            self.text_layer.clear()

    def _disable_editorial_tools(self) -> None:
        self.session.ui.delete_text_enabled = False
        self.session.ui.insert_text_enabled = False
        self.session.ui.replace_text_enabled = False

    def _set_editorial_tool(self, attribute: str, enabled: bool, *,
                            preserve_selection: bool = False) -> None:
        enabled = bool(enabled) and self.session.lifecycle == Lifecycle.READY
        if enabled and getattr(self.session.ui, attribute):
            return
        if enabled:
            self.cancel_pen_gesture()
            self.cancel_ink_move()
            self.cancel_text_box_edit()
            self.set_pen_enabled(False)
            self.set_text_box_enabled(False)
            self.set_callout_enabled(False)
            if not preserve_selection:
                self._clear_editorial_interaction()
            self._disable_editorial_tools()
        elif getattr(self.session.ui, attribute):
            self._clear_editorial_interaction()
        setattr(self.session.ui, attribute, enabled)
        if enabled:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        elif not (self.pen_enabled or self.text_box_enabled or self.callout_enabled):
            self.viewport().unsetCursor()
        self.pen_state_changed.emit()
        self.viewport().update()

    def set_delete_text_enabled(self, enabled: bool) -> None:
        self._set_editorial_tool("delete_text_enabled", enabled)

    def set_insert_text_enabled(self, enabled: bool) -> None:
        self._set_editorial_tool("insert_text_enabled", enabled)

    def set_replace_text_enabled(self, enabled: bool) -> None:
        self._set_editorial_tool("replace_text_enabled", enabled)

    def activate_delete_text(self) -> None:
        """Toolbar activation consumes a confirmed M6 range before clearing it."""
        target = self.text_layer.confirmed
        self._set_editorial_tool("delete_text_enabled", True,
                                preserve_selection=target is not None)
        if target is not None:
            self._queue_editorial(AnnotationKind.DELETE_TEXT, target)

    def activate_replace_text(self) -> None:
        """Toolbar activation opens the plain editor from the exact M6 range."""
        target = self.text_layer.confirmed
        self._set_editorial_tool("replace_text_enabled", True,
                                preserve_selection=target is not None)
        if target is not None:
            self._begin_editorial_editor(AnnotationKind.REPLACE_TEXT, target)

    def set_text_box_fill(self, name: str) -> None:
        if name not in dict(TEXT_BOX_FILL_PRESETS):
            raise ValueError("Unknown text-box fill")
        self.session.ui.text_box_fill = name
        if self._editing_style is not None:
            self._editing_style = replace(self._editing_style,
                                          fill=dict(TEXT_BOX_FILL_PRESETS)[name])
            self._apply_text_editor_style()
        else:
            self._update_selected_text_style(fill=dict(TEXT_BOX_FILL_PRESETS)[name])
        self.viewport().update()

    def set_text_box_border(self, name: str) -> None:
        if name not in dict(TEXT_BOX_BORDER_PRESETS):
            raise ValueError("Unknown text-box border")
        self.session.ui.text_box_border = name
        if self._editing_style is not None:
            self._editing_style = replace(self._editing_style,
                                          border=dict(TEXT_BOX_BORDER_PRESETS)[name])
            self._apply_text_editor_style()
        else:
            self._update_selected_text_style(border=dict(TEXT_BOX_BORDER_PRESETS)[name])
        self.viewport().update()

    def set_text_box_text_color(self, name: str) -> None:
        if name not in dict(PEN_COLOR_PRESETS):
            raise ValueError("Unknown text-box text color")
        self.session.ui.text_box_text_color = name
        if self._editing_style is not None:
            self._apply_editor_character_format(color=dict(PEN_COLOR_PRESETS)[name])
        else:
            self._update_selected_text_style(delta=RichTextFormatDelta(
                color=dict(PEN_COLOR_PRESETS)[name]))
        self.viewport().update()

    def set_text_box_font_family(self, family: str) -> None:
        if family not in ("Helvetica", "Courier"):
            raise ValueError("Unknown text-box font family")
        self.session.ui.text_box_font_family = family
        if self._editing_style is not None:
            self._apply_editor_character_format(font_family=family)
        else:
            self._update_selected_text_style(delta=RichTextFormatDelta(font_family=family))
        self.viewport().update()

    def set_text_box_font_size(self, size: int | float) -> None:
        if type(size) not in (int, float) or not 6 <= float(size) <= 72:
            raise ValueError("Invalid text-box font size")
        self.session.ui.text_box_font_size = float(size)
        if self._editing_style is not None:
            self._apply_editor_character_format(font_size=float(size))
        else:
            self._update_selected_text_style(delta=RichTextFormatDelta(font_size=float(size)))
        self.viewport().update()

    def set_text_box_bold(self, enabled: bool) -> None:
        self.session.ui.text_box_bold = bool(enabled)
        if self._editing_style is not None:
            self._apply_editor_character_format(bold=bool(enabled))
        else:
            self._update_selected_text_style(delta=RichTextFormatDelta(bold=bool(enabled)))
        self.viewport().update()

    def set_text_box_italic(self, enabled: bool) -> None:
        self.session.ui.text_box_italic = bool(enabled)
        if self._editing_style is not None:
            self._apply_editor_character_format(italic=bool(enabled))
        else:
            self._update_selected_text_style(delta=RichTextFormatDelta(italic=bool(enabled)))
        self.viewport().update()

    def preserve_text_box_formatting_selection(self) -> None:
        """Remember a real editor range before a toolbar control takes focus."""
        if self._text_editor is None:
            return
        self._formatting_cursor = QTextCursor(self._text_editor.textCursor())
        self._use_saved_formatting_cursor = True
        self._text_editor.retain_next_focus_loss()

    def set_pen_color(self, name: str) -> None:
        if name not in dict(PEN_COLOR_PRESETS):
            raise ValueError("Unknown pen color")
        if name != self.session.ui.pen_color:
            self.session.ui.pen_color = name
            self.pen_state_changed.emit()
            self.viewport().update()

    def cancel_pen_gesture(self) -> None:
        if not self.pen_gesture.active:
            return
        self.pen_gesture.cancel()
        self._gesture_style = None
        self.viewport().update()

    def cancel_ink_move(self) -> None:
        if self._ink_move is None:
            return
        self._ink_move = None
        self.viewport().update()

    def cancel_text_box_edit(self) -> None:
        pending_creation = self._text_editor is not None and self._editing_text_box_id is None
        self.external_interaction.cancel_gesture()
        self._text_box_drag = None
        self._text_box_move = None
        self._callout_drag = None
        self._callout_move = None
        if self._text_editor is not None:
            editor = self._text_editor
            editor.clear_formatting_ranges()
            # A replacement editor can take focus before Qt destroys this one.
            # Do not let that deferred focus-out auto-confirm the restored
            # editor a second time (notably after a synchronous rejection).
            editor.blockSignals(True)
            # deleteLater alone leaves an invisible editor as Qt's focus owner
            # until the next event turn.  A local fit rejection has no worker
            # callback to do another recovery pass, so that stale overlay can
            # intercept Escape and mouse input in the meantime.
            if QWidget.mouseGrabber() is editor:
                editor.releaseMouse()
            editor.hide()
            editor.clearFocus()
            self.viewport().setFocus(Qt.FocusReason.OtherFocusReason)
            editor.deleteLater()
        self._text_editor = None
        self._editing_text_box_id = None
        self._editing_target = None
        self._editing_style = None
        self._editing_rich_text = None
        self._formatting_cursor = None
        self._use_saved_formatting_cursor = False
        if pending_creation:
            # Draft geometry belongs solely to the editor state; no command,
            # annotation model, optimistic pending entry or history step exists
            # to undo. Clear selection/context ownership without changing the
            # active creation tool, so the next draw remains immediately ready.
            self._set_selected_ink(None)
            self._set_selected_text_markup(None)
            self._set_selected_text_box(None)
            self._set_selected_callout(None)
            self._set_selected_inventory(None)
            self.text_layer.clear()
        self.selection_changed.emit()
        self.viewport().update()

    def delete_selected_ink(self) -> None:
        if self.selected_ink_id is None or self.pending_delete_id is not None:
            return
        model = self.session.annotations.get(self.selected_ink_id)
        if (model is None or model.kind != AnnotationKind.INK
                or model.ownership != AnnotationOwnership.CREATED_BY_APP):
            self.selected_ink_id = None
            self.viewport().update()
            return
        command = DeleteAnnotation(
            CommandContext.create(self.session.document_id,
                                  self.session.document_generation,
                                  CommandSource.UI),
            model.annotation_id, model.page_index)
        self.pending_delete_id = command.context.command_id
        self.ink_requested.emit(command)

    def _origin(self) -> tuple[float, float]:
        center = max(0.0, (self.viewport().width() - self.page_layout.width) / 2)
        return self.horizontalScrollBar().value() - center, self.verticalScrollBar().value()

    def page_viewport_rect(self, index: int) -> QRectF:
        page = self.page_layout.pages[index]
        x, y = self._origin()
        return QRectF(page.x - x, page.y - y, page.width, page.height)

    def coordinate_mapper(self, index: int) -> CoordinateMapper:
        page = self.page_layout.pages[index]
        return CoordinateMapper(self.session.pages[index], self.session.ui.zoom * BASE_SCALE,
                                (page.x, page.y), self._origin())

    def _reading_anchor(self, focus: QPointF | None = None) -> tuple[int, tuple[float, float], QPointF]:
        """Choose the nearest visible page to the focus, then a point on that page.

        Ties prefer the larger visible area, then the left/earlier page. Clamping
        to the visible page intersection avoids anchoring the gutter or the blank
        space below a short page, and correctly identifies right-hand pages.
        """
        if focus is None and self._layout_anchor is not None:
            index, canonical = self._layout_anchor
            point = QPointF(*self.coordinate_mapper(index).canonical_to_viewport(canonical))
            if QRectF(self.viewport().rect()).adjusted(-1, -1, 1, 1).contains(point):
                return index, canonical, point
        focus = focus or QPointF(self.viewport().width() / 2, self.viewport().height() / 2)
        x, y = self._origin()
        viewport = QRectF(self.viewport().rect())
        visible = self.page_layout.visible(x, y, viewport.width(), viewport.height())

        def distance(index: int) -> tuple[float, float, int]:
            rect = self.page_viewport_rect(index).intersected(viewport)
            dx = max(rect.left() - focus.x(), 0, focus.x() - rect.right())
            dy = max(rect.top() - focus.y(), 0, focus.y() - rect.bottom())
            return dx * dx + dy * dy, -rect.width() * rect.height(), index

        index = (min(visible, key=distance) if visible else
                 self.page_layout.page_at(y + focus.y(), x + focus.x()))
        rect = self.page_viewport_rect(index)
        if visible:
            rect = rect.intersected(viewport)
        point = QPointF(min(rect.right(), max(rect.left(), focus.x())),
                        min(rect.bottom(), max(rect.top(), focus.y())))
        canonical = self.coordinate_mapper(index).viewport_to_canonical((point.x(), point.y()))
        return index, canonical, point

    def _apply_layout(self, layout: PageLayout, anchor: tuple[int, tuple[float, float], QPointF],
                      *, zoom_changed: bool = False) -> None:
        self.sticky_note_reader.dismiss()
        index, canonical, point = anchor
        self._relayout = True
        try:
            self.session.ui.zoom = layout.scale / BASE_SCALE
            self.session.ui.layout = layout.mode.value
            self.page_layout = layout
            if zoom_changed:
                self.scheduler.defer_zoom()
            self._update_ranges()
            page = layout.pages[index]
            mapper = CoordinateMapper(self.session.pages[index], layout.scale, (page.x, page.y))
            scene_x, scene_y = mapper.canonical_to_viewport(canonical)
            center = max(0.0, (self.viewport().width() - layout.width) / 2)
            self.horizontalScrollBar().setValue(round(scene_x + center - point.x()))
            self.verticalScrollBar().setValue(round(scene_y - point.y()))
        finally:
            self._relayout = False
        # Keep the chosen PDF point through repeated geometry changes. When a
        # spread fits entirely, its gutter may replace the old page at the viewport
        # center; recapturing that center would drift to the companion page.
        self._layout_anchor = index, canonical
        self._last_y, self._last_scroll = self.verticalScrollBar().value(), perf_counter()
        self._track_viewport()
        self.viewport().update()

    def set_layout(self, mode: LayoutMode | str) -> None:
        mode = LayoutMode(mode)
        if self.session.lifecycle != Lifecycle.READY or mode == self.page_layout.mode:
            return
        if self._text_editor is not None and not self.try_confirm_text_box_editor():
            return
        with self.scheduler.metrics.span("layout_geometry_ms"):
            anchor = self._reading_anchor()
            try:
                layout = PageLayout(self.session.pages, self.page_layout.scale, mode,
                                    column_gap=self.page_layout.column_gap)
            except ValueError:
                return  # Qt scroll coordinates cannot represent the requested layout.
            self._apply_layout(layout, anchor)
            self.layout_changed.emit(mode.value)

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        if self._text_editor is not None and not self.try_confirm_text_box_editor():
            return
        with self.scheduler.metrics.span("zoom_geometry_ms"):
            self._set_zoom(zoom, anchor)

    def _set_zoom(self, zoom: float, anchor: QPointF | None) -> None:
        if not isfinite(zoom) or self.session.lifecycle != Lifecycle.READY:
            return
        zoom = min(MAX_ZOOM, max(MIN_ZOOM, zoom))
        if zoom == self.session.ui.zoom:
            return
        reading_anchor = self._reading_anchor(anchor)
        try:
            layout = PageLayout(self.session.pages, zoom * BASE_SCALE, self.page_layout.mode,
                                column_gap=self.page_layout.column_gap)
        except ValueError:
            # Keep the old valid layout/zoom if Qt's integer scroll range would overflow.
            return
        with self.scheduler.metrics.span("zoom_scroll_ms"):
            self._apply_layout(layout, reading_anchor, zoom_changed=True)
            self.zoom_changed.emit(zoom)

    def zoom_in(self) -> None:
        self.set_zoom(self.session.ui.zoom * 1.1)

    def zoom_out(self) -> None:
        self.set_zoom(self.session.ui.zoom / 1.1)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120
            if not steps and not event.pixelDelta().isNull():
                steps = event.pixelDelta().y() / 120
            self.set_zoom(self.session.ui.zoom * 1.1 ** max(-20, min(20, steps)), event.position())
            event.accept()
        else:
            super().wheelEvent(event)  # retain the validated continuous scroll behavior

    def _update_ranges(self) -> None:
        self.verticalScrollBar().setPageStep(self.viewport().height())
        self.verticalScrollBar().setRange(0, max(0, ceil(self.page_layout.height) - self.viewport().height()))
        self.horizontalScrollBar().setPageStep(self.viewport().width())
        self.horizontalScrollBar().setRange(0, max(0, ceil(self.page_layout.width) - self.viewport().width()))
        self._track_viewport()

    def _track_viewport(self) -> None:
        if self._relayout:
            return
        x, y = self._origin()
        visible = self.page_layout.visible(x, y, self.viewport().width(), self.viewport().height())
        nearby = self.page_layout.nearby(visible, y, self.viewport().height(), self.nearby_rows,
                                       direction=self._direction)
        viewport_rect = QRectF(self.viewport().rect())
        clips = []
        text_clips: dict[int, PdfRect] = {}
        for index in visible:
            page_rect = self.page_viewport_rect(index)
            intersection = page_rect.intersected(viewport_rect)
            if intersection.isEmpty():
                continue
            clips.append((index, ((intersection.left() - page_rect.left()) / self.session.ui.zoom,
                                  (intersection.top() - page_rect.top()) / self.session.ui.zoom,
                                  (intersection.right() - page_rect.left()) / self.session.ui.zoom,
                                  (intersection.bottom() - page_rect.top()) / self.session.ui.zoom)))
            mapper = self.coordinate_mapper(index)
            corners = tuple(mapper.viewport_to_canonical((x, y)) for x, y in (
                (intersection.left(), intersection.top()),
                (intersection.right(), intersection.top()),
                (intersection.left(), intersection.bottom()),
                (intersection.right(), intersection.bottom())))
            xs, ys = (tuple(point[0] for point in corners),
                      tuple(point[1] for point in corners))
            geometry = self.session.pages[index]
            x0 = min(geometry.width_pt, max(0.0, min(xs)))
            x1 = min(geometry.width_pt, max(0.0, max(xs)))
            y0 = min(geometry.height_pt, max(0.0, min(ys)))
            y1 = min(geometry.height_pt, max(0.0, max(ys)))
            if x1 > x0 and y1 > y0:
                text_clips[index] = PdfRect(x0, y0, x1, y1)
        self.scheduler.set_viewport(visible, nearby, self.viewport().devicePixelRatioF(),
                                    tuple(clips), self._direction)
        self.text_layer.set_visible(visible, text_clips)
        if self.scheduler.active and visible:
            index, canonical, _ = self._reading_anchor()
            self.session.ui.reading_anchor.page_index = index
            self.session.ui.reading_anchor.offset_pt = canonical[1]

    def set_active(self, active: bool, *, confirm_editor_on_deactivate: bool = True) -> None:
        """Change viewport activity without accidentally committing an editor.

        Normal tab deactivation confirms a live editor.  Failure presentation is
        deliberately different: it must only stop rendering/interaction, never
        submit another annotation command.
        """
        active = active and self.session.lifecycle == Lifecycle.READY
        if not active:
            self.sticky_note_reader.dismiss()
        if active == self._active:
            return
        if not active:
            self.external_interaction.cancel_gesture()
            self._appearance_epoch += 1
            if self.session.worker is not None:
                self.session.worker.cancel_annotation_appearances()
            self.cancel_pen_gesture()
            self.cancel_ink_move()
            self._text_caret_click = None
            if confirm_editor_on_deactivate:
                self.try_confirm_active_editor()
            self._callout_anchor = None
            self._callout_drag = None
            self._callout_move = None
        if active:
            self._track_viewport()
        self.scheduler.set_active(active)
        self.text_layer.set_active(active)
        self.search_layer.set_active(active)
        self._active = active

    def tick(self) -> None:
        self.sticky_note_reader.tick()
        if self.session.saving:
            return
        if self.scheduler.active:
            self._track_viewport()  # handles screen/DPR changes as well as scroll/zoom
        self.scheduler.interaction_pending = (
            self.text_layer.needs_interaction or self.pen_gesture.active
            or self._callout_drag is not None or self._callout_move is not None
            or bool(self.pending_inks) or bool(self.pending_ink_moves))
        self.scheduler.tick()
        self._tick_annotation_appearances()
        self.text_layer.tick(visible_raster_ready=self.scheduler.visible_ready,
                             render_pending=self.scheduler.pending is not None)
        self.search_layer.tick(visible_raster_ready=self.scheduler.visible_ready,
                               render_pending=self.scheduler.pending is not None)
        if (self._awaiting_insert_editor and self._editorial_editor is None
                and self.text_layer.insertion is not None):
            self._awaiting_insert_editor = False
            self._begin_editorial_editor(AnnotationKind.INSERT_TEXT,
                                         self.text_layer.insertion)
        if (self._awaiting_range_action is not None
                and self.text_layer.confirmed is not None):
            action, self._awaiting_range_action = self._awaiting_range_action, None
            if action == AnnotationKind.DELETE_TEXT:
                self._queue_editorial(AnnotationKind.DELETE_TEXT, self.text_layer.confirmed)
                self.text_layer.clear()
            else:
                self._begin_editorial_editor(action, self.text_layer.confirmed)

    def show_failure(self, message: str) -> None:
        if self.error_message == message and not self._active:
            return
        self.error_message = message
        # Reporting an error is presentation only.  In particular, do not use
        # the normal tab-change auto-confirm path here: a synchronous rejection
        # would emit session_changed again while this failure is being shown.
        self.set_active(False, confirm_editor_on_deactivate=False)
        self.viewport().update()

    def document_reopened(self) -> None:
        """Invalidate generation-bound resources without rebuilding the view."""
        self.sticky_note_reader.dismiss()
        self.scheduler.clear()
        self.scheduler.pending = None
        self.text_layer.clear()
        self.text_layer.pending = None
        self.search_layer.pending = None
        self._appearance_epoch += 1
        self._appearance_errors.clear()
        self._track_viewport()
        self.viewport().update()

    def set_search_query(self, query: str) -> None:
        self.search_layer.set_query(query)

    def close_search(self) -> None:
        self.search_layer.close()

    def navigate_search(self, forward: bool) -> None:
        match = self.search_layer.navigate(forward)
        if match is not None:
            target = self._display_rect(match.page_index, match.bbox)
            self.verticalScrollBar().setValue(round(
                self.verticalScrollBar().value() + target.center().y()
                - self.viewport().height() / 2))
            self.horizontalScrollBar().setValue(round(
                self.horizontalScrollBar().value() + target.center().x()
                - self.viewport().width() / 2))

    def _search_changed(self) -> None:
        self.search_changed.emit()
        self.viewport().update()

    def apply_highlight(self) -> None:
        target = self.text_layer.confirmed
        if target is None or self.pending_highlight_id is not None:
            return
        command = CreateHighlight(
            CommandContext.create(self.session.document_id,
                                  self.session.document_generation,
                                  CommandSource.UI), target,
            HighlightStyle(dict(HIGHLIGHT_COLOR_PRESETS)[self.session.ui.highlight_color]))
        self.pending_highlight_id = command.context.command_id
        self.selection_changed.emit()
        self.highlight_requested.emit(command)

    def handle_annotation_result(self, result: MutationResult) -> None:
        if self.external_interaction.result(result):
            return
        if result.command_id == self.pending_highlight_id:
            self.pending_highlight_id = None
            if result.success:
                self.text_layer.clear()
            self.selection_changed.emit()
            self.viewport().update()
            return
        if result.command_id == self.pending_delete_id:
            self.pending_delete_id = None
            if result.success:
                self.selected_ink_id = None
                self.selected_text_markup_id = None
                self.selected_text_box_id = None
                self.selected_callout_id = None
                # The manager removes a successfully deleted external logical
                # item before emitting this result. Selection is feedback only;
                # it must not leave a stale overlay or sidebar target behind.
                if self.selected_inventory_id is not None:
                    self._set_selected_inventory(None)
            self.selection_changed.emit()
            self.viewport().update()
            return
        pending = self.pending_inks.pop(result.command_id, None)
        if pending is not None:
            self.pen_roundtrip_ms.append((perf_counter() - pending.submitted_at) * 1000)
            self.viewport().update()
            return
        pending_move = self.pending_ink_moves.pop(result.command_id, None)
        if pending_move is not None:
            self.ink_move_roundtrip_ms.append((perf_counter() - pending_move.submitted_at) * 1000)
            self.viewport().update()
            return
        pending_text = self.pending_text_boxes.pop(result.command_id, None)
        if pending_text is not None:
            pasted_annotation_id = self._pending_paste_ids.pop(result.command_id, None)
            self.text_box_roundtrip_ms.append((perf_counter() - pending_text.submitted_at) * 1000)
            if result.success and pasted_annotation_id is not None:
                model = self.session.annotations.get(pasted_annotation_id)
                if model is not None and model.kind == AnnotationKind.TEXT_BOX:
                    self._set_selected_callout(None)
                    self._set_selected_text_box(pasted_annotation_id)
                elif model is not None and model.kind == AnnotationKind.CALLOUT:
                    self._set_selected_text_box(None)
                    self._set_selected_callout(pasted_annotation_id)
            if result.success and pending_text.annotation_id is None:
                # A creation draft is one-shot only after the worker accepts
                # it. Failed creates restore their editor and preserve the
                # active tool, so no user text or retry path is discarded.
                if isinstance(pending_text.target, RectTarget):
                    self.set_text_box_enabled(False)
                else:
                    self.set_callout_enabled(False)
            if not result.success and pending_text.restore_editor and self._text_editor is None:
                # The worker is authoritative. A failed create/update returns to
                # the exact editable plain-text state; move/resize/transfer roll
                # back to their confirmed model instead.
                self._begin_text_box_editor(pending_text.target, pending_text.annotation_id,
                                             pending_text.style, pending_text.text,
                                             pending_text.rich_text, restoring=True)
            self.viewport().update()
            return
        pending_editorial = self.pending_editorials.pop(result.command_id, None)
        if pending_editorial is not None:
            if result.success and result.annotation_id is not None:
                self._set_selected_text_markup(result.annotation_id.value)
                self.text_layer.clear()
                self._return_editorial_to_neutral(pending_editorial.kind)
            elif self._editorial_editor is None:
                self._begin_editorial_editor(pending_editorial.kind, pending_editorial.target,
                                             pending_editorial.text or "")
            self.selection_changed.emit()
            self.viewport().update()

    def _return_editorial_to_neutral(self, kind: AnnotationKind) -> None:
        """Finish only accepted one-shot text corrections in neutral mode."""
        if kind == AnnotationKind.DELETE_TEXT:
            self.set_delete_text_enabled(False)
        elif kind == AnnotationKind.INSERT_TEXT:
            self.set_insert_text_enabled(False)
        elif kind == AnnotationKind.REPLACE_TEXT:
            self.set_replace_text_enabled(False)

    def _selection_changed(self) -> None:
        self.selection_changed.emit()
        self.viewport().update()

    def _text_failed(self, error) -> None:
        # Text extraction/selection failure does not make raster viewing fail.
        self.text_error.emit(error)
        self.selection_changed.emit()

    def _hide_editorial_tooltip(self) -> None:
        self._hover_editorial_id = None
        QToolTip.hideText()

    @staticmethod
    def editorial_tooltip_text(model) -> str | None:
        """Return the unmodified confirmed payload; no viewer text is normalized."""
        if model.kind == AnnotationKind.INSERT_TEXT and isinstance(model.text, str):
            return model.text
        if model.kind == AnnotationKind.REPLACE_TEXT and isinstance(model.text, str):
            return model.text
        return None

    def _update_editorial_tooltip(self, point: QPointF) -> None:
        if (self.delete_text_enabled or self.insert_text_enabled or self.replace_text_enabled
                or self._editorial_editor is not None or self._text_editor is not None):
            self._hide_editorial_tooltip()
            return
        page_index = self._page_at_viewport(point)
        annotation_id = None
        external = None
        if page_index is not None:
            annotation_id = (self._hit_test_editorial_insert(point, page_index)
                             or self._hit_test_non_movable_text_markup(point, page_index))
            if annotation_id is None:
                external = self._hit_test_inventory_editorial(point, page_index)
        model = self.session.annotations.get(annotation_id) if annotation_id else None
        text = (self.editorial_tooltip_text(model) if model is not None
                else self._inventory_editorial_tooltip_text(external) if external is not None else None)
        if text is None:
            self._hide_editorial_tooltip()
            return
        self._show_editorial_tooltip(annotation_id or external.logical_id, text, point)

    @staticmethod
    def _inventory_editorial_tooltip_text(summary: AnnotationSummary) -> str | None:
        return summary.preview if summary.kind in ("insert_text", "replace_text") else None

    def _show_editorial_tooltip(self, annotation_id: str, text: str, point: QPointF) -> None:
        if annotation_id == self._hover_editorial_id:
            return
        self._hover_editorial_id = annotation_id
        # Keep whitespace and line breaks visible while bounding the tooltip.
        rich = '<div style="max-width: 360px; white-space: pre-wrap">' + escape(text) + '</div>'
        QToolTip.showText(self.viewport().mapToGlobal(point.toPoint()), rich, self.viewport())

    def _page_at_viewport(self, point: QPointF) -> int | None:
        x, y = self._origin()
        for index in self.page_layout.visible(
                x, y, self.viewport().width(), self.viewport().height()):
            if self.page_viewport_rect(index).contains(point):
                return index
        return None

    def _begin_transient_pan(self, point: QPointF) -> None:
        """Claim Ctrl+left press before it can become a PDF interaction."""
        self._pan_start = QPointF(point)
        self._pan_scroll_start = (self.horizontalScrollBar().value(),
                                  self.verticalScrollBar().value())
        self._pan_active = False
        self._pan_consumed = True
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)

    def _continue_transient_pan(self, event) -> None:
        if self._pan_start is None or self._pan_scroll_start is None:
            return
        if not event.buttons() & Qt.MouseButton.LeftButton:
            return
        delta_x = event.position().x() - self._pan_start.x()
        delta_y = event.position().y() - self._pan_start.y()
        if not self._pan_active:
            threshold = QApplication.startDragDistance()
            if delta_x * delta_x + delta_y * delta_y < threshold * threshold:
                return
            self._pan_active = True
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        start_x, start_y = self._pan_scroll_start
        self.horizontalScrollBar().setValue(round(start_x - delta_x))
        self.verticalScrollBar().setValue(round(start_y - delta_y))

    def _end_transient_pan(self, modifiers) -> None:
        self._pan_start = None
        self._pan_scroll_start = None
        self._pan_active = False
        self._pan_consumed = False
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self._restore_interaction_cursor()

    def _restore_interaction_cursor(self) -> None:
        if self.pen_enabled or self.text_box_enabled or self.callout_enabled:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        elif self.delete_text_enabled or self.insert_text_enabled or self.replace_text_enabled:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.viewport().unsetCursor()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._hide_editorial_tooltip()
        point = event.position()
        if self._text_editor is not None:
            self.try_confirm_text_box_editor()
            event.accept()
            return
        if self._editorial_editor is not None:
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._begin_transient_pan(point)
            event.accept()
            return
        page_index = self._page_at_viewport(point)
        if page_index is not None:
            self._paste_target_candidate = page_index, QPointF(point)
        if self.pen_enabled:
            if page_index is not None:
                canonical, display = self._pen_point(page_index, point)
                self._gesture_style = self.pen_style
                self.pen_gesture.begin(page_index, canonical, display)
                self.viewport().update()
            event.accept()
            return
        if self.text_box_enabled:
            if page_index is not None:
                canonical = self._canonical_clamped(page_index, point)
                self._text_box_drag = page_index, canonical, canonical
                self._set_selected_text_box(None)
                self.text_layer.clear()
                self.viewport().update()
            event.accept()
            return
        if self.callout_enabled:
            if page_index is not None:
                canonical = self._canonical_clamped(page_index, point)
                if self._callout_anchor is None:
                    # Two-stage gesture: click the anchor, then click (or drag
                    # to) the position of one compact standard-size box.
                    self._callout_anchor = page_index, canonical
                elif self._callout_anchor[0] == page_index:
                    self._callout_drag = page_index, self._callout_anchor[1], canonical
                else:
                    self._callout_anchor = page_index, canonical
                self._set_selected_text_box(None)
                self._set_selected_callout(None)
                self.text_layer.clear()
                self.viewport().update()
            event.accept()
            return
        if self.insert_text_enabled:
            if page_index is not None:
                canonical = self._canonical_clamped(page_index, point)
                self._awaiting_insert_editor = self.text_layer.begin_insertion(
                    page_index, canonical)
                self._set_selected_text_markup(None)
                self._set_selected_ink(None)
                self._set_selected_text_box(None)
                self._set_selected_callout(None)
                self.viewport().update()
            event.accept()
            return
        if self.delete_text_enabled or self.replace_text_enabled:
            if page_index is not None:
                canonical = self._canonical_clamped(page_index, point)
                self._awaiting_range_action = (
                    AnnotationKind.DELETE_TEXT if self.delete_text_enabled
                    else AnnotationKind.REPLACE_TEXT)
                if not self.text_layer.begin_drag(page_index, canonical):
                    self._awaiting_range_action = None
                self._set_selected_text_markup(None)
                self._set_selected_ink(None)
                self._set_selected_text_box(None)
                self._set_selected_callout(None)
            event.accept()
            return
        if page_index is None:
            self._set_selected_ink(None)
            self._set_selected_text_markup(None)
            self._set_selected_text_box(None)
            self._set_selected_callout(None)
            self.text_layer.clear()
            event.accept()
            return
        self._set_selected_inventory(None)
        text_hit = self._hit_test_text_box(point, page_index)
        if text_hit is not None:
            model = self.session.annotations.get(text_hit)
            if (model is not None and isinstance(model.target, RectTarget)
                    and isinstance(model.style, FreeTextStyle)):
                canonical = self._canonical_clamped(page_index, point)
                self._set_selected_text_box(text_hit)
                self._set_selected_ink(None)
                self._set_selected_text_markup(None)
                self.text_layer.clear()
                corner = self._resize_corner(point, page_index, model.target.rect)
                self._text_box_move = _FreeTextMove(text_hit, model.target, canonical, corner,
                                                    model.target.rect, model.page_index)
                event.accept()
                return
        callout_hit = self._hit_test_callout(point, page_index)
        if callout_hit is not None:
            model = self.session.annotations.get(callout_hit)
            if (model is not None and isinstance(model.target, CalloutTarget)
                    and isinstance(model.style, FreeTextStyle)):
                canonical = self._canonical_clamped(page_index, point)
                self._set_selected_callout(callout_hit)
                self._set_selected_text_box(None)
                self._set_selected_ink(None)
                self._set_selected_text_markup(None)
                self.text_layer.clear()
                corner = self._resize_corner(point, page_index, model.target.text_rect)
                action = corner or ("anchor" if self._callout_anchor_hit(
                    point, page_index, model.target) else "move")
                self._callout_move = _CalloutMove(callout_hit, model.target, canonical, action,
                                                   model.target.text_rect, model.target.anchor,
                                                   model.page_index)
                event.accept()
                return
        hit = self._hit_test_ink(point, page_index)
        if hit is not None:
            model = self.session.annotations.get(hit)
            if (model is not None and isinstance(model.target, InkTarget)
                    and isinstance(model.style, InkStyle)):
                canonical = self.coordinate_mapper(page_index).viewport_to_canonical(
                    (point.x(), point.y()))
                self._set_selected_ink(hit)
                self._set_selected_text_markup(None)
                self.text_layer.clear()
                self._ink_move = _InkMove(hit, model.target, model.style, PdfPoint(*canonical))
                event.accept()
                return
        # App-created models are the authoritative editable layer.  Their
        # persisted PDF counterparts are also present in the native inventory,
        # so external selection must run only after the M7 editable hit-tests.
        inventory_hit = self._hit_test_inventory(point, page_index)
        if inventory_hit is not None:
            summary = self.session.annotation_inventory[inventory_hit]
            if (summary.kind == "sticky_note" and (self.pending_text_boxes or self.pending_editorials
                    or self.external_interaction.pending_editor is not None)):
                # Qt can auto-confirm the focused editor before this press is
                # delivered. Preserve that editor's precedence while it commits.
                event.accept()
                return
            self._set_selected_inventory(inventory_hit)
            self._set_selected_ink(None)
            self._set_selected_text_markup(None)
            self._set_selected_text_box(None)
            self._set_selected_callout(None)
            self.text_layer.clear()
            self.external_interaction.press(summary, point)
            if summary.kind == "sticky_note":
                self.sticky_note_reader.open_note(summary)
            event.accept()
            return
        insert_hit = self._hit_test_editorial_insert(point, page_index)
        if insert_hit is not None:
            self._text_markup_click = _TextMarkupClick(
                insert_hit, page_index, self._canonical_clamped(page_index, point), QPointF(point))
            event.accept()
            return
        markup_hit = self._hit_test_non_movable_text_markup(point, page_index)
        if markup_hit is not None:
            self._text_markup_click = _TextMarkupClick(
                markup_hit, page_index, self._canonical_clamped(page_index, point), QPointF(point))
            event.accept()
            return
        self._set_selected_ink(None)
        self._set_selected_text_markup(None)
        self._set_selected_text_box(None)
        self._set_selected_callout(None)
        canonical = self._canonical_clamped(page_index, point)
        if self.text_layer.can_begin_insertion(page_index, canonical):
            # Delay choosing caret versus range until the pointer has crossed
            # Qt's drag threshold. This keeps normal clicks from producing the
            # old one-character range while retaining M6 drag semantics.
            self._text_caret_click = _TextCaretClick(page_index, canonical, QPointF(point))
        else:
            self.text_layer.clear()
        event.accept()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.viewport() and event.type() == QEvent.Type.ContextMenu:
            self._show_insert_context_menu(event.pos())
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def contextMenuEvent(self, event) -> None:
        """Handle platform routes that target the scroll area instead of its viewport."""
        self._show_insert_context_menu(self.viewport().mapFrom(self, event.pos()))
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._pan_consumed:
            self._continue_transient_pan(event)
            event.accept()
            return
        if not event.buttons():
            if (event.modifiers() & Qt.KeyboardModifier.ControlModifier
                    and self._text_editor is None and self._editorial_editor is None):
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
                event.accept()
                return
            if self.viewport().cursor().shape() == Qt.CursorShape.OpenHandCursor:
                self._restore_interaction_cursor()
        if event.buttons() & Qt.MouseButton.LeftButton and self.external_interaction.move(event.position()):
            event.accept()
            return
        if event.buttons():
            self._hide_editorial_tooltip()
        else:
            self._update_editorial_tooltip(event.position())
        if self._paste_target_candidate is not None and event.buttons() & Qt.MouseButton.LeftButton:
            _page, start = self._paste_target_candidate
            dx, dy = event.position().x() - start.x(), event.position().y() - start.y()
            if dx * dx + dy * dy > 16:
                self._paste_target_candidate = None
        if self.pen_gesture.active:
            started = perf_counter()
            if event.buttons() & Qt.MouseButton.LeftButton:
                page_index = self.pen_gesture.page_index
                canonical, display = self._pen_point(page_index, event.position())
                if self.pen_gesture.update(canonical, display):
                    self.viewport().update()
            self.pen_move_ms.append((perf_counter() - started) * 1000)
            event.accept()
            return
        if self._text_markup_click is not None:
            click = self._text_markup_click
            if event.buttons() & Qt.MouseButton.LeftButton:
                dx = event.position().x() - click.viewport_start.x()
                dy = event.position().y() - click.viewport_start.y()
                if dx * dx + dy * dy > 4:
                    self._text_markup_click = None
                    self._set_selected_text_markup(None)
                    current = self._canonical_clamped(click.page_index, event.position())
                    if self.text_layer.begin_drag(click.page_index, click.start):
                        self.text_layer.update_drag(current)
            event.accept()
            return
        if self._text_caret_click is not None:
            click = self._text_caret_click
            if event.buttons() & Qt.MouseButton.LeftButton:
                dx = event.position().x() - click.viewport_start.x()
                dy = event.position().y() - click.viewport_start.y()
                if dx * dx + dy * dy >= QApplication.startDragDistance() ** 2:
                    self._text_caret_click = None
                    if self.text_layer.begin_drag(click.page_index, click.start):
                        self.text_layer.update_drag(
                            self._canonical_clamped(click.page_index, event.position()))
            event.accept()
            return
        if self._text_box_drag is not None:
            page_index, start, _current = self._text_box_drag
            if event.buttons() & Qt.MouseButton.LeftButton:
                rect = self.page_viewport_rect(page_index)
                point = event.position()
                clamped = QPointF(min(rect.right(), max(rect.left(), point.x())),
                                  min(rect.bottom(), max(rect.top(), point.y())))
                self._text_box_drag = page_index, start, self._canonical_clamped(page_index, clamped)
                self.viewport().update()
            event.accept()
            return
        if self._callout_drag is not None:
            page_index, anchor, _current = self._callout_drag
            if event.buttons() & Qt.MouseButton.LeftButton:
                rect = self.page_viewport_rect(page_index)
                point = event.position()
                clamped = QPointF(min(rect.right(), max(rect.left(), point.x())),
                                  min(rect.bottom(), max(rect.top(), point.y())))
                self._callout_drag = page_index, anchor, self._canonical_clamped(page_index, clamped)
                self.viewport().update()
            event.accept()
            return
        if self._callout_move is not None:
            started = perf_counter()
            move = self._callout_move
            if event.buttons() & Qt.MouseButton.LeftButton:
                destination = (move.target.page_index if move.action != "move"
                               else self._drop_page(event.position()))
                current = self._canonical_clamped(destination, event.position())
                origin = self.coordinate_mapper(move.target.page_index).canonical_to_viewport(
                    (move.start.x, move.start.y))
                if ((event.position().x() - origin[0]) ** 2
                        + (event.position().y() - origin[1]) ** 2) > 4:
                    move.dragging = True
                model = self.session.annotations.get(move.annotation_id)
                if move.dragging and model is not None and isinstance(model.style, FreeTextStyle):
                    rich = model.rich_text or RichText.plain_text(
                        model.text or "", font_family=model.style.font_family,
                        font_size=model.style.font_size, color=model.style.text)
                    if move.action == "anchor":
                        move.preview_page_index = move.target.page_index
                        move.anchor = current
                    else:
                        move.preview_page_index = destination
                        move.rect = self._preview_callout_rect(move, current, destination, rich, model.style)
                        move.anchor = self._preview_callout_anchor(move, destination, move.rect)
                    self.viewport().update()
            self.text_box_move_ms.append((perf_counter() - started) * 1000)
            event.accept()
            return
        if self._text_box_move is not None:
            started = perf_counter()
            move = self._text_box_move
            if event.buttons() & Qt.MouseButton.LeftButton:
                destination = (move.target.page_index if move.resizing_corner is not None
                               else self._drop_page(event.position()))
                current = self._canonical_clamped(destination, event.position())
                start_display = self.coordinate_mapper(move.target.page_index).canonical_to_viewport(
                    (move.start.x, move.start.y))
                if ((event.position().x() - start_display[0]) ** 2
                        + (event.position().y() - start_display[1]) ** 2) > 4:
                    move.dragging = True
                if move.dragging:
                    move.preview_page_index = destination
                    model = self.session.annotations.get(move.annotation_id)
                    if model is not None and isinstance(model.style, FreeTextStyle):
                        rich_text = model.rich_text or RichText.plain_text(
                            model.text or "", font_family=model.style.font_family,
                            font_size=model.style.font_size, color=model.style.text)
                        move.rect = self._preview_text_box_rect(move, current, destination,
                                                                rich_text, model.style)
                        self.viewport().update()
            self.text_box_move_ms.append((perf_counter() - started) * 1000)
            event.accept()
            return
        if self._ink_move is not None:
            started = perf_counter()
            move = self._ink_move
            point = event.position()
            mapper = self.coordinate_mapper(move.target.page_index)
            current = mapper.viewport_to_canonical((point.x(), point.y()))
            display_origin = mapper.canonical_to_viewport((move.start.x, move.start.y))
            if ((point.x() - display_origin[0]) ** 2 + (point.y() - display_origin[1]) ** 2) > 16.0:
                move.dragging = True
            if move.dragging and event.buttons() & Qt.MouseButton.LeftButton:
                geometry = self.session.pages[move.target.page_index]
                move.delta_x, move.delta_y = clamp_ink_translation(
                    move.target, geometry.width_pt, geometry.height_pt,
                    current[0] - move.start.x, current[1] - move.start.y)
                self.viewport().update()
            self.ink_move_ms.append((perf_counter() - started) * 1000)
            event.accept()
            return
        page_index = self.text_layer.drag_page
        if not event.buttons():
            self._update_freetext_hover_cursor(event.position())
        if page_index is None or not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        rect = self.page_viewport_rect(page_index)
        point = event.position()
        clamped = QPointF(min(rect.right(), max(rect.left(), point.x())),
                          min(rect.bottom(), max(rect.top(), point.y())))
        canonical = self.coordinate_mapper(page_index).viewport_to_canonical(
            (clamped.x(), clamped.y()))
        self.text_layer.update_drag(PdfPoint(*canonical))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pan_consumed:
            self._end_transient_pan(event.modifiers())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.external_interaction.release(event.position()):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            candidate, self._paste_target_candidate = self._paste_target_candidate, None
            if candidate is not None:
                self.paste_target_page = candidate[0]
        if event.button() == Qt.MouseButton.LeftButton and self.pen_gesture.active:
            started = perf_counter()
            page_index = self.pen_gesture.page_index
            canonical, display = self._pen_point(page_index, event.position())
            self.pen_gesture.update(canonical, display, final=True)
            target, metrics = self.pen_gesture.finalize()
            self.last_pen_metrics = metrics
            style, self._gesture_style = self._gesture_style, None
            if target is not None and style is not None:
                command = CreateInk(
                    CommandContext.create(self.session.document_id,
                                          self.session.document_generation,
                                          CommandSource.UI), target, style)
                self.pending_inks[command.context.command_id] = _PendingInk(
                    target, style, perf_counter())
                self.ink_requested.emit(command)
            self.pen_release_enqueue_ms.append((perf_counter() - started) * 1000)
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._text_markup_click is not None:
            click, self._text_markup_click = self._text_markup_click, None
            self._set_selected_text_markup(click.annotation_id)
            self._set_selected_ink(None)
            self._set_selected_text_box(None)
            self._set_selected_callout(None)
            self.text_layer.clear()
            model = self.session.annotations.get(click.annotation_id)
            text = self.editorial_tooltip_text(model) if model is not None else None
            if text is not None:
                self._show_editorial_tooltip(click.annotation_id, text, event.position())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._ink_move is not None:
            started = perf_counter()
            move, self._ink_move = self._ink_move, None
            if move.dragging and (move.delta_x != 0.0 or move.delta_y != 0.0):
                model = self.session.annotations.get(move.annotation_id)
                if model is not None:
                    command = MoveInk(
                        CommandContext.create(self.session.document_id,
                                              self.session.document_generation,
                                              CommandSource.UI), model.annotation_id,
                        move.target.page_index, move.delta_x, move.delta_y)
                    self.pending_ink_moves[command.context.command_id] = _PendingInkMove(
                        move, perf_counter())
                    self.ink_requested.emit(command)
            self.ink_move_release_enqueue_ms.append((perf_counter() - started) * 1000)
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._text_box_drag is not None:
            page_index, start, end = self._text_box_drag
            self._text_box_drag = None
            rect = _final_text_box_rect(start, end)
            if rect is not None:
                self._begin_text_box_editor(RectTarget(page_index, rect), None, self.text_box_style)
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._callout_drag is not None:
            page_index, anchor, end = self._callout_drag
            self._callout_drag = None
            self._callout_anchor = None
            rect = self._initial_callout_rect(page_index, end, anchor)
            self._begin_text_box_editor(self._callout_target(page_index, rect, anchor), None,
                                        self.text_box_style)
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._callout_move is not None:
            started = perf_counter()
            move, self._callout_move = self._callout_move, None
            model = self.session.annotations.get(move.annotation_id)
            if (model is not None and move.dragging and move.rect is not None
                    and move.anchor is not None):
                target = self._callout_target(move.preview_page_index or model.page_index,
                                              move.rect, move.anchor)
                context = CommandContext.create(self.session.document_id,
                                                self.session.document_generation,
                                                CommandSource.UI)
                if target == model.target:
                    command = None
                elif move.action == "anchor":
                    command = MoveCalloutAnchor(context, model.annotation_id, target)
                elif move.action in ("tl", "tr", "bl", "br"):
                    command = ResizeCallout(context, model.annotation_id, target)
                elif target.page_index == model.page_index:
                    command = MoveCallout(context, model.annotation_id, model.page_index,
                                          target.text_rect.x0 - model.target.text_rect.x0,
                                          target.text_rect.y0 - model.target.text_rect.y0)
                else:
                    command = TransferCallout(context, model.annotation_id, model.page_index, target)
                if command is not None:
                    rich = model.rich_text or RichText.plain_text(
                        model.text or "", font_family=model.style.font_family,
                        font_size=model.style.font_size, color=model.style.text)
                    self._queue_text_box(command, target, rich.plain, rich, model.style)
            self.text_box_release_enqueue_ms.append((perf_counter() - started) * 1000)
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._text_box_move is not None:
            started = perf_counter()
            move, self._text_box_move = self._text_box_move, None
            model = self.session.annotations.get(move.annotation_id)
            if model is not None and move.dragging and move.rect is not None:
                context = CommandContext.create(self.session.document_id,
                                                self.session.document_generation,
                                                CommandSource.UI)
                target = RectTarget(move.preview_page_index or model.page_index, move.rect)
                if target == model.target:
                    command = None
                elif move.resizing_corner is not None:
                    command = ResizeFreeText(context, model.annotation_id,
                                             target)
                elif target.page_index == model.page_index:
                    command = MoveFreeText(context, model.annotation_id, model.page_index,
                                           move.rect.x0 - move.target.rect.x0,
                                           move.rect.y0 - move.target.rect.y0)
                else:
                    command = TransferFreeText(context, model.annotation_id, model.page_index, target)
                if command is not None:
                    rich_text = model.rich_text or RichText.plain_text(
                        model.text or "", font_family=model.style.font_family,
                        font_size=model.style.font_size, color=model.style.text)
                    self._queue_text_box(command, target, model.text or "", rich_text, model.style)
            self.text_box_release_enqueue_ms.append((perf_counter() - started) * 1000)
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._text_caret_click is not None:
            click, self._text_caret_click = self._text_caret_click, None
            self.text_layer.begin_insertion(click.page_index, click.start)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.text_layer.drag_page is not None:
            self.text_layer.end_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton and not self.text_box_enabled
                and not self.callout_enabled):
            page_index = self._page_at_viewport(event.position())
            if page_index is not None:
                external_id = self._hit_test_inventory(event.position(), page_index)
                if external_id is not None and self.external_interaction.request_editor(
                        self.session.annotation_inventory[external_id]):
                    event.accept()
                    return
                annotation_id = self._hit_test_text_box(event.position(), page_index)
                model = self.session.annotations.get(annotation_id) if annotation_id else None
                if (model is not None and isinstance(model.target, RectTarget)
                        and isinstance(model.style, FreeTextStyle)):
                    self._set_selected_text_box(annotation_id)
                    self._begin_text_box_editor(model.target, annotation_id, model.style,
                                                model.text or "", model.rich_text)
                    event.accept()
                    return
                callout_id = self._hit_test_callout(event.position(), page_index)
                callout = self.session.annotations.get(callout_id) if callout_id else None
                if (callout is not None and isinstance(callout.target, CalloutTarget)
                        and isinstance(callout.style, FreeTextStyle)):
                    self._set_selected_callout(callout_id)
                    self._begin_text_box_editor(callout.target, callout_id, callout.style,
                                                callout.text or "", callout.rich_text)
                    event.accept()
                    return
                canonical = self._canonical_clamped(page_index, event.position())
                if self.text_layer.select_word(page_index, canonical):
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def _show_insert_context_menu(self, point) -> None:
        """Offer Insert Text only for an existing canonical character boundary."""
        if (self.session.lifecycle != Lifecycle.READY or self.text_box_enabled
                or self.callout_enabled or self.pen_enabled or self._text_editor is not None
                or self._editorial_editor is not None):
            return
        if self._insert_context_menu is not None:
            self._insert_context_menu.close()
        page_index = self._page_at_viewport(QPointF(point))
        if page_index is None:
            self.text_layer.clear()
            return
        canonical = self._canonical_clamped(page_index, QPointF(point))
        if not self.text_layer.can_begin_insertion(page_index, canonical):
            self.text_layer.clear()
            return
        self._set_selected_ink(None)
        self._set_selected_text_markup(None)
        self._set_selected_text_box(None)
        self._set_selected_callout(None)
        self._set_selected_inventory(None)
        if not self.text_layer.begin_insertion(page_index, canonical):
            return
        menu = QMenu(self.viewport())
        self._insert_context_menu = menu
        self._insert_context_menu_generation += 1
        generation = self._insert_context_menu_generation
        action = menu.addAction("Inserisci testo")
        action.triggered.connect(self._begin_context_insert)
        menu.aboutToHide.connect(lambda: self._close_insert_context_menu(menu, generation))
        menu.popup(self.viewport().mapToGlobal(point))

    def _close_insert_context_menu(self, menu: QMenu, generation: int) -> None:
        if menu is not self._insert_context_menu:
            return
        self._insert_context_menu = None
        menu.deleteLater()

        # QMenu hides before it emits QAction.triggered for a mouse selection.
        # Defer dismissal cleanup one GUI turn: the action can mark the existing
        # canonical target as owned by the M11 editor workflow first. A plain
        # dismissal still clears it, while a newer menu cannot clear its target.
        QTimer.singleShot(0, lambda: self._clear_dismissed_insert_target(generation))

    def _clear_dismissed_insert_target(self, generation: int) -> None:
        if (generation == self._insert_context_menu_generation
                and not self._awaiting_insert_editor):
            self.text_layer.clear_insertion()

    def _begin_context_insert(self) -> None:
        """Open M11's existing editor for the caret resolved by this menu."""
        if self.text_layer.insertion is not None or self.text_layer.insertion_preview is not None:
            self._awaiting_insert_editor = True
            self.selection_changed.emit()

    def _begin_editorial_editor(self, kind: AnnotationKind,
                                target: TextRangeTarget | TextInsertionTarget,
                                text: str = "") -> None:
        if kind not in (AnnotationKind.INSERT_TEXT, AnnotationKind.REPLACE_TEXT):
            raise ValueError("Editorial editor requires Insert or Replace")
        if self._editorial_editor is not None:
            return
        self._editing_editorial_kind = kind
        self._editing_editorial_target = target
        editor = _EditorialTextEditor(self.viewport())
        editor.setAccessibleName("Testo correzione editoriale")
        editor.setPlainText(text)
        editor.confirmed.connect(self._confirm_editorial_editor)
        editor.cancelled.connect(self._cancel_editorial_editor)
        self._editorial_editor = editor
        self._position_editorial_editor()
        editor.show()
        editor.setFocus()
        self.viewport().update()

    def _position_editorial_editor(self) -> None:
        editor, target = self._editorial_editor, self._editing_editorial_target
        if editor is None or target is None:
            return
        if isinstance(target, TextInsertionTarget):
            point = target.anchor
        else:
            point = target.quads[-1].lower_right
        mapped = self.coordinate_mapper(target.page_index).canonical_to_viewport((point.x, point.y))
        width, height = 230, 76
        x = max(0, min(self.viewport().width() - width, round(mapped[0] + 8)))
        y = max(0, min(self.viewport().height() - height, round(mapped[1] + 8)))
        editor.setGeometry(x, y, width, height)

    def _cancel_editorial_editor(self) -> None:
        if self._editorial_editor is not None:
            self._editorial_editor.deleteLater()
        self._editorial_editor = None
        self._editing_editorial_kind = None
        self._editing_editorial_target = None
        self.text_layer.clear()
        self.viewport().update()

    def _confirm_editorial_editor(self) -> bool:
        editor, kind, target = (self._editorial_editor, self._editing_editorial_kind,
                                self._editing_editorial_target)
        if editor is None or kind is None or target is None:
            return True
        text = editor.toPlainText()
        # Empty Insert/Replace is deliberately not an implicit Delete.
        if not text:
            self._cancel_editorial_editor()
            return True
        editor.deleteLater()
        self._editorial_editor = None
        self._editing_editorial_kind = None
        self._editing_editorial_target = None
        return self._queue_editorial(kind, target, text)

    def _queue_editorial(self, kind: AnnotationKind,
                         target: TextRangeTarget | TextInsertionTarget,
                         text: str | None = None) -> bool:
        context = CommandContext.create(self.session.document_id,
                                        self.session.document_generation,
                                        CommandSource.UI)
        if kind == AnnotationKind.DELETE_TEXT and isinstance(target, TextRangeTarget):
            command = CreateDeleteText(context, target)
        elif kind == AnnotationKind.INSERT_TEXT and isinstance(target, TextInsertionTarget) and text:
            command = CreateInsertText(context, target, text)
        elif kind == AnnotationKind.REPLACE_TEXT and isinstance(target, TextRangeTarget) and text:
            command = CreateReplaceText(context, target, text)
        else:
            return False
        self.pending_editorials[command.context.command_id] = _PendingEditorial(
            kind, target, perf_counter(), text)
        # The optimistic overlay takes over from the M6 preview immediately;
        # never paint a selection/caret twice while the worker is pending.
        self.text_layer.clear()
        self.editorial_requested.emit(command)
        self.selection_changed.emit()
        self.viewport().update()
        # AnnotationService can reject synchronously.  Its result handler
        # removes the pending entry and restores the editor, so callers such as
        # Save can reliably keep the save barrier from starting.
        return command.context.command_id in self.pending_editorials

    def keyPressEvent(self, event) -> None:
        if (event.key() == Qt.Key.Key_Control and self._text_editor is None
                and self._editorial_editor is None and self.viewport().underMouse()):
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.external_interaction.gesture is not None:
            self.external_interaction.cancel_gesture()
            event.accept()
            return
        if (event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and self.selected_inventory_id is not None):
            self._delete_selected_inventory()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and (self._awaiting_insert_editor
                                                 or self._awaiting_range_action is not None):
            self._clear_editorial_interaction()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and (self._text_box_drag is not None
                                                 or self._text_box_move is not None
                                                 or self._callout_anchor is not None
                                                 or self._callout_drag is not None
                                                 or self._callout_move is not None):
            self.cancel_text_box_edit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.pen_gesture.active:
            self.cancel_pen_gesture()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self._ink_move is not None:
            self.cancel_ink_move()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.selected_text_markup_id is not None:
                self._delete_selected_text_markup()
                event.accept()
                return
            if self.selected_text_box_id is not None:
                self._delete_selected_text_box()
                event.accept()
                return
            if self.selected_callout_id is not None:
                self._delete_selected_callout()
                event.accept()
                return
            if self.selected_ink_id is not None:
                self.delete_selected_ink()
                event.accept()
                return
        if event.matches(QKeySequence.StandardKey.Copy):
            text = self.text_layer.selected_text()
            if text is not None:
                QApplication.clipboard().setText(text)
                event.accept()
                return
            if self._copy_selected_annotation():
                event.accept()
                return
        if event.matches(QKeySequence.StandardKey.Cut):
            if self._copy_selected_annotation():
                # Clipboard publication is deliberately prior to Delete: a failed
                # or stale delete leaves both the source and its reusable payload.
                self._delete_selected_freetext()
                event.accept()
                return
        if event.matches(QKeySequence.StandardKey.Paste):
            if self._paste_annotation():
                event.accept()
                return
        if event.key() == Qt.Key.Key_Escape and self.text_layer.has_target:
            self.text_layer.clear()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and (self.pen_enabled or self.text_box_enabled
                                                 or self.callout_enabled):
            self.set_pen_enabled(False)
            self.set_text_box_enabled(False)
            self.set_callout_enabled(False)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.selected_inventory_id is not None:
            self._set_selected_inventory(None)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and any((self.selected_ink_id is not None,
                                                     self.selected_text_markup_id is not None,
                                                     self.selected_text_box_id is not None,
                                                     self.selected_callout_id is not None)):
            self._set_selected_ink(None)
            self._set_selected_text_markup(None)
            self._set_selected_text_box(None)
            self._set_selected_callout(None)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Control:
            if self._pan_consumed:
                # The following mouse release remains consumed, so releasing
                # Ctrl mid-drag can never revive the suppressed PDF gesture.
                self._pan_start = None
                self._pan_scroll_start = None
                self._pan_active = False
            self._restore_interaction_cursor()
        super().keyReleaseEvent(event)

    def _selected_freetext_model(self):
        annotation_id = self.selected_text_box_id or self.selected_callout_id
        model = self.session.annotations.get(annotation_id) if annotation_id else None
        if (model is None or model.ownership != AnnotationOwnership.CREATED_BY_APP
                or model.kind not in (AnnotationKind.TEXT_BOX, AnnotationKind.CALLOUT)
                or not isinstance(model.target, (RectTarget, CalloutTarget))
                or not isinstance(model.style, FreeTextStyle)):
            return None
        return model

    def _copy_selected_annotation(self) -> bool:
        model = self._selected_freetext_model()
        if model is None:
            return False
        try:
            page = self.session.pages[model.page_index]
            payload = AnnotationClipboardPayload.from_model(
                model, PageBounds(page.width_pt, page.height_pt)).to_bytes()
        except (IndexError, ValueError):
            return False
        mime = QMimeData()
        mime.setData(ANNOTATION_CLIPBOARD_MIME, payload)
        QApplication.clipboard().setMimeData(mime)
        self._clipboard_paste_bytes, self._clipboard_paste_count = payload, 0
        # Only a click after Copy overrides the current viewer page.
        self.paste_target_page = None
        return True

    def _delete_selected_freetext(self) -> None:
        model = self._selected_freetext_model()
        if model is None or self.pending_delete_id is not None:
            return
        command = DeleteAnnotation(CommandContext.create(
            self.session.document_id, self.session.document_generation, CommandSource.UI),
            model.annotation_id, model.page_index)
        self.pending_delete_id = command.context.command_id
        self.text_box_requested.emit(command)

    def _paste_annotation(self) -> bool:
        mime = QApplication.clipboard().mimeData()
        if mime is None or not mime.hasFormat(ANNOTATION_CLIPBOARD_MIME):
            return False
        try:
            raw = bytes(mime.data(ANNOTATION_CLIPBOARD_MIME))
            payload = AnnotationClipboardPayload.from_bytes(raw)
            page_index = (self.paste_target_page if self.paste_target_page is not None
                          else self.session.ui.reading_anchor.page_index)
            if not 0 <= page_index < len(self.session.pages):
                page_index = 0
            page = self.session.pages[page_index]
            if raw != self._clipboard_paste_bytes:
                self._clipboard_paste_bytes, self._clipboard_paste_count = raw, 0
            self._clipboard_paste_count += 1
            # Convert the required widget-space cascade to canonical coordinates
            # at the destination page's current zoom / rotation.
            mapper = self.coordinate_mapper(page_index)
            origin = mapper.canonical_to_viewport((0.0, 0.0))
            cascade_px = min(120.0, 24.0 * self._clipboard_paste_count)
            shifted = mapper.viewport_to_canonical((origin[0] + cascade_px,
                                                     origin[1] + cascade_px))
            base = mapper.viewport_to_canonical(origin)
            target = payload.target_for_destination(
                page_index, PageBounds(page.width_pt, page.height_pt),
                shifted[0] - base[0], shifted[1] - base[1])
            context = CommandContext.create(self.session.document_id,
                                            self.session.document_generation,
                                            CommandSource.UI)
            command = (CreateFreeText(context, target, payload.text, payload.style,
                                      rich_text=payload.rich_text)
                       if isinstance(target, RectTarget)
                       else CreateCallout(context, target, payload.text, payload.style,
                                          rich_text=payload.rich_text))
        except (IndexError, ValueError):
            return False
        # Unlike the original implementation, paste must join the existing
        # pending overlay path before sending IPC.  The result callback removes
        # this overlay after DocumentManager adopts the confirmed model.
        self._pending_paste_ids[command.context.command_id] = command.annotation_id.value
        self._queue_text_box(command, target, payload.text, payload.rich_text,
                             payload.style, restore_editor=False)
        self.viewport().update()
        return True

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.sticky_note_reader.dismiss()
        if not self._relayout:
            self._layout_anchor = None
        self._update_ranges()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.sticky_note_reader.dismiss()
        if self._relayout:
            return
        self._layout_anchor = None
        now = perf_counter()
        y = self.verticalScrollBar().value()
        if y != self._last_y:
            self._direction = 1 if y > self._last_y else -1
            if abs(y - self._last_y) / max(.001, now - self._last_scroll) > 1500:
                self.scheduler.defer_prefetch()
            self._last_y, self._last_scroll = y, now
        self._track_viewport()
        self._position_text_editor()
        self._position_editorial_editor()
        self.viewport().update()

    def paintEvent(self, event) -> None:
        with self.scheduler.metrics.span("paint_ms"):
            self._paint(event)

    def _paint(self, event) -> None:
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            background = QColor("#595c61")
            painter.fillRect(event.rect(), background)
            if self.error_message:
                painter.setPen(QColor("white"))
                painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self.error_message)
                return
            x, y = self._origin()
            visible = self.page_layout.visible(x, y, self.viewport().width(), self.viewport().height())
            for index in visible:
                rect = self.page_viewport_rect(index)
                painter.fillRect(rect, QColor("#fafafa"))
                painted = False
                preview = (self.scheduler.budget.preview(self.scheduler._key(index))
                           if self.scheduler.active else None)
                if preview is not None:
                    with self.scheduler.budget.borrow(preview.key) as borrowed:
                        if borrowed is not None:
                            painter.drawImage(rect, borrowed.image)
                            painted = True
                current_region_keys = self.scheduler.visible_region_keys(index)
                regional_keys = set(current_region_keys)
                if regional_keys:
                    target_scale = current_region_keys[0].scale
                    entries = [entry for entry in self.scheduler.budget.page_entries(
                        self.session.document_id, self.session.document_generation, index)
                               if entry.key.region is not None and entry.coverage is not None]
                    # Older-density tiles are previews; exact current tiles paint last.
                    entries.sort(key=lambda entry: (entry.key in regional_keys,
                                                    -abs(entry.key.scale - target_scale)))
                    painter.save()
                    painter.setClipRect(rect)
                    for entry in entries:
                        x0, y0, x1, y1 = entry.coverage
                        target = QRectF(rect.left() + x0 * self.session.ui.zoom,
                                        rect.top() + y0 * self.session.ui.zoom,
                                        (x1 - x0) * self.session.ui.zoom,
                                        (y1 - y0) * self.session.ui.zoom)
                        if not target.intersects(QRectF(event.rect())):
                            continue
                        with self.scheduler.budget.borrow(entry.key) as borrowed:
                            if borrowed is not None:
                                painter.drawImage(target, borrowed.image)
                                painted = True
                    painter.restore()
                if not painted:
                    painter.setPen(QColor("#686868"))
                    message = (f"Impossibile visualizzare la pagina {index + 1}."
                               if index in self.scheduler.errors else f"Pagina {index + 1}")
                    painter.drawText(rect.intersected(QRectF(self.viewport().rect())),
                                     Qt.AlignmentFlag.AlignCenter, message)
                self._paint_overlays(painter, index)
            if self.page_layout.mode != LayoutMode.SINGLE:
                self._paint_spread_seams(painter, visible, background)
        finally:
            if painter.isActive():
                painter.end()

    def _paint_spread_seams(self, painter: QPainter, visible: tuple[int, ...], background: QColor) -> None:
        # Align in device pixels. Keep the pages touching in layout coordinates
        # so navigation, hit testing and annotation geometry are unchanged.
        transform = painter.deviceTransform()
        pixel = 1 / transform.m11()
        offset = transform.dx()
        for row_index in {self.page_layout.page_rows[index] for index in visible}:
            pages = self.page_layout.rows[row_index].page_indices
            if len(pages) != 2:
                continue
            left, right = (self.page_viewport_rect(index) for index in pages)
            edge = floor(right.left() / pixel + offset)
            painter.fillRect(QRectF((edge - offset) * pixel, right.top(), pixel,
                                    min(left.height(), right.height())), background)

    def _paint_overlays(self, painter: QPainter, page_index: int) -> None:
        mapper = self.coordinate_mapper(page_index)
        # Deterministic z-order: raster, confirmed Highlight, Ink, FreeText,
        # selection previews. All app annotations remain independent of rasters.
        for model in self.session.annotations.values():
            if (model.page_index != page_index or model.kind != AnnotationKind.HIGHLIGHT
                    or not isinstance(model.target, TextRangeTarget)):
                # The model registry will contain other annotation kinds in later
                # milestones; M6 paints only native text-range highlights.
                continue
            style = model.style
            color = QColor(255, 235, 0)
            if isinstance(style, HighlightStyle):
                color = QColor.fromRgbF(style.color.red, style.color.green,
                                        style.color.blue, style.opacity)
            self._paint_quads(painter, mapper, model.target.quads, color,
                              selected=model.annotation_id.value == self.selected_text_markup_id)
        for model in self.session.annotations.values():
            if (model.page_index != page_index
                    or model.kind not in (AnnotationKind.DELETE_TEXT,
                                          AnnotationKind.INSERT_TEXT,
                                          AnnotationKind.REPLACE_TEXT)
                    or not isinstance(model.target, (TextRangeTarget, TextInsertionTarget))):
                continue
            self._paint_editorial(painter, mapper, model.kind, model.target,
                                  selected=model.annotation_id.value == self.selected_text_markup_id)
        for model in self.session.annotations.values():
            if (model.page_index == page_index and model.kind == AnnotationKind.INK
                    and isinstance(model.target, InkTarget)
                    and isinstance(model.style, InkStyle)):
                if model.annotation_id.value in self._moving_ink_ids():
                    continue
                self._paint_ink(
                    painter, mapper, model.target, model.style,
                    selected=model.annotation_id.value == self.selected_ink_id)
        for model in self.session.annotations.values():
            if (model.page_index != page_index or model.kind != AnnotationKind.TEXT_BOX
                    or not isinstance(model.target, RectTarget)
                    or not isinstance(model.style, FreeTextStyle)
                    or model.annotation_id.value in self._moving_text_box_ids()
                    or model.annotation_id.value == self._editing_text_box_id):
                continue
            self._paint_text_box(painter, mapper, model.target.rect, model.rich_text or RichText.plain_text(
                model.text or "", font_family=model.style.font_family, font_size=model.style.font_size,
                color=model.style.text), model.style,
                                 selected=model.annotation_id.value == self.selected_text_box_id)
        for model in self.session.annotations.values():
            if (model.page_index != page_index or model.kind != AnnotationKind.CALLOUT
                    or not isinstance(model.target, CalloutTarget)
                    or not isinstance(model.style, FreeTextStyle)
                    or model.annotation_id.value in self._moving_callout_ids()
                    or model.annotation_id.value == self._editing_text_box_id):
                continue
            self._paint_callout(painter, mapper, model.target, model.rich_text or RichText.plain_text(
                model.text or "", font_family=model.style.font_family, font_size=model.style.font_size,
                color=model.style.text), model.style,
                selected=model.annotation_id.value == self.selected_callout_id)
        # The page raster excludes native annotations. Inventory overlays
        # share their summaries with hit testing and the sidebar.
        for summary in self.session.annotation_inventory.values():
            if summary.page_index == page_index and _uses_inventory_overlay(summary):
                self.external_interaction.paint(
                    painter, mapper, summary, summary.logical_id == self.selected_inventory_id)
        if (isinstance(self._editing_target, CalloutTarget)
                and self._editing_target.page_index == page_index
                and self._editing_style is not None):
            # The QTextEdit owns the box while editing; this paints the same
            # leader and open arrow as preview and confirmed state beneath it.
            self._paint_callout(painter, mapper, self._editing_target, None,
                                self._editing_style, selected=True)
        search = self.session.ui.search
        current = search.current_result
        for index, match in enumerate(search.results):
            if match.page_index == page_index:
                self._paint_quads(painter, mapper, match.quads,
                                  QColor(255, 208, 55, 160 if index == current else 72),
                                  selected=index == current)
        selection = self.text_layer.visual_selection
        if selection is not None and self.text_layer.drag_page == page_index:
            self._paint_quads(painter, mapper, selection.quads, QColor(45, 120, 230, 92))
        if (self.text_layer.insertion_preview is not None
                and self.text_layer.drag_page == page_index):
            self._paint_caret(painter, mapper, *self.text_layer.insertion_preview, selected=True)
        for pending in self.pending_editorials.values():
            if pending.target.page_index == page_index:
                self._paint_editorial(painter, mapper, pending.kind, pending.target, selected=True)
        for pending in self.pending_inks.values():
            if pending.target.page_index == page_index:
                self._paint_ink(painter, mapper, pending.target, pending.style)
        for pending in self.pending_ink_moves.values():
            if pending.move.target.page_index == page_index:
                self._paint_ink(painter, mapper, pending.move.preview_target(), pending.move.style,
                                selected=pending.move.annotation_id == self.selected_ink_id)
        if self._ink_move is not None and self._ink_move.target.page_index == page_index:
            self._paint_ink(painter, mapper, self._ink_move.preview_target(), self._ink_move.style,
                            selected=True)
        for pending in self.pending_text_boxes.values():
            if pending.target.page_index == page_index:
                if isinstance(pending.target, RectTarget):
                    self._paint_text_box(painter, mapper, pending.target.rect, pending.rich_text,
                                         pending.style, selected=True)
                else:
                    self._paint_callout(painter, mapper, pending.target, pending.rich_text,
                                        pending.style, selected=True)
        if self._text_box_drag is not None and self._text_box_drag[0] == page_index:
            _page, start, end = self._text_box_drag
            self._paint_text_box_preview(painter, mapper, start, end, self.text_box_style)
        if (self._text_box_move is not None
                and self._text_box_move.preview_page_index == page_index):
            model = self.session.annotations.get(self._text_box_move.annotation_id)
            if model is not None and self._text_box_move.rect is not None:
                self._paint_text_box(painter, mapper, self._text_box_move.rect,
                                     model.rich_text or RichText.plain_text(
                                         model.text or "", font_family=model.style.font_family,
                                         font_size=model.style.font_size, color=model.style.text),
                                     model.style, selected=True)
        if self._callout_anchor is not None and self._callout_anchor[0] == page_index:
            anchor = self._callout_anchor[1]
            display = mapper.canonical_to_viewport((anchor.x, anchor.y))
            painter.setPen(QPen(QColor("#1452db"), 1.5))
            painter.setBrush(QColor("#d6e5ff"))
            painter.drawEllipse(QPointF(*display), 5, 5)
        if self._callout_drag is not None and self._callout_drag[0] == page_index:
            _page, anchor, end = self._callout_drag
            rect = self._initial_callout_rect(page_index, end, anchor)
            self._paint_callout_preview(painter, mapper,
                                        self._callout_target(page_index, rect, anchor),
                                        self.text_box_style)
        if (self._callout_move is not None
                and self._callout_move.preview_page_index == page_index
                and self._callout_move.rect is not None and self._callout_move.anchor is not None):
            model = self.session.annotations.get(self._callout_move.annotation_id)
            if model is not None and isinstance(model.style, FreeTextStyle):
                target = self._callout_target(page_index, self._callout_move.rect,
                                              self._callout_move.anchor)
                self._paint_callout(painter, mapper, target, model.rich_text or RichText.plain_text(
                    model.text or "", font_family=model.style.font_family,
                    font_size=model.style.font_size, color=model.style.text), model.style, selected=True)
        if self.pen_gesture.page_index == page_index and self._gesture_style is not None:
            points = self.pen_gesture.points
            if len(points) >= 2:
                self._paint_ink(painter, mapper,
                                InkTarget(page_index, (points,)), self._gesture_style)

    def _pen_point(self, page_index: int, point: QPointF) -> tuple[PdfPoint, tuple[float, float]]:
        rect = self.page_viewport_rect(page_index)
        x = min(rect.right(), max(rect.left(), point.x()))
        y = min(rect.bottom(), max(rect.top(), point.y()))
        canonical = self.coordinate_mapper(page_index).viewport_to_canonical((x, y))
        geometry = self.session.pages[page_index]
        canonical = (min(geometry.width_pt, max(0.0, canonical[0])),
                     min(geometry.height_pt, max(0.0, canonical[1])))
        return PdfPoint(*canonical), (x, y)

    def _canonical_clamped(self, page_index: int, point: QPointF) -> PdfPoint:
        rect = self.page_viewport_rect(page_index)
        x = min(rect.right(), max(rect.left(), point.x()))
        y = min(rect.bottom(), max(rect.top(), point.y()))
        canonical = self.coordinate_mapper(page_index).viewport_to_canonical((x, y))
        geometry = self.session.pages[page_index]
        return PdfPoint(min(geometry.width_pt, max(0.0, canonical[0])),
                        min(geometry.height_pt, max(0.0, canonical[1])))

    def _begin_text_box_editor(self, target: RectTarget | CalloutTarget, annotation_id: str | None,
                               style: FreeTextStyle, text: str = "",
                               rich_text: RichText | None = None, *, restoring: bool = False) -> None:
        self.cancel_text_box_edit()
        self._editing_text_box_id, self._editing_target, self._editing_style = annotation_id, target, style
        self._editing_rich_text = rich_text or (RichText.plain_text(
            text, font_family=style.font_family, font_size=style.font_size, color=style.text,
            bold=self.session.ui.text_box_bold, italic=self.session.ui.text_box_italic) if text else None)
        editor = _TextBoxEditor(self.viewport())
        editor.confirmed.connect(self._confirm_text_box_editor)
        editor.cancelled.connect(self.cancel_text_box_edit)
        editor.auto_confirm_requested.connect(self.try_confirm_text_box_editor)
        editor.textChanged.connect(self._editor_content_changed)
        editor.selectionChanged.connect(self._remember_editor_cursor)
        editor.selectionChanged.connect(self.selection_changed)
        editor.cursorPositionChanged.connect(self._remember_editor_cursor)
        self._text_editor = editor
        if annotation_id is not None and annotation_id.startswith("native:"):
            # Native FreeText geometry is authoritative. QTextEdit otherwise
            # keeps its platform minimum-size hint, which can turn a small
            # imported Callout into a viewport-covering editor. This is only
            # overlay presentation: it never grows the PDF Rect or bypasses
            # the fit guard at commit time.
            editor.setMinimumSize(0, 0)
            editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        if restoring:
            # A failure restores this editor synchronously from the result signal.
            # Ignore the construction-time focus transition so it cannot submit the
            # same rejected mutation recursively before the user can correct it.
            editor.retain_next_focus_loss()
        self._apply_text_editor_style()
        self._position_text_editor()
        self._grow_active_editor_to_fit(self._editing_rich_text)
        editor.show()
        editor.setFocus()

    def _remember_editor_cursor(self) -> None:
        if self._text_editor is not None:
            self._formatting_cursor = QTextCursor(self._text_editor.textCursor())

    def _editor_content_changed(self) -> None:
        if self._text_editor is not None and self._text_editor.toPlainText():
            try:
                self._editing_rich_text = self._editor_rich_text()
            except ValueError:
                # Keep the exact QTextDocument state editable. Confirmation
                # reports the validation error without poisoning unrelated UI
                # transitions or discarding the user's content.
                return
            self._grow_active_editor_to_fit(self._editing_rich_text)

    def _grow_active_editor_to_fit(self, rich_text: RichText | None = None) -> bool:
        """Grow an app-owned draft downwards only when its laid-out text needs it."""
        editor, target, style = self._text_editor, self._editing_target, self._editing_style
        if editor is None or target is None or style is None:
            return False
        # Imported profiles retain their externally verified geometry policy.
        if self._editing_text_box_id is not None and self._editing_text_box_id.startswith("native:"):
            return True
        if not editor.toPlainText():
            return True
        rich = rich_text or self._editor_rich_text()
        rect = self._editing_rect()
        required_height = self._required_text_height(target.page_index, rect.x1 - rect.x0, style, rich)
        if required_height <= rect.y1 - rect.y0 + .01:
            return True

        geometry = self.session.pages[target.page_index]
        height = min(required_height, geometry.height_pt)
        # Preserve width and the current/manual height as a floor. Reaching the
        # lower page boundary may move the box up just enough to retain a valid
        # canonical rectangle; neither case changes its Callout anchor.
        y0 = min(rect.y0, geometry.height_pt - height)
        y0 = max(0.0, y0)
        grown = PdfRect(rect.x0, y0, rect.x1, y0 + height)
        if isinstance(target, RectTarget):
            self._editing_target = RectTarget(target.page_index, grown)
        else:
            self._editing_target = self._callout_target(target.page_index, grown, target.anchor)
        self._position_text_editor()
        self.viewport().update()
        return required_height <= height + .01

    def _required_text_height(self, page_index: int, width: float,
                              style: FreeTextStyle, rich_text: RichText) -> float:
        """Measure with the same QTextDocument configuration used for painting."""
        zoom = self.coordinate_mapper(page_index).zoom
        document = QTextDocument()
        self._apply_rich_text_document(document, rich_text, zoom, style.opacity)
        document.setDocumentMargin(TEXT_BOX_CONTENT_PADDING)
        content_width = max(1.0, (width - 2 * style.border_width) * zoom)
        document.setTextWidth(content_width)
        content_height = document.documentLayout().documentSize().height()
        return ceil((content_height + 2 * style.border_width * zoom) / zoom)

    def _position_text_editor(self) -> None:
        if self._text_editor is None or self._editing_target is None:
            return
        rect = self._display_rect(self._editing_target.page_index, self._editing_rect())
        self._text_editor.setGeometry(rect.toAlignedRect())

    def _editing_rect(self) -> PdfRect:
        if isinstance(self._editing_target, RectTarget):
            return self._editing_target.rect
        if isinstance(self._editing_target, CalloutTarget):
            return self._editing_target.text_rect
        raise ValueError("No active FreeText target")

    def _apply_text_editor_style(self) -> None:
        if self._text_editor is None or self._editing_target is None or self._editing_style is None:
            return
        style = self._editing_style
        base_font = QFont(_editor_font_family(style.font_family))
        base_font.setPixelSize(max(1, round(style.font_size * self.coordinate_mapper(
            self._editing_target.page_index).zoom)))
        self._text_editor.setFont(base_font)
        self._apply_rich_text_document(self._text_editor.document(), self._editing_rich_text,
                                       self.coordinate_mapper(self._editing_target.page_index).zoom,
                                       style.opacity)
        if self._editing_rich_text is None:
            self._text_editor.setCurrentCharFormat(self._char_format(RichTextRun(
                "x", style.font_family, style.font_size, style.text,
                self.session.ui.text_box_bold, self.session.ui.text_box_italic),
                self.coordinate_mapper(self._editing_target.page_index).zoom))
        self._text_editor.document().setDocumentMargin(TEXT_BOX_CONTENT_PADDING)
        fill = ("transparent" if style.fill is None else
                "rgba(%d, %d, %d, %g)" % (round(style.fill.red * 255),
                                            round(style.fill.green * 255),
                                            round(style.fill.blue * 255), style.opacity))
        border = "rgb(%d, %d, %d)" % (round(style.border.red * 255),
                                        round(style.border.green * 255),
                                        round(style.border.blue * 255))
        text = "rgb(%d, %d, %d)" % (round(style.text.red * 255),
                                      round(style.text.green * 255),
                                      round(style.text.blue * 255))
        self._text_editor.setStyleSheet(
            "QTextEdit { background-color: %s; color: %s; border: %gpx solid %s; padding: 0px; }"
            % (fill, text, max(1.0, style.border_width * self.coordinate_mapper(
                self._editing_target.page_index).zoom), border))

    @staticmethod
    def _char_format(run: RichTextRun, zoom: float, opacity: float = 1.0) -> QTextCharFormat:
        fmt = QTextCharFormat()
        font = QFont(_editor_font_family(run.font_family))
        font.setPixelSize(max(1, round(run.font_size * zoom)))
        font.setWeight(QFont.Weight.Bold if run.bold else QFont.Weight.Normal)
        font.setItalic(run.italic)
        fmt.setFont(font)
        fmt.setForeground(QColor.fromRgbF(run.color.red, run.color.green, run.color.blue, opacity))
        fmt.setProperty(RICH_COLOR_PROPERTY, (run.color.red, run.color.green, run.color.blue))
        return fmt

    @classmethod
    def _apply_rich_text_document(cls, document: QTextDocument, rich_text: RichText | None,
                                  zoom: float, opacity: float = 1.0) -> None:
        if rich_text is None:
            return
        cursor = QTextCursor(document)
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        cursor.beginEditBlock()
        for run in rich_text.runs:
            cursor.insertText(run.text, cls._char_format(run, zoom, opacity))
        cursor.endEditBlock()

    def _apply_editor_character_format(self, *, font_family: str | None = None,
                                       font_size: float | None = None, color=None,
                                       bold: bool | None = None, italic: bool | None = None) -> None:
        """Apply to selection, or establish the deterministic next-insertion format."""
        if self._text_editor is None or self._editing_target is None:
            return
        saved = self._formatting_cursor
        cursor = (QTextCursor(saved) if self._use_saved_formatting_cursor and saved is not None
                  and saved.document() is self._text_editor.document()
                  else self._text_editor.textCursor())
        # A selection can cross differently-formatted runs.  Merge only the
        # requested attributes: deriving a complete QFont from the first
        # selected character would silently overwrite the other run fields.
        fmt = QTextCharFormat()
        zoom = self.coordinate_mapper(self._editing_target.page_index).zoom
        if font_family is not None:
            fmt.setFontFamilies([_editor_font_family(font_family)])
        if font_size is not None:
            fmt.setProperty(QTextFormat.Property.FontPixelSize,
                            max(1, round(font_size * zoom)))
        if color is not None:
            fmt.setForeground(QColor.fromRgbF(color.red, color.green, color.blue))
            fmt.setProperty(RICH_COLOR_PROPERTY, (color.red, color.green, color.blue))
        if bold is not None:
            fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        if italic is not None:
            fmt.setFontItalic(italic)
        self._text_editor.apply_character_format(fmt, cursor)
        self._formatting_cursor = QTextCursor(self._text_editor.textCursor())
        self._use_saved_formatting_cursor = False
        self._text_editor.clear_retained_focus_loss()
        self._text_editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self._editing_rich_text = self._editor_rich_text()
        self._grow_active_editor_to_fit(self._editing_rich_text)

    def _editor_font_size(self, fmt: QTextCharFormat) -> float:
        fallback = (self._editing_style.font_size if self._editing_style is not None
                    else self.session.ui.text_box_font_size)
        if self._editing_target is None:
            return fallback
        pixels = fmt.font().pixelSize()
        size = (pixels / self.coordinate_mapper(self._editing_target.page_index).zoom
                if pixels > 0 else fallback)
        return size if 6 <= size <= 72 else fallback

    def _editor_font_family(self, fmt: QTextCharFormat) -> str:
        family = _pdf_font_family(fmt.font().family() or "")
        if family in FREE_TEXT_FONT_FAMILIES:
            return family
        return (self._editing_style.font_family if self._editing_style is not None
                else self.session.ui.text_box_font_family)

    @staticmethod
    def _editor_color(fmt: QTextCharFormat):
        stored = fmt.property(RICH_COLOR_PROPERTY)
        if isinstance(stored, (tuple, list)) and len(stored) == 3:
            return RgbColor(*stored)
        color = fmt.foreground().color()
        return RgbColor(color.redF(), color.greenF(), color.blueF())

    def _editor_rich_text(self) -> RichText:
        if self._text_editor is None or self._editing_target is None:
            raise ValueError("No active text editor")
        runs: list[RichTextRun] = []
        document = self._text_editor.document()
        block = document.begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid() and fragment.text():
                    fmt = fragment.charFormat()
                    runs.append(RichTextRun(fragment.text(), self._editor_font_family(fmt),
                                            self._editor_font_size(fmt),
                                            self._editor_color(fmt),
                                            fmt.fontWeight() >= QFont.Weight.Bold, fmt.fontItalic()))
                iterator += 1
            if block.next().isValid():
                if runs:
                    last = runs.pop()
                    runs.append(RichTextRun(last.text + "\n", last.font_family, last.font_size,
                                            last.color, last.bold, last.italic))
            block = block.next()
        if not runs:
            style = self._editing_style
            if style is None:
                raise ValueError("No active text style")
            return RichText.plain_text("", font_family=style.font_family,
                                       font_size=style.font_size, color=style.text,
                                       bold=self.session.ui.text_box_bold,
                                       italic=self.session.ui.text_box_italic)
        return RichText(tuple(_coalesce_runs(runs)))

    def try_confirm_text_box_editor(self) -> bool:
        """Attempt ordinary auto-confirm without discarding invalid edit state."""
        return self._confirm_text_box_editor()

    def try_confirm_active_editor(self) -> bool:
        """Confirm the one active editor before a tab change or Save barrier."""
        if self._text_editor is not None:
            return self._confirm_text_box_editor()
        return self._confirm_editorial_editor()

    def _confirm_text_box_editor(self) -> bool:
        if self._text_editor is None or self._editing_target is None or self._editing_style is None:
            return False
        text = self._text_editor.toPlainText()
        target, style, annotation_id = (self._editing_target, self._editing_style,
                                        self._editing_text_box_id)
        try:
            rich_text = self._editor_rich_text()
        except ValueError:
            self.status_message("Il contenuto del testo non è valido.")
            self._text_editor.setFocus(Qt.FocusReason.OtherFocusReason)
            return False
        if not self._grow_active_editor_to_fit(rich_text):
            self.status_message("Il testo non entra nello spazio disponibile della pagina.")
            return False
        target = self._editing_target
        if target is None:
            return False
        context = CommandContext.create(self.session.document_id, self.session.document_generation,
                                        CommandSource.UI)
        if annotation_id is not None and annotation_id.startswith("native:"):
            return self.external_interaction.confirm_editor(annotation_id, rich_text, style)
        if annotation_id is None:
            command = (CreateFreeText(context, target, text, style, rich_text=rich_text)
                       if isinstance(target, RectTarget)
                       else CreateCallout(context, target, text, style, rich_text=rich_text))
        else:
            model = self.session.annotations.get(annotation_id)
            if model is None:
                self.status_message("Il riquadro non è più disponibile.")
                return False
            command = (UpdateFreeText(context, model.annotation_id, target.page_index, text, style,
                                      rich_text) if isinstance(target, RectTarget)
                       else UpdateCallout(context, model.annotation_id, target, text, style,
                                          rich_text))
        self.cancel_text_box_edit()
        return self._queue_text_box(command, target, text, rich_text, style)

    def status_message(self, message: str) -> None:
        self.text_error.emit(type("TextBoxMessage", (), {"message": message})())

    def _queue_text_box(self, command, target: RectTarget | CalloutTarget, text: str, rich_text: RichText,
                        style: FreeTextStyle, *, restore_editor: bool | None = None) -> bool:
        self.pending_text_boxes[command.context.command_id] = _PendingFreeText(target, text, rich_text, style,
                                                                                 (command.annotation_id.value
                                                                                   if not isinstance(command, (CreateFreeText,
                                                                                                               CreateCallout))
                                                                                  else None), perf_counter(),
                                                                                  (isinstance(command, (CreateFreeText,
                                                                                                        UpdateFreeText,
                                                                                                        CreateCallout,
                                                                                                        UpdateCallout))
                                                                                   if restore_editor is None else restore_editor))
        self.text_box_requested.emit(command)
        # See _queue_editorial: an immediate service rejection is handled
        # synchronously and must not be mistaken for a pending confirmation.
        return command.context.command_id in self.pending_text_boxes

    def _delete_selected_text_box(self) -> None:
        if self.pending_delete_id is not None or self.selected_text_box_id is None:
            return
        model = self.session.annotations.get(self.selected_text_box_id)
        if model is None or model.kind != AnnotationKind.TEXT_BOX:
            return
        command = DeleteAnnotation(CommandContext.create(self.session.document_id,
                                                          self.session.document_generation,
                                                          CommandSource.UI),
                                   model.annotation_id, model.page_index)
        self.pending_delete_id = command.context.command_id
        self.text_box_requested.emit(command)

    def _delete_selected_callout(self) -> None:
        if self.pending_delete_id is not None or self.selected_callout_id is None:
            return
        model = self.session.annotations.get(self.selected_callout_id)
        if model is None or model.kind != AnnotationKind.CALLOUT:
            return
        command = DeleteAnnotation(CommandContext.create(self.session.document_id,
                                                           self.session.document_generation,
                                                           CommandSource.UI),
                                   model.annotation_id, model.page_index)
        self.pending_delete_id = command.context.command_id
        self.text_box_requested.emit(command)

    def _delete_selected_text_markup(self) -> None:
        """Delete an app-created non-movable text markup via the common service."""
        if self.pending_delete_id is not None or self.selected_text_markup_id is None:
            return
        model = self.session.annotations.get(self.selected_text_markup_id)
        if (model is None or model.kind not in (NON_MOVABLE_TEXT_MARKUP_KINDS
                                                | {AnnotationKind.INSERT_TEXT})
                or model.ownership != AnnotationOwnership.CREATED_BY_APP
                or not isinstance(model.target, (TextRangeTarget, TextInsertionTarget))):
            return
        command = DeleteAnnotation(CommandContext.create(self.session.document_id,
                                                          self.session.document_generation,
                                                          CommandSource.UI),
                                   model.annotation_id, model.page_index)
        self.pending_delete_id = command.context.command_id
        if model.kind in (AnnotationKind.DELETE_TEXT, AnnotationKind.INSERT_TEXT,
                          AnnotationKind.REPLACE_TEXT):
            self.editorial_requested.emit(command)
        else:
            self.highlight_requested.emit(command)

    def _delete_selected_inventory(self) -> None:
        """Delete the selected native graph through the existing ownership route."""
        if self.pending_delete_id is not None or self.selected_inventory_id is None:
            return
        summary = self.session.annotation_inventory.get(self.selected_inventory_id)
        if summary is None or not summary.capabilities.deletable:
            self.status_message("Annotazione esterna non eliminabile in sicurezza.")
            return
        context = CommandContext.create(self.session.document_id, self.session.document_generation, CommandSource.UI)
        if summary.provenance == "app":
            model = self.session.annotations.get(summary.logical_id)
            if model is None or model.kind != AnnotationKind.STICKY_NOTE:
                return
            command = DeleteAnnotation(context, model.annotation_id, model.page_index)
        else:
            command = DeleteExternalAnnotation(context, external_command_annotation_id(summary.logical_id),
                                               summary.logical_id, summary.page_index)
        self.pending_delete_id = command.context.command_id
        # Reuse the common annotation signal; the document manager routes it
        # through AnnotationService and the persistent worker.
        self.text_box_requested.emit(command)

    def select_inventory_annotation(self, logical_id: str, *, reveal: bool = False) -> None:
        summary = self.session.annotation_inventory.get(logical_id)
        if summary is not None and is_attached_reply(summary, self.session.annotation_inventory):
            self.select_inventory_annotation(summary.reply.parent_logical_id, reveal=reveal)
            return
        if summary is None:
            return
        model = self.session.annotations.get(logical_id) if summary.provenance == "app" else None
        if model is not None and not _uses_inventory_overlay(summary):
            self.selected_inventory_id = None
            if model.kind == AnnotationKind.INK:
                self._set_selected_ink(logical_id)
            elif model.kind == AnnotationKind.TEXT_BOX:
                self._set_selected_text_box(logical_id)
            elif model.kind == AnnotationKind.CALLOUT:
                self._set_selected_callout(logical_id)
            else:
                self._set_selected_text_markup(logical_id)
            self.session.selected_annotation_id = logical_id
            self.inventory_annotation_selected.emit(logical_id)
        else:
            self._set_selected_inventory(logical_id)
        if reveal:
            target = self._display_rect(summary.page_index, summary.geometry.bbox)
            delta = target.center().y() - self.viewport().height() / 2
            self.verticalScrollBar().setValue(round(self.verticalScrollBar().value() + delta))
            # A horizontal reveal is useful in double layouts but remains
            # bounded by the ordinary scroll bar range.
            horizontal = target.center().x() - self.viewport().width() / 2
            self.horizontalScrollBar().setValue(round(self.horizontalScrollBar().value() + horizontal))

    def _set_selected_inventory(self, logical_id: str | None) -> None:
        if logical_id == self.selected_inventory_id:
            return
        self.selected_inventory_id = logical_id
        self.session.selected_annotation_id = logical_id
        if logical_id is not None:
            self.inventory_annotation_selected.emit(logical_id)
        self.selection_changed.emit()
        self.viewport().update()

    def _update_selected_text_style(self, *, fill: RgbColor | None | object = ...,
                                    border: RgbColor | None = None,
                                    delta: RichTextFormatDelta | None = None) -> None:
        annotation_id = self.selected_text_box_id or self.selected_callout_id
        if annotation_id is None or self.pending_text_boxes:
            return
        model = self.session.annotations.get(annotation_id)
        if (model is None or not isinstance(model.target, (RectTarget, CalloutTarget))
                or not isinstance(model.style, FreeTextStyle)):
            return
        style = model.style
        if fill is not ...:
            style = replace(style, fill=fill)
        if border is not None:
            style = replace(style, border=border)
        if delta is not None:
            if delta.color is not None:
                style = replace(style, text=delta.color)
            if delta.font_size is not None:
                style = replace(style, font_size=delta.font_size)
            if delta.font_family is not None:
                style = replace(style, font_family=delta.font_family)
        existing = model.rich_text or RichText.plain_text(
            model.text or "", font_family=model.style.font_family,
            font_size=model.style.font_size, color=model.style.text)
        rich_text = apply_rich_text_format_delta(existing, delta) if delta is not None else existing
        if style == model.style and rich_text == existing:
            return
        context = CommandContext.create(self.session.document_id,
                                        self.session.document_generation,
                                        CommandSource.UI)
        command = (UpdateFreeText(context, model.annotation_id, model.page_index,
                                  rich_text.plain, style, rich_text)
                   if isinstance(model.target, RectTarget)
                   else UpdateCallout(context, model.annotation_id, model.target,
                                      rich_text.plain, style, rich_text))
        self._queue_text_box(command, model.target, rich_text.plain, rich_text, style)

    def _set_selected_text_box(self, annotation_id: str | None) -> None:
        if annotation_id == self.selected_text_box_id:
            return
        self.selected_text_box_id = annotation_id
        model = self.session.annotations.get(annotation_id) if annotation_id is not None else None
        if model is not None and isinstance(model.style, FreeTextStyle):
            rich = model.rich_text or RichText.plain_text(
                model.text or "", font_family=model.style.font_family,
                font_size=model.style.font_size, color=model.style.text)
            first = rich.runs[0]
            self.session.ui.text_box_fill = next((name for name, color in TEXT_BOX_FILL_PRESETS
                                                  if color == model.style.fill), "transparent")
            self.session.ui.text_box_border = next((name for name, color in TEXT_BOX_BORDER_PRESETS
                                                    if color == model.style.border), "black")
            self.session.ui.text_box_text_color = next((name for name, color in PEN_COLOR_PRESETS
                                                        if color == first.color), "black")
            self.session.ui.text_box_font_family = first.font_family
            self.session.ui.text_box_font_size = first.font_size
            self.session.ui.text_box_bold = first.bold
            self.session.ui.text_box_italic = first.italic
        self.selection_changed.emit()
        self.viewport().update()

    def _set_selected_callout(self, annotation_id: str | None) -> None:
        if annotation_id == self.selected_callout_id:
            return
        self.selected_callout_id = annotation_id
        model = self.session.annotations.get(annotation_id) if annotation_id is not None else None
        if model is not None and isinstance(model.style, FreeTextStyle):
            rich = model.rich_text or RichText.plain_text(
                model.text or "", font_family=model.style.font_family,
                font_size=model.style.font_size, color=model.style.text)
            first = rich.runs[0]
            self.session.ui.text_box_fill = next((name for name, color in TEXT_BOX_FILL_PRESETS
                                                   if color == model.style.fill), "transparent")
            self.session.ui.text_box_border = next((name for name, color in TEXT_BOX_BORDER_PRESETS
                                                     if color == model.style.border), "black")
            self.session.ui.text_box_text_color = next((name for name, color in PEN_COLOR_PRESETS
                                                         if color == first.color), "black")
            self.session.ui.text_box_font_family = first.font_family
            self.session.ui.text_box_font_size = first.font_size
            self.session.ui.text_box_bold = first.bold
            self.session.ui.text_box_italic = first.italic
        self.selection_changed.emit()
        self.viewport().update()

    def _moving_text_box_ids(self) -> set[str]:
        ids = {pending.annotation_id for pending in self.pending_text_boxes.values()
               if pending.annotation_id is not None}
        if self._text_box_move is not None:
            ids.add(self._text_box_move.annotation_id)
        return ids

    def _moving_callout_ids(self) -> set[str]:
        ids = {pending.annotation_id for pending in self.pending_text_boxes.values()
               if pending.annotation_id is not None
               and isinstance(pending.target, CalloutTarget)}
        if self._callout_move is not None:
            ids.add(self._callout_move.annotation_id)
        return ids

    def _drop_page(self, point: QPointF) -> int:
        """Resolve a page even over gaps, making cross-page drops deterministic."""
        x, y = self._origin()
        return self.page_layout.page_at(y + point.y(), x + point.x())

    def _display_rect(self, page_index: int, rect: PdfRect) -> QRectF:
        mapper = self.coordinate_mapper(page_index)
        points = tuple(mapper.canonical_to_viewport(point) for point in
                       ((rect.x0, rect.y0), (rect.x1, rect.y0),
                        (rect.x0, rect.y1), (rect.x1, rect.y1)))
        return QRectF(min(point[0] for point in points), min(point[1] for point in points),
                      max(point[0] for point in points) - min(point[0] for point in points),
                      max(point[1] for point in points) - min(point[1] for point in points))

    def _hit_test_text_box(self, point: QPointF, page_index: int) -> str | None:
        for model in reversed(tuple(self.session.annotations.values())):
            if (model.page_index == page_index and model.kind == AnnotationKind.TEXT_BOX
                    and model.ownership == AnnotationOwnership.CREATED_BY_APP
                    and isinstance(model.target, RectTarget)
                    and self._display_rect(page_index, model.target.rect).contains(point)):
                return model.annotation_id.value
        return None

    def _hit_test_inventory(self, point: QPointF, page_index: int) -> str | None:
        for summary in reversed(tuple(self.session.annotation_inventory.values())):
            if (not _uses_inventory_overlay(summary) or summary.page_index != page_index
                    or not summary.capabilities.selectable
                    or is_attached_reply(summary, self.session.annotation_inventory)):
                continue
            if summary.kind == "line" and len(summary.geometry.line) == 2:
                if self._hit_test_native_line(summary, point):
                    return summary.logical_id
                continue
            rect = (note_icon_rect(self.coordinate_mapper(page_index), summary.geometry.bbox)
                    if summary.kind == "sticky_note" else
                    self._display_rect(page_index, summary.geometry.bbox).adjusted(-5, -5, 5, 5))
            if rect.contains(point):
                return summary.logical_id
        return None

    def _hit_test_native_line(self, summary: AnnotationSummary, point: QPointF) -> bool:
        mapper = self.coordinate_mapper(summary.page_index)
        a, b = (QPointF(*mapper.canonical_to_viewport((p.x, p.y))) for p in summary.geometry.line)
        width = max(0.0, float(summary.style.get("border", {}).get("width", 1)))
        tolerance = max(5.0, width * mapper.zoom / 2 + 3.0)
        return self._screen_segment_distance_sq(point, a, b) <= tolerance ** 2

    def _hit_test_inventory_editorial(self, point: QPointF,
                                      page_index: int) -> AnnotationSummary | None:
        """Read-only editorial hit areas mirror M11 without worker traffic."""
        mapper = self.coordinate_mapper(page_index)
        for summary in reversed(tuple(self.session.annotation_inventory.values())):
            if summary.page_index != page_index or summary.kind not in ("insert_text", "replace_text"):
                continue
            caret = summary.geometry.caret
            if caret is not None:
                top = QPointF(*mapper.canonical_to_viewport((caret[0].x, caret[0].y)))
                bottom = QPointF(*mapper.canonical_to_viewport((caret[1].x, caret[1].y)))
                if self._screen_segment_distance_sq(point, top, bottom) <= INSERT_CARET_HIT_RADIUS ** 2:
                    return summary
            for quad in summary.geometry.quads:
                polygon = QPolygonF([QPointF(*mapper.canonical_to_viewport((item.x, item.y)))
                                     for item in (quad.upper_left, quad.upper_right,
                                                  quad.lower_right, quad.lower_left)])
                if polygon.containsPoint(point, Qt.FillRule.OddEvenFill):
                    return summary
        return None

    def _paint_inventory_annotation(self, painter: QPainter, mapper: CoordinateMapper,
                                    summary: AnnotationSummary, selected: bool,
                                    *, retained_appearance_key: RenderKey | None = None) -> None:
        """Conservative Qt representation for arbitrary native annotations.

        Known markup and ink retain their actual geometry.  Other subtypes use
        a visible read-only bounding representation instead of pretending their
        PDF appearance can be edited losslessly.
        """
        if is_attached_reply(summary, self.session.annotation_inventory):
            return
        style = summary.style
        stroke = style.get("stroke") if isinstance(style, dict) else None
        fill = style.get("fill") if isinstance(style, dict) else None
        opacity = style.get("opacity", 1.0) if isinstance(style, dict) else 1.0
        def color(values, fallback: QColor) -> QColor:
            if isinstance(values, (list, tuple)) and len(values) >= 3:
                try:
                    return QColor.fromRgbF(float(values[0]), float(values[1]), float(values[2]),
                                           max(.15, min(1.0, float(opacity))))
                except (TypeError, ValueError):
                    pass
            return fallback
        line = color(stroke, QColor(220, 68, 68, 210))
        surface = color(fill, QColor(255, 229, 76, 105))
        if summary.kind == "highlight":
            values = stroke or []
            if len(values) == 1:
                values = values * 3
            elif len(values) == 4:
                c, m, y, k = values
                values = [1-min(1, c+k), 1-min(1, m+k), 1-min(1, y+k)]
            if len(values) == 3:
                surface = QColor.fromRgbF(*values, max(0.0, min(1.0, float(opacity))))
        if summary.kind in ("text_box", "callout", "line"):
            native_clip = summary.geometry.text_rect if summary.kind == "callout" else None
            if native_clip is None:
                native_painted = (self._paint_native_appearance(
                    painter, mapper, summary, retained_key=retained_appearance_key)
                    if retained_appearance_key is not None else
                    self._paint_native_appearance(painter, mapper, summary))
            else:
                native_painted = (self._paint_native_appearance(
                    painter, mapper, summary, clip_to=native_clip, retained_key=retained_appearance_key)
                    if retained_appearance_key is not None else
                    self._paint_native_appearance(painter, mapper, summary, clip_to=native_clip))
            if native_painted:
                # A FreeText appearance stream is clipped to its annotation
                # rectangle.  Native Callout /CL geometry can extend outside
                # that rectangle, so rendering the AP by itself can erase the
                # leader after an editor-triggered repaint.  Draw only the
                # portion of /CL not covered by the native AP raster; text,
                # fill, border and any in-Rect leader come solely from the AP.
                if summary.kind == "callout":
                    # The AP is box/text-only above. The Qt overlay owns the
                    # complete /CL and its one OpenArrow, avoiding an AP arrow
                    # plus an overlay arrow in an idle restored state.
                    self._paint_external_callout_leader(
                        painter, mapper, summary.geometry.callout_line, line,
                        max(1.0, mapper.zoom))
                if selected:
                    painter.save()
                    painter.setPen(QPen(QColor(45, 120, 230), 2.0))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(self._display_rect(summary.page_index,
                                                        summary.geometry.bbox).adjusted(-2, -2, 2, 2))
                    painter.restore()
                return
            if summary.provenance == "external" and (summary.appearance_available or summary.kind == "line"):
                # An opaque external AP is authoritative. Never repaint it
                # through the generic semantic FreeText path while its native
                # raster is pending: that fallback is the transient red flash.
                return
        if summary.kind in ("delete_text", "insert_text", "replace_text"):
            self._paint_external_editorial(painter, mapper, summary, selected=selected)
        elif summary.kind == "highlight":
            if summary.geometry.quads:
                self._paint_quads(painter, mapper, summary.geometry.quads, surface, selected=selected)
            else:
                painter.save()
                painter.setPen(QPen(QColor(20, 125, 230), 1.5) if selected else Qt.PenStyle.NoPen)
                painter.setBrush(surface)
                painter.drawRect(self._display_rect(summary.page_index, summary.geometry.bbox))
                painter.restore()
        elif summary.kind == "ink":
            self._paint_external_ink(painter, mapper, summary, selected=selected)
        elif summary.kind in ("text_box", "callout"):
            self._paint_external_freetext(painter, mapper, summary, selected=selected)
        elif summary.kind in ("underline", "squiggly", "strikeout"):
            painter.save()
            painter.setPen(QPen(line, 2.0 if selected else 1.2))
            quads = summary.geometry.quads
            if not quads:
                rect = summary.geometry.bbox
                painter.drawLine(QPointF(*mapper.canonical_to_viewport((rect.x0, (rect.y0 + rect.y1) / 2))),
                                 QPointF(*mapper.canonical_to_viewport((rect.x1, (rect.y0 + rect.y1) / 2))))
            for quad in quads:
                a = QPointF(*mapper.canonical_to_viewport((quad.upper_left.x, quad.upper_left.y)))
                b = QPointF(*mapper.canonical_to_viewport((quad.lower_right.x, quad.lower_right.y)))
                y = (a.y() + b.y()) / 2
                painter.drawLine(QPointF(a.x(), y), QPointF(b.x(), y))
            painter.restore()
        elif summary.kind == "sticky_note":
            paint_note_icon(painter, note_icon_rect(mapper, summary.geometry.bbox), selected=selected)
        else:
            rect = self._display_rect(summary.page_index, summary.geometry.bbox)
            painter.save()
            pen = QPen(line, 2.0 if selected else 1.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            if summary.kind == "sticky_note":
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "N")
            else:
                painter.drawText(rect.adjusted(2, 2, -2, -2), Qt.AlignmentFlag.AlignTop |
                                 Qt.AlignmentFlag.AlignLeft, summary.subtype or "Annotazione")
            painter.restore()
        if selected and summary.kind != "sticky_note":
            painter.save()
            painter.setPen(QPen(QColor(45, 120, 230), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._display_rect(summary.page_index, summary.geometry.bbox).adjusted(-2, -2, 2, 2))
            painter.restore()

    @staticmethod
    def _inventory_color(value, fallback: RgbColor) -> RgbColor:
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            try:
                return RgbColor(float(value[0]), float(value[1]), float(value[2]))
            except (TypeError, ValueError):
                pass
        return fallback

    def _paint_external_ink(self, painter: QPainter, mapper: CoordinateMapper,
                            summary: AnnotationSummary, *, selected: bool) -> None:
        color = self._inventory_color(summary.style.get("stroke"), RgbColor(.86, .12, .12))
        opacity = summary.style.get("opacity", 1.0)
        try:
            style = InkStyle(color, opacity=max(0.0, min(1.0, float(opacity))))
            target = InkTarget(summary.page_index, summary.geometry.paths)
        except (TypeError, ValueError):
            # Retain visibility for a syntactically supported but malformed
            # Ink object; only unknown subtypes use the generic renderer.
            painter.save()
            painter.setPen(QPen(QColor(220, 68, 68), 1.2))
            painter.drawRect(self._display_rect(summary.page_index, summary.geometry.bbox))
            painter.restore()
            return
        self._paint_ink(painter, mapper, target, style, selected=selected)

    def _paint_external_freetext(self, painter: QPainter, mapper: CoordinateMapper,
                                 summary: AnnotationSummary, *, selected: bool) -> None:
        border = self._inventory_color(summary.style.get("stroke"), RgbColor(.15, .15, .15))
        fill_values = summary.style.get("fill")
        fill = self._inventory_color(fill_values, border) if fill_values else None
        try:
            text = self._inventory_color(summary.style.get("text"), RgbColor(0, 0, 0))
            style = FreeTextStyle(border, fill, text,
                                  opacity=max(0.0, min(1.0, float(summary.style.get("opacity", 1.0)))),
                                  font_size=10.0)
            rich = RichText.plain_text(summary.preview, font_family=style.font_family,
                                       font_size=style.font_size, color=style.text)
            text_rect = summary.geometry.text_rect or summary.geometry.bbox
            if summary.kind == "text_box":
                self._paint_text_box(painter, mapper, text_rect, rich, style, selected=selected)
            elif len(summary.geometry.callout_line) >= 2:
                self._paint_external_callout(painter, mapper, text_rect, summary.geometry.callout_line,
                                             rich, style, selected=selected)
            else:
                self._paint_text_box(painter, mapper, text_rect, rich, style, selected=selected)
        except (TypeError, ValueError):
            # A non-standard external FreeText remains visible through the
            # generic geometry fallback above its normal renderer path.
            painter.save()
            painter.setPen(QPen(QColor(220, 68, 68), 1.2))
            painter.drawRect(self._display_rect(summary.page_index, summary.geometry.bbox))
            painter.restore()

    def _paint_external_callout(self, painter: QPainter, mapper: CoordinateMapper, text_rect: PdfRect,
                                line: tuple[PdfPoint, ...], rich: RichText,
                                style: FreeTextStyle, *, selected: bool) -> None:
        """Fallback preserving Acrobat's /CL points without app-callout heuristics."""
        self._paint_external_callout_leader(
            painter, mapper, line,
            QColor.fromRgbF(style.border.red, style.border.green, style.border.blue, style.opacity),
            max(1.0, style.border_width * mapper.zoom))
        self._paint_text_box(painter, mapper, text_rect, rich, style, selected=selected)

    def _paint_external_callout_leader(self, painter: QPainter, mapper: CoordinateMapper,
                                       line: tuple[PdfPoint, ...], color: QColor, width: float,
                                       *, covered: tuple[float, float, float, float] | None = None) -> None:
        """Paint just the native callout line; never synthesize its text appearance."""
        if len(line) < 2:
            return
        points = [QPointF(*mapper.canonical_to_viewport((point.x, point.y))) for point in line]
        arrow_toward = self._open_arrow_toward(points[0], points[1:])
        arrow_bounds = self._open_arrow_paint_bounds(points[0], arrow_toward, width)
        paint_bounds = self._leader_paint_bounds(points, arrow_bounds, width)
        viewport_bounds = QRectF(self.viewport().rect())
        self._last_callout_leader_paint = _CalloutLeaderPaint(
            line[0], (paint_bounds.x(), paint_bounds.y(), paint_bounds.width(), paint_bounds.height()),
            (arrow_bounds.x(), arrow_bounds.y(), arrow_bounds.width(), arrow_bounds.height()),
            (viewport_bounds.x(), viewport_bounds.y(), viewport_bounds.width(), viewport_bounds.height()))
        painter.save()
        try:
            if covered is not None:
                x0, y0, x1, y1 = covered
                corners = (mapper.canonical_to_viewport((x0, y0)),
                           mapper.canonical_to_viewport((x1, y1)))
                native_rect = QRectF(min(corners[0][0], corners[1][0]),
                                     min(corners[0][1], corners[1][1]),
                                     abs(corners[1][0] - corners[0][0]),
                                     abs(corners[1][1] - corners[0][1]))
                outside = QPainterPath()
                outside.addRect(viewport_bounds)
                outside.addRect(native_rect)
                outside.setFillRule(Qt.FillRule.OddEvenFill)
                painter.setClipPath(outside, Qt.ClipOperation.ReplaceClip)
            painter.setPen(QPen(color, width))
            painter.drawPolyline(QPolygonF(points))
            # Do not add a second arrowhead when the native AP already owns
            # the anchor.  The exterior clip retains one only when it lies
            # beyond that AP coverage.
            self._paint_open_arrow(painter, points[0], arrow_toward)
        finally:
            painter.restore()

    def _appearance_key(self, summary: AnnotationSummary) -> RenderKey:
        # Native annotation appearances have their own mutation identity.  A
        # document revision also changes for unrelated annotations, so using
        # it here would turn every visible external AP into a cache miss.
        # Mutation deltas explicitly retire the affected logical IDs below.
        return RenderKey(self.session.document_id, self.session.document_generation,
                         summary.page_index,
                         requested_raster_scale(self.scheduler.zoom, self.scheduler.dpr),
                         self.scheduler.dpr,
                         annotation_id=summary.logical_id)

    def invalidate_annotation_appearances(self, logical_ids: tuple[str, ...]) -> None:
        """Retire precisely the native APs changed by a worker mutation."""
        affected = set(logical_ids)
        if not affected:
            return
        ticket = self._appearance_pending
        if ticket is not None and ticket.logical_id in affected:
            # The one in-flight AP was rendered from the pre-mutation native
            # object.  Its stable logical cache key must not let it adopt after
            # this cache entry has been retired.
            self._appearance_epoch += 1
            if self.session.worker is not None:
                self.session.worker.cancel_annotation_appearances()
        for key in self.scheduler.budget.keys:
            if (key.document_id == self.session.document_id
                    and key.generation == self.session.document_generation
                    and isinstance(key.annotation_id, str)
                    and any(key.annotation_id == logical_id
                            or key.annotation_id.startswith(f"{logical_id}@")
                            for logical_id in affected)):
                self.scheduler.budget.remove(key)
                sources = getattr(self, "_appearance_sources", None)
                if sources is not None:
                    sources.pop(key, None)
                self._appearance_errors.discard(key)
        self._history_appearance_invalidations.difference_update(affected)
        self.viewport().update()

    def apply_history_delta(self, delta) -> None:
        """Keep selection unless its one logical target disappeared."""
        # An ordinary UI external edit also has a HistoryDelta so Undo can
        # later restore it. Its pending gesture owns the explicit predecessor
        # raster; only a delta with no such command is an Undo/Redo restore.
        if self.external_interaction.pending is None:
            self.external_interaction.history_delta(delta)
            self._history_appearance_invalidations.update(delta.appearance_invalidated_ids)
            self._history_native_appearance_pending.update(delta.appearance_invalidated_ids)
            if delta.appearance_invalidated_ids and self.session.worker is not None:
                # A restore has no AP identity in the GUI domain. Drop any
                # queued response rather than admitting it under its revision.
                self._appearance_epoch += 1
                self.session.worker.cancel_annotation_appearances()
        removed = set(delta.removed_ids) | set(delta.inventory_removed_ids)
        if self.selected_inventory_id in removed:
            self._set_selected_inventory(None)
        if self.selected_ink_id in removed:
            self._set_selected_ink(None)
        if self.selected_text_markup_id in removed:
            self._set_selected_text_markup(None)
        if self.selected_text_box_id in removed:
            self._set_selected_text_box(None)
        if self.selected_callout_id in removed:
            self._set_selected_callout(None)

    def _paint_native_appearance(self, painter: QPainter, mapper: CoordinateMapper,
                                 summary: AnnotationSummary, *, clip_to: PdfRect | None = None,
                                 retained_key: RenderKey | None = None) -> bool:
        entry = self._native_appearance_entry(summary, retained_key=retained_key)
        if entry is None or entry.coverage is None:
            return False
        x0, y0, x1, y1 = entry.coverage
        corners = (mapper.canonical_to_viewport((x0, y0)), mapper.canonical_to_viewport((x1, y1)))
        target = QRectF(min(corners[0][0], corners[1][0]), min(corners[0][1], corners[1][1]),
                        abs(corners[1][0] - corners[0][0]), abs(corners[1][1] - corners[0][1]))
        mapped = painter.transform().mapRect(target)
        current_key = self._appearance_key(summary)
        self._last_native_appearance_paint = _NativeAppearancePaint(
            summary.logical_id, self.session.document_revision, current_key, entry.key,
            "native_ap_raster" if entry.key == current_key else "retained_native_ap_raster",
            (entry.image.width(), entry.image.height()), tuple(entry.coverage), summary.geometry.bbox,
            summary.geometry.callout_line, clip_to,
            (mapped.x(), mapped.y(), mapped.width(), mapped.height()))
        painter.save()
        try:
            if clip_to is not None:
                painter.setClipRect(self._display_rect(summary.page_index, clip_to),
                                    Qt.ClipOperation.IntersectClip)
            with self.scheduler.budget.borrow(entry.key) as borrowed:
                if borrowed is None:
                    return False
                painter.drawImage(target, borrowed.image)
        finally:
            painter.restore()
        return True

    def _has_current_native_appearance(self, summary: AnnotationSummary) -> bool:
        """Whether the cache owns this annotation's exact current AP revision."""
        key = self._appearance_key(summary)
        return self.scheduler.budget.get(key, record=False) is not None

    def _native_appearance_coverage(self, summary: AnnotationSummary, *,
                                    retained_key: RenderKey | None = None) -> tuple[float, float, float, float] | None:
        """Return the exact cached AP coverage used by the previous paint."""
        entry = self._native_appearance_entry(summary, retained_key=retained_key)
        return tuple(entry.coverage) if entry is not None and entry.coverage is not None else None

    def _native_appearance_entry(self, summary: AnnotationSummary, *,
                                 retained_key: RenderKey | None = None):
        """Return the exact current AP, or an explicit live-gesture source."""
        key = self._appearance_key(summary)
        entry = self.scheduler.budget.get(key, record=False) or self.scheduler.budget.preview(key)
        if entry is not None:
            return entry
        if retained_key is not None:
            return (self.scheduler.budget.get(retained_key, record=False)
                    or self.scheduler.budget.preview(retained_key))
        return None

    def _appearance_targets(self) -> tuple[AnnotationSummary, ...]:
        pages = self.scheduler.visible + self.scheduler.nearby
        return tuple(summary for summary in self.session.annotation_inventory.values()
                     if (summary.provenance == "external" and summary.kind in ("text_box", "callout", "line")
                         and (summary.appearance_available or summary.kind == "line")
                         and summary.page_index in pages))

    def _tick_annotation_appearances(self) -> None:
        if self.scheduler.epoch != self._appearance_scheduler_epoch:
            self._appearance_scheduler_epoch = self.scheduler.epoch
            self._appearance_epoch += 1
        worker = self.session.worker
        ticket = self._appearance_pending
        if ticket is not None and worker is not None:
            delivery = worker.poll(ticket.request_id)
            if delivery is not None:
                with delivery:
                    response = delivery.response
                    if (response is not None and response.success and ticket.epoch == self._appearance_epoch
                            and response.operation == Operation.ANNOTATION_APPEARANCE
                            and response.document_id == self.session.document_id
                            and response.document_generation == self.session.document_generation
                            and response.request_id == ticket.request_id
                            and response.payload.get("logical_id") == ticket.logical_id):
                        cost = response.payload["stride"] * response.payload["height"]
                        if cost <= ticket.max_bytes and self.scheduler.budget.can_admit(ticket.key, cost):
                            coverage = tuple(response.payload["coverage"])
                            try:
                                admitted = self.scheduler.budget.put(ticket.key, cost,
                                                                     response.payload["raster_scale"],
                                                                     lambda: adopt_raster(delivery), coverage)
                                if admitted:
                                    self._appearance_sources[ticket.key] = ticket.source_summary
                                    self._history_native_appearance_pending.discard(ticket.logical_id)
                                    resident = set(self.scheduler.budget.keys)
                                    self._appearance_sources = {
                                        cached: source for cached, source in self._appearance_sources.items()
                                        if cached in resident}
                                else:
                                    self._appearance_errors.add(ticket.key)
                            except (KeyError, TypeError, ValueError, MemoryError):
                                self._appearance_errors.add(ticket.key)
                        else:
                            self._appearance_errors.add(ticket.key)
                self.viewport().update()
            if not worker.is_pending(ticket.request_id):
                self._appearance_pending = None
        if (self._appearance_pending is not None or not self.scheduler.active or worker is None
                or worker.failure or self.scheduler.interaction_pending):
            return
        for summary in self._appearance_targets():
            key = self._appearance_key(summary)
            if self.scheduler.budget.get(key, record=False) is not None or key in self._appearance_errors:
                continue
            limit = self.scheduler.raster_limit
            if not self.scheduler.budget.can_admit(key, limit):
                return
            request_id = worker.submit(Operation.ANNOTATION_APPEARANCE, {
                "logical_id": summary.logical_id, "page_index": summary.page_index,
                "render_scale": key.scale, "max_bytes": limit})
            if request_id is not None:
                self._appearance_pending = _AppearanceTicket(key, summary.logical_id, request_id,
                                                             self._appearance_epoch, limit, summary)
            return

    def _paint_external_editorial(self, painter: QPainter, mapper: CoordinateMapper,
                                  summary: AnnotationSummary, *, selected: bool) -> None:
        if summary.kind in ("delete_text", "replace_text"):
            painter.save()
            painter.setPen(QPen(QColor(20, 125, 230) if selected else QColor(222, 70, 70), 1.8))
            quads = summary.geometry.quads
            if not quads:
                rect = summary.geometry.bbox
                y = (rect.y0 + rect.y1) / 2
                painter.drawLine(QPointF(*mapper.canonical_to_viewport((rect.x0, y))),
                                 QPointF(*mapper.canonical_to_viewport((rect.x1, y))))
            for quad in quads:
                left = ((quad.upper_left.x + quad.lower_left.x) / 2,
                        (quad.upper_left.y + quad.lower_left.y) / 2)
                right = ((quad.upper_right.x + quad.lower_right.x) / 2,
                         (quad.upper_right.y + quad.lower_right.y) / 2)
                painter.drawLine(QPointF(*mapper.canonical_to_viewport(left)),
                                 QPointF(*mapper.canonical_to_viewport(right)))
            painter.restore()
        if summary.kind in ("insert_text", "replace_text") and summary.geometry.caret is not None:
            self._paint_caret(painter, mapper, *summary.geometry.caret, selected=selected)
        elif summary.kind in ("insert_text", "replace_text"):
            rect = summary.geometry.bbox
            center = (rect.x0 + rect.x1) / 2
            self._paint_caret(painter, mapper, PdfPoint(center, rect.y0),
                              PdfPoint(center, rect.y1), selected=selected)

    def _hit_test_non_movable_text_markup(self, point: QPointF, page_index: int) -> str | None:
        """Hit real text quads, never the permissive union bounding rectangle."""
        mapper = self.coordinate_mapper(page_index)
        for model in reversed(tuple(self.session.annotations.values())):
            if (model.page_index != page_index or model.kind not in NON_MOVABLE_TEXT_MARKUP_KINDS
                    or model.ownership != AnnotationOwnership.CREATED_BY_APP
                    or not isinstance(model.target, TextRangeTarget)):
                continue
            for quad in model.target.quads:
                perimeter = (quad.upper_left, quad.upper_right,
                             quad.lower_right, quad.lower_left)
                polygon = QPolygonF([QPointF(*mapper.canonical_to_viewport((item.x, item.y)))
                                     for item in perimeter])
                if polygon.containsPoint(point, Qt.FillRule.OddEvenFill):
                    return model.annotation_id.value
        return None

    def _hit_test_editorial_insert(self, point: QPointF, page_index: int) -> str | None:
        """Caret hit testing stays screen-space so it remains usable at any zoom."""
        mapper = self.coordinate_mapper(page_index)
        for model in reversed(tuple(self.session.annotations.values())):
            if (model.page_index != page_index or model.kind != AnnotationKind.INSERT_TEXT
                    or model.ownership != AnnotationOwnership.CREATED_BY_APP
                    or not isinstance(model.target, TextInsertionTarget)):
                continue
            top = QPointF(*mapper.canonical_to_viewport((model.target.boundary_top.x,
                                                          model.target.boundary_top.y)))
            bottom = QPointF(*mapper.canonical_to_viewport((model.target.anchor.x,
                                                             model.target.anchor.y)))
            if self._screen_segment_distance_sq(point, top, bottom) <= INSERT_CARET_HIT_RADIUS ** 2:
                return model.annotation_id.value
        return None

    @staticmethod
    def _screen_segment_distance_sq(point: QPointF, start: QPointF, end: QPointF) -> float:
        dx, dy = end.x() - start.x(), end.y() - start.y()
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return (point.x() - start.x()) ** 2 + (point.y() - start.y()) ** 2
        ratio = max(0.0, min(1.0, ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy)
                               / length_sq))
        x, y = start.x() + ratio * dx, start.y() + ratio * dy
        return (point.x() - x) ** 2 + (point.y() - y) ** 2

    def _hit_test_callout(self, point: QPointF, page_index: int) -> str | None:
        for model in reversed(tuple(self.session.annotations.values())):
            if (model.page_index == page_index and model.kind == AnnotationKind.CALLOUT
                    and model.ownership == AnnotationOwnership.CREATED_BY_APP
                    and isinstance(model.target, CalloutTarget)
                    and (self._display_rect(page_index, model.target.text_rect).contains(point)
                         or self._callout_anchor_hit(point, page_index, model.target))):
                return model.annotation_id.value
        return None

    def _callout_anchor_hit(self, point: QPointF, page_index: int, target: CalloutTarget) -> bool:
        anchor = self.coordinate_mapper(page_index).canonical_to_viewport(
            (target.anchor.x, target.anchor.y))
        return ((anchor[0] - point.x()) ** 2 + (anchor[1] - point.y()) ** 2
                <= CALLOUT_ANCHOR_HIT_RADIUS ** 2)

    def _initial_callout_rect(self, page_index: int, position: PdfPoint,
                              anchor: PdfPoint) -> PdfRect:
        """Return the compact second-click Callout box, kept clear of its anchor."""
        geometry = self.session.pages[page_index]
        width = min(CALLOUT_INITIAL_WIDTH * CALLOUT_INITIAL_WIDTH_SCALE,
                    geometry.width_pt)
        # Measure one real editor line rather than estimating it from a font
        # size. The draft and the editor consequently agree from the first
        # Callout click, including family/weight/italic metrics.
        style = self.text_box_style
        one_line = RichText.plain_text("M", font_family=style.font_family,
                                       font_size=style.font_size, color=style.text,
                                       bold=self.session.ui.text_box_bold,
                                       italic=self.session.ui.text_box_italic)
        height = min(max(TEXT_BOX_MIN_SIZE,
                         self._required_text_height(page_index, width, style, one_line)),
                     geometry.height_pt)

        def rect_at(x0: float, y0: float) -> PdfRect:
            x0 = min(geometry.width_pt - width, max(0.0, x0))
            y0 = min(geometry.height_pt - height, max(0.0, y0))
            return PdfRect(x0, y0, x0 + width, y0 + height)

        def clears_anchor(rect: PdfRect) -> bool:
            return not (rect.x0 - CALLOUT_ANCHOR_CLEARANCE < anchor.x
                        < rect.x1 + CALLOUT_ANCHOR_CLEARANCE
                        and rect.y0 - CALLOUT_ANCHOR_CLEARANCE < anchor.y
                        < rect.y1 + CALLOUT_ANCHOR_CLEARANCE)

        preferred = rect_at(position.x, position.y)
        if clears_anchor(preferred):
            return preferred
        # Respect the user's intended side first, then use the other three
        # deterministic positions. This leaves a visible leader even when the
        # second click is close to or directly over the anchor.
        horizontal = ((anchor.x + CALLOUT_ANCHOR_CLEARANCE, position.y)
                      if position.x >= anchor.x else
                      (anchor.x - CALLOUT_ANCHOR_CLEARANCE - width, position.y))
        opposite_horizontal = ((anchor.x - CALLOUT_ANCHOR_CLEARANCE - width, position.y)
                               if position.x >= anchor.x else
                               (anchor.x + CALLOUT_ANCHOR_CLEARANCE, position.y))
        vertical = ((position.x, anchor.y + CALLOUT_ANCHOR_CLEARANCE)
                    if position.y >= anchor.y else
                    (position.x, anchor.y - CALLOUT_ANCHOR_CLEARANCE - height))
        opposite_vertical = ((position.x, anchor.y - CALLOUT_ANCHOR_CLEARANCE - height)
                             if position.y >= anchor.y else
                             (position.x, anchor.y + CALLOUT_ANCHOR_CLEARANCE))
        for candidate in (horizontal, opposite_horizontal, vertical, opposite_vertical):
            rect = rect_at(*candidate)
            if clears_anchor(rect):
                return rect
        return preferred

    def _callout_target(self, page_index: int, rect: PdfRect, anchor: PdfPoint) -> CalloutTarget:
        """Derive the knee from anchor and box attachment; it is never independently edited."""
        return CalloutTarget(page_index, rect, anchor, callout_line(rect, anchor)[1])

    def _preview_callout_anchor(self, move: _CalloutMove, destination: int,
                                rect: PdfRect) -> PdfPoint:
        if destination == move.target.page_index:
            # MoveCallout is a box move: the anchor remains on its original
            # page point and only the leader/knee follows the box.
            return move.target.anchor
        dx, dy = rect.x0 - move.target.text_rect.x0, rect.y0 - move.target.text_rect.y0
        geometry = self.session.pages[destination]
        return PdfPoint(min(geometry.width_pt, max(0.0, move.target.anchor.x + dx)),
                        min(geometry.height_pt, max(0.0, move.target.anchor.y + dy)))

    def _preview_callout_rect(self, move: _CalloutMove, current: PdfPoint,
                              destination: int, rich_text: RichText,
                              style: FreeTextStyle) -> PdfRect:
        proxy = _FreeTextMove(move.annotation_id,
                              RectTarget(move.target.page_index, move.target.text_rect),
                              move.start, None if move.action == "move" else move.action,
                              move.rect, move.preview_page_index, move.dragging)
        return self._preview_text_box_rect(proxy, current, destination, rich_text, style)

    def _resize_corner(self, point: QPointF, page_index: int, rect: PdfRect) -> str | None:
        display = self._display_rect(page_index, rect)
        corners = {"tl": display.topLeft(), "tr": display.topRight(),
                   "bl": display.bottomLeft(), "br": display.bottomRight()}
        return next((name for name, corner in corners.items()
                     if (corner.x() - point.x()) ** 2 + (corner.y() - point.y()) ** 2
                     <= TEXT_BOX_RESIZE_HANDLE_HIT_RADIUS ** 2), None)

    def _preview_text_box_rect(self, move: _FreeTextMove, current: PdfPoint,
                               destination: int, rich_text: RichText,
                               style: FreeTextStyle) -> PdfRect:
        original = move.target.rect
        geometry = self.session.pages[destination]
        if move.resizing_corner is None:
            width, height = original.x1 - original.x0, original.y1 - original.y0
            if width > geometry.width_pt or height > geometry.height_pt:
                move.preview_page_index = move.target.page_index
                return original
            x0 = min(geometry.width_pt - width, max(0.0, current.x - (move.start.x - original.x0)))
            y0 = min(geometry.height_pt - height, max(0.0, current.y - (move.start.y - original.y0)))
            return PdfRect(x0, y0, x0 + width, y0 + height)
        x0, y0, x1, y1 = original.x0, original.y0, original.x1, original.y1
        if "l" in move.resizing_corner:
            x0 = min(x1 - 12, max(0, current.x))
        else:
            x1 = max(x0 + 12, min(geometry.width_pt, current.x))
        if "t" in move.resizing_corner:
            y0 = min(y1 - 12, max(0, current.y))
        else:
            y1 = max(y0 + 12, min(geometry.height_pt, current.y))
        requested = PdfRect(x0, y0, x1, y1)
        return self._clamp_resize_rect(original, requested, rich_text, style)

    def _update_freetext_hover_cursor(self, point: QPointF) -> None:
        """Expose the same screen-space resize affordance for Text Box and Callout."""
        if self.pen_enabled or self.text_box_enabled or self.callout_enabled:
            return
        for model in reversed(tuple(self.session.annotations.values())):
            if (model.ownership != AnnotationOwnership.CREATED_BY_APP
                    or not isinstance(model.target, (RectTarget, CalloutTarget))):
                continue
            rect = (model.target.rect if isinstance(model.target, RectTarget)
                    else model.target.text_rect)
            corner = self._resize_corner(point, model.page_index, rect)
            if corner in ("tl", "br"):
                self.viewport().setCursor(Qt.CursorShape.SizeFDiagCursor)
                return
            if corner in ("tr", "bl"):
                self.viewport().setCursor(Qt.CursorShape.SizeBDiagCursor)
                return
        if not self.external_interaction.hover_cursor(point):
            self.viewport().unsetCursor()

    @staticmethod
    def _clamp_resize_rect(original: PdfRect, requested: PdfRect, rich_text: RichText,
                           style: FreeTextStyle) -> PdfRect:
        """Geometry is limited by the page and minimum size, never text fit."""
        return requested

    def _set_selected_ink(self, annotation_id: str | None) -> None:
        if annotation_id == self.selected_ink_id:
            return
        self.selected_ink_id = annotation_id
        self.selection_changed.emit()
        self.viewport().update()

    def _set_selected_text_markup(self, annotation_id: str | None) -> None:
        if annotation_id == self.selected_text_markup_id:
            return
        self.selected_text_markup_id = annotation_id
        self.selection_changed.emit()
        self.viewport().update()

    def _moving_ink_ids(self) -> set[str]:
        ids = {pending.move.annotation_id for pending in self.pending_ink_moves.values()}
        if self._ink_move is not None:
            ids.add(self._ink_move.annotation_id)
        return ids

    def _hit_test_ink(self, point: QPointF, page_index: int) -> str | None:
        mapper = self.coordinate_mapper(page_index)
        for model in reversed(tuple(self.session.annotations.values())):
            if (model.page_index != page_index or model.kind != AnnotationKind.INK
                    or model.ownership != AnnotationOwnership.CREATED_BY_APP
                    or not isinstance(model.target, InkTarget)
                    or not isinstance(model.style, InkStyle)):
                continue
            limit = max(6.0, model.style.width * mapper.zoom / 2 + 4.0)
            limit_sq = limit * limit
            for path in model.target.paths:
                display = tuple(QPointF(*mapper.canonical_to_viewport((p.x, p.y)))
                                for p in path)
                if any(self._segment_distance_sq(point, a, b) <= limit_sq
                       for a, b in zip(display, display[1:])):
                    return model.annotation_id.value
        return None

    @staticmethod
    def _segment_distance_sq(point: QPointF, a: QPointF, b: QPointF) -> float:
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if dx == 0.0 and dy == 0.0:
            return (point.x() - a.x()) ** 2 + (point.y() - a.y()) ** 2
        ratio = ((point.x() - a.x()) * dx + (point.y() - a.y()) * dy) / (dx * dx + dy * dy)
        ratio = min(1.0, max(0.0, ratio))
        x, y = a.x() + ratio * dx, a.y() + ratio * dy
        return (point.x() - x) ** 2 + (point.y() - y) ** 2

    @staticmethod
    def _paint_ink(painter: QPainter, mapper: CoordinateMapper,
                   target: InkTarget, style: InkStyle, *, selected: bool = False) -> None:
        color = QColor.fromRgbF(style.color.red, style.color.green,
                                style.color.blue, style.opacity)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        widths_colors = ((style.width * mapper.zoom + 4.0, QColor(20, 125, 230, 150)),
                         (style.width * mapper.zoom, color)) if selected else (
                             (style.width * mapper.zoom, color),)
        for width, stroke in widths_colors:
            painter.setPen(QPen(stroke, width, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            for path in target.paths:
                painter.drawPolyline(QPolygonF([
                    QPointF(*mapper.canonical_to_viewport((point.x, point.y)))
                    for point in path
                ]))
        painter.restore()

    def _paint_callout(self, painter: QPainter, mapper: CoordinateMapper,
        target: CalloutTarget, rich_text: RichText | None, style: FreeTextStyle,
                       *, selected: bool = False) -> None:
        rect, anchor = target.text_rect, target.anchor
        points = list(callout_line(rect, anchor))
        painter.save()
        try:
            painter.setPen(QPen(QColor.fromRgbF(style.border.red, style.border.green,
                                                 style.border.blue, style.opacity),
                                max(1.0, style.border_width * mapper.zoom)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            display_points = [QPointF(*mapper.canonical_to_viewport((p.x, p.y))) for p in points]
            painter.drawPolyline(QPolygonF(display_points))
            self._paint_open_arrow(
                painter, display_points[0], self._open_arrow_toward(display_points[0], display_points[1:]))
            if selected:
                point = mapper.canonical_to_viewport((anchor.x, anchor.y))
                painter.setBrush(QColor(20, 125, 230))
                painter.drawEllipse(QPointF(*point), 4, 4)
        finally:
            painter.restore()
        if rich_text is None:
            # Gestures deliberately have no domain text yet. Paint only the
            # transient Qt geometry instead of manufacturing invalid RichText.
            self._paint_text_box_display(painter, self._display_rect(target.page_index, rect),
                                         None, style, mapper.zoom, selected=selected)
        else:
            self._paint_text_box(painter, mapper, rect, rich_text, style, selected=selected)

    def _paint_callout_preview(self, painter: QPainter, mapper: CoordinateMapper,
                               target: CalloutTarget, style: FreeTextStyle) -> None:
        self._paint_callout(painter, mapper, target, None, style, selected=True)

    @staticmethod
    def _open_arrow_toward(tip: QPointF, points: list[QPointF]) -> QPointF:
        """Skip a coincident Callout knee when choosing the OpenArrow direction."""
        return next((point for point in points
                     if (point.x() - tip.x()) ** 2 + (point.y() - tip.y()) ** 2 > 1e-6), tip)

    @staticmethod
    def _open_arrow_wings(tip: QPointF, toward: QPointF) -> tuple[QPointF, QPointF]:
        angle = atan2(toward.y() - tip.y(), toward.x() - tip.x())
        wing = 10.0
        return tuple(QPointF(tip.x() + wing * cos(angle + offset),
                             tip.y() + wing * sin(angle + offset))
                     for offset in (-.58, .58))

    @staticmethod
    def _paint_open_arrow(painter: QPainter, tip: QPointF, toward: QPointF) -> None:
        """Paint a screen-space open arrow at the Callout anchor endpoint."""
        for point in PdfView._open_arrow_wings(tip, toward):
            painter.drawLine(tip, point)

    @staticmethod
    def _open_arrow_paint_bounds(tip: QPointF, toward: QPointF, width: float) -> QRectF:
        """Full OpenArrow extent: two wings, pen radius and antialias fringe."""
        points = [tip, *PdfView._open_arrow_wings(tip, toward)]
        margin = max(0.5, width / 2.0) + 1.5
        xs, ys = [point.x() for point in points], [point.y() for point in points]
        return QRectF(min(xs) - margin, min(ys) - margin,
                      max(xs) - min(xs) + 2 * margin, max(ys) - min(ys) + 2 * margin)

    @staticmethod
    def _leader_paint_bounds(points: list[QPointF], arrow_bounds: QRectF, width: float) -> QRectF:
        """Paint envelope used for focused leader-overlay regression checks."""
        margin = max(0.5, width / 2.0) + 1.5
        line_bounds = QRectF(min(point.x() for point in points) - margin,
                            min(point.y() for point in points) - margin,
                            max(point.x() for point in points) - min(point.x() for point in points) + 2 * margin,
                            max(point.y() for point in points) - min(point.y() for point in points) + 2 * margin)
        return line_bounds.united(arrow_bounds)

    @staticmethod
    def _paint_text_box(painter: QPainter, mapper: CoordinateMapper, rect: PdfRect,
                        rich_text: RichText, style: FreeTextStyle, *, selected: bool = False) -> None:
        corners = tuple(QPointF(*mapper.canonical_to_viewport(point)) for point in
                        ((rect.x0, rect.y0), (rect.x1, rect.y0),
                         (rect.x0, rect.y1), (rect.x1, rect.y1)))
        display = QRectF(min(point.x() for point in corners), min(point.y() for point in corners),
                         max(point.x() for point in corners) - min(point.x() for point in corners),
                         max(point.y() for point in corners) - min(point.y() for point in corners))
        PdfView._paint_text_box_display(painter, display, rich_text, style, mapper.zoom,
                                        selected=selected)

    @staticmethod
    def _paint_text_box_preview(painter: QPainter, mapper: CoordinateMapper,
                                start: PdfPoint, end: PdfPoint, style: FreeTextStyle) -> None:
        """Paint only non-degenerate gesture geometry; it is never a PDF value object."""
        corners = tuple(QPointF(*mapper.canonical_to_viewport(point)) for point in
                        ((start.x, start.y), (end.x, start.y),
                         (start.x, end.y), (end.x, end.y)))
        display = QRectF(min(point.x() for point in corners), min(point.y() for point in corners),
                         max(point.x() for point in corners) - min(point.x() for point in corners),
                         max(point.y() for point in corners) - min(point.y() for point in corners))
        if display.width() <= 0 or display.height() <= 0:
            return
        PdfView._paint_text_box_display(painter, display, None, style, mapper.zoom, selected=True)

    @staticmethod
    def _paint_text_box_display(painter: QPainter, display: QRectF,
                                rich_text: RichText | None, style: FreeTextStyle, zoom: float,
                                *, selected: bool = False) -> None:
        painter.save()
        try:
            fill = QColor.fromRgbF(style.fill.red, style.fill.green, style.fill.blue, style.opacity) \
                if style.fill is not None else Qt.BrushStyle.NoBrush
            painter.setBrush(fill)
            border = QColor.fromRgbF(style.border.red, style.border.green, style.border.blue, style.opacity)
            painter.setPen(QPen(border, max(1.0, style.border_width * zoom)))
            painter.drawRect(display)
            if rich_text is not None:
                document = QTextDocument()
                PdfView._apply_rich_text_document(document, rich_text, zoom, style.opacity)
                document.setDocumentMargin(TEXT_BOX_CONTENT_PADDING)
                contents = display.adjusted(style.border_width * zoom, style.border_width * zoom,
                                            -style.border_width * zoom, -style.border_width * zoom)
                document.setTextWidth(max(0.0, contents.width()))
                painter.save()
                painter.setClipRect(contents)
                painter.translate(contents.topLeft())
                document.drawContents(painter, QRectF(0, 0, contents.width(), contents.height()))
                painter.restore()
            if selected:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(20, 125, 230, 180), 2))
                painter.drawRect(display.adjusted(-2, -2, 2, 2))
                painter.setBrush(QColor(20, 125, 230))
                for point in (display.topLeft(), display.topRight(),
                              display.bottomLeft(), display.bottomRight()):
                    painter.drawRect(QRectF(point.x() - 3, point.y() - 3, 6, 6))
        finally:
            painter.restore()

    @staticmethod
    def _paint_quads(painter: QPainter, mapper: CoordinateMapper,
                     quads: tuple[PdfQuad, ...], color: QColor, *, selected: bool = False) -> None:
        painter.save()
        painter.setPen(QPen(QColor(20, 125, 230, 210), 1.5) if selected else Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for quad in quads:
            perimeter = (quad.upper_left, quad.upper_right,
                         quad.lower_right, quad.lower_left)
            painter.drawPolygon(QPolygonF([
                QPointF(*mapper.canonical_to_viewport((point.x, point.y)))
                for point in perimeter
            ]))
        painter.restore()

    @staticmethod
    def _paint_caret(painter: QPainter, mapper: CoordinateMapper,
                     top: PdfPoint, bottom: PdfPoint, *, selected: bool = False) -> None:
        painter.save()
        color = QColor(20, 125, 230) if selected else QColor(126, 55, 161)
        painter.setPen(QPen(color, 2.0))
        painter.drawLine(QPointF(*mapper.canonical_to_viewport((top.x, top.y))),
                         QPointF(*mapper.canonical_to_viewport((bottom.x, bottom.y))))
        painter.restore()

    @classmethod
    def _paint_editorial(cls, painter: QPainter, mapper: CoordinateMapper,
                         kind: AnnotationKind,
                         target: TextRangeTarget | TextInsertionTarget,
                         *, selected: bool = False) -> None:
        if isinstance(target, TextRangeTarget) and kind in (AnnotationKind.DELETE_TEXT,
                                                              AnnotationKind.REPLACE_TEXT):
            painter.save()
            painter.setPen(QPen(QColor(20, 125, 230) if selected else QColor(222, 70, 70), 1.8))
            for quad in target.quads:
                left = ((quad.upper_left.x + quad.lower_left.x) / 2,
                        (quad.upper_left.y + quad.lower_left.y) / 2)
                right = ((quad.upper_right.x + quad.lower_right.x) / 2,
                         (quad.upper_right.y + quad.lower_right.y) / 2)
                painter.drawLine(QPointF(*mapper.canonical_to_viewport(left)),
                                 QPointF(*mapper.canonical_to_viewport(right)))
            painter.restore()
        if kind == AnnotationKind.INSERT_TEXT and isinstance(target, TextInsertionTarget):
            cls._paint_caret(painter, mapper, target.boundary_top, target.anchor, selected=selected)
        elif kind == AnnotationKind.REPLACE_TEXT and isinstance(target, TextRangeTarget):
            cls._paint_caret(painter, mapper, target.quads[-1].upper_right,
                             target.quads[-1].lower_right, selected=selected)


def _final_text_box_rect(start: PdfPoint, end: PdfPoint) -> PdfRect | None:
    x0, x1 = min(start.x, end.x), max(start.x, end.x)
    y0, y1 = min(start.y, end.y), max(start.y, end.y)
    if x1 - x0 < TEXT_BOX_MIN_SIZE or y1 - y0 < TEXT_BOX_MIN_SIZE:
        return None
    return PdfRect(x0, y0, x1, y1)


def _coalesce_runs(runs: list[RichTextRun]) -> tuple[RichTextRun, ...]:
    """Make editor extraction deterministic without losing a style boundary."""
    coalesced: list[RichTextRun] = []
    for run in runs:
        if (coalesced and coalesced[-1].font_family == run.font_family
                and coalesced[-1].font_size == run.font_size
                and coalesced[-1].color == run.color
                and coalesced[-1].bold == run.bold and coalesced[-1].italic == run.italic):
            previous = coalesced.pop()
            coalesced.append(RichTextRun(previous.text + run.text, previous.font_family,
                                         previous.font_size, previous.color,
                                         previous.bold, previous.italic))
        else:
            coalesced.append(run)
    return tuple(coalesced)
