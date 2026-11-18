"""UI-only Sticky Note icon and reader, backed by the shared native inventory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QToolButton, QVBoxLayout, QWidget)

from ..app.document_session import Lifecycle
from ..app.ui_chrome import COLORS
from ..domain.annotation_inventory import AnnotationSummary, is_attached_reply
from ..domain.geometry import PdfRect
from .coordinate_mapper import CoordinateMapper

if TYPE_CHECKING:
    from .pdf_view import PdfView


def note_icon_rect(mapper: CoordinateMapper, bbox: PdfRect) -> QRectF:
    """One viewport-logical hit/paint box; PDF geometry and DPR stay untouched."""
    x, y = mapper.canonical_to_viewport(((bbox.x0 + bbox.x1) / 2,
                                        (bbox.y0 + bbox.y1) / 2))
    left, top = mapper.canonical_to_viewport(mapper.display_to_canonical((0, 0)))
    width, height = mapper.geometry.displayed_size
    # At low zoom a fixed-size icon can protrude beyond its page, where
    # mouse presses cannot resolve to that page. Keep its hit box on-page.
    width, height = width * mapper.zoom, height * mapper.zoom
    radius = 11
    margin_x, margin_y = min(radius, width / 2), min(radius, height / 2)
    x = min(max(x, left + margin_x), left + width - margin_x)
    y = min(max(y, top + margin_y), top + height - margin_y)
    return QRectF(x - radius, y - radius, radius * 2, radius * 2)


def paint_note_icon(painter: QPainter, rect: QRectF, *, selected: bool) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(COLORS.accent if selected else COLORS.border), 1.2))
    painter.setBrush(QColor(COLORS.active if selected else COLORS.surface))
    painter.drawRoundedRect(rect, 4, 4)
    x, y = rect.left(), rect.top()
    paper = QPainterPath(QPointF(x + 6, y + 4))
    for point in ((x + 13, y + 4), (x + 17, y + 8), (x + 17, y + 18),
                  (x + 6, y + 18)):
        paper.lineTo(QPointF(*point))
    paper.closeSubpath()
    painter.setPen(QPen(QColor(COLORS.primary), 1.1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(paper)
    painter.drawLine(QPointF(x + 13, y + 4), QPointF(x + 13, y + 8))
    painter.drawLine(QPointF(x + 13, y + 8), QPointF(x + 17, y + 8))
    for row in (11, 14):
        painter.drawLine(QPointF(x + 9, y + row), QPointF(x + 14, y + row))
    painter.restore()


class StickyNoteReader(QFrame):
    """A single transient reader per viewer; never owns PDF or mutation state."""

    detail_requested = Signal(str)

    def __init__(self, view: PdfView) -> None:
        super().__init__(view.viewport())
        self.view = view
        self.logical_id: str | None = None
        self._generation = 0
        self._loaded = False
        self._failed = False
        self.setObjectName("stickyNoteReader")
        self.setStyleSheet(f"""
            QFrame#stickyNoteReader {{ background: {COLORS.chrome};
                border: 1px solid {COLORS.border}; border-radius: 8px; }}
            QLabel {{ color: {COLORS.primary}; border: 0; }}
            QPlainTextEdit {{ background: {COLORS.surface}; color: {COLORS.primary};
                border: 0; border-radius: 4px; padding: 6px; }}
            QToolButton {{ color: {COLORS.secondary}; background: transparent;
                border: 0; border-radius: 4px; }}
            QToolButton:hover {{ background: {COLORS.hover}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        header = QHBoxLayout()
        title = QLabel("Nota", self)
        header.addWidget(title)
        header.addStretch()
        self.close_button = QToolButton(self)
        self.close_button.setText("×")
        self.close_button.setAccessibleName("Chiudi nota")
        self.close_button.setFixedSize(24, 24)
        self.close_button.clicked.connect(self.dismiss)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        self.author = QLabel(self)
        self.author.setTextFormat(Qt.TextFormat.PlainText)
        self.author.setWordWrap(True)
        self.author.setMaximumHeight(42)
        layout.addWidget(self.author)
        self.text = QPlainTextEdit(self)
        self.text.setAccessibleName("Contenuto della nota")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text.setPlaceholderText("Nota senza contenuto.")
        layout.addWidget(self.text)
        self.hide()

    def open_note(self, summary: AnnotationSummary) -> None:
        if is_attached_reply(summary, self.view.session.annotation_inventory):
            self.dismiss()
            return
        self.logical_id = summary.logical_id
        self._generation = self.view.session.document_generation
        self._loaded = self._failed = False
        self.author.setText(summary.author or "")
        self.author.setVisible(bool(summary.author))
        self.text.setPlainText("Caricamento contenuto…")
        viewport = self.view.viewport()
        icon = note_icon_rect(self.view.coordinate_mapper(summary.page_index), summary.geometry.bbox)
        width, height = min(360, max(1, viewport.width() - 16)), min(280, max(1, viewport.height() - 16))
        x = icon.right() + 8
        if x + width > viewport.width() - 8:
            x = icon.left() - width - 8
        self.setGeometry(round(max(0, min(x, viewport.width() - width))),
                         round(max(0, min(icon.top(), viewport.height() - height))), width, height)
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self)
        # Copy/navigation stay local to this read-only editor, including Delete.
        self.text.setFocus(Qt.FocusReason.OtherFocusReason)
        self.tick()

    def dismiss(self) -> None:
        focused = self.isAncestorOf(QApplication.focusWidget()) if QApplication.focusWidget() else False
        self.hide()
        if focused:
            self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    def hideEvent(self, event) -> None:
        self.logical_id = None
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def tick(self) -> None:
        logical_id = self.logical_id
        if logical_id is None:
            return
        session = self.view.session
        if (not self.view._active or session.lifecycle != Lifecycle.READY or session.saving
                or session.document_generation != self._generation
                or logical_id not in session.annotation_inventory
                or is_attached_reply(session.annotation_inventory[logical_id], session.annotation_inventory)):
            self.dismiss()
            return
        if self._loaded or self._failed:
            return
        detail = session.annotation_details.get(logical_id)
        if detail is not None:
            self.text.setPlainText(detail.contents)
            self._loaded = True
        elif session.inventory_request_id is None:
            # One existing inventory/detail channel is shared with the sidebar.
            # Retry only the current open target once that channel is available.
            self.detail_requested.emit(logical_id)

    def detail_failed(self, logical_id: str) -> None:
        if (self.logical_id == logical_id
                and self._generation == self.view.session.document_generation):
            self._failed = True
            self.text.setPlainText("Impossibile caricare il contenuto della nota.")

    def eventFilter(self, watched, event) -> bool:
        if self.logical_id is None:
            return False
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return True
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and isinstance(watched, QWidget) and self.isAncestorOf(watched)):
            # An ignored read-only editing key must not bubble into PDF Delete.
            return True
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
            if watched is not self and not self.isAncestorOf(watched):
                self.dismiss()
                # The outside click still reaches the original tool/tab/control.
        return False
