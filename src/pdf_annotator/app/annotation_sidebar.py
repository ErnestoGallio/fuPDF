"""Minimal right-hand annotation list backed by ``DocumentSession`` inventory."""

from html import escape
from math import ceil

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QTextLayout, QTextOption
from PySide6.QtWidgets import (QDockWidget, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QTextEdit,
                               QToolButton, QWidget, QVBoxLayout)

from .document_session import DocumentSession
from .ui_chrome import COLORS
from ..domain.results import HistoryDelta
from ..domain.annotation_inventory import AnnotationSummary, is_attached_reply


_LABELS = {
    "ink": "Penna", "line": "Linea", "highlight": "Evidenziazione", "text_box": "Riquadro testo",
    "callout": "Callout", "delete_text": "Elimina testo", "insert_text": "Inserisci testo",
    "replace_text": "Sostituisci testo", "sticky_note": "Nota", "underline": "Sottolineatura",
    "squiggly": "Sottolineatura ondulata", "strikeout": "Barrato",
}

_LINE_ENDS = ("Nessuna", "Quadrato", "Cerchio", "Rombo", "Freccia aperta",
              "Freccia chiusa", "Barra", "Freccia aperta inversa",
              "Freccia chiusa inversa", "Tratto obliquo")


def _line_properties(summary: AnnotationSummary) -> str:
    """Read-only properties in the existing detail card; no new edit controls."""
    fields = []
    if len(summary.geometry.line) == 2:
        a, b = summary.geometry.line
        fields.append(f"Estremi (pt): ({a.x:g}, {a.y:g}) → ({b.x:g}, {b.y:g})")
    stroke = summary.style.get("stroke", [])
    if stroke:
        space = {1: "grigio", 3: "RGB", 4: "CMYK"}.get(len(stroke), "nativo")
        fields.append(f"Colore ({space}): " + ", ".join(f"{value:g}" for value in stroke))
    width = summary.style.get("border", {}).get("width", -1)
    if width >= 0:
        fields.append(f"Spessore: {width:g} pt")
    ends = summary.style.get("line_ends", [])
    if len(ends) == 2:
        fields.append("Terminazioni: " + " → ".join(
            _LINE_ENDS[end] if type(end) is int and 0 <= end < len(_LINE_ENDS) else str(end)
            for end in ends))
    if summary.created:
        fields.append("Creato: " + summary.created)
    if summary.modified:
        fields.append("Modificato: " + summary.modified)
    return "\n".join(fields)


_REPLY_ISSUES = {
    "missing_parent": "riferimento mancante", "invalid_irt": "riferimento non valido",
    "cycle": "riferimento ciclico", "cross_page": "riferimento a un'altra pagina",
    "unsupported_reply_type": "relazione non supportata",
    "unreadable_parent": "annotazione principale non leggibile",
    "unresolved_parent": "annotazione principale non risolvibile",
}

_PREVIEW = Qt.ItemDataRole.UserRole + 1
_AUTHOR = Qt.ItemDataRole.UserRole + 2
_PREVIEW_LINES = 5
_PADDING = 8
_ROW_INSET = 1
_MAX_CARD_HEIGHT = 360


def _author_font(base: QFont) -> QFont:
    font = QFont(base)
    if font.pixelSize() > 0:
        font.setPixelSize(max(1, round(font.pixelSize() * .9)))
    else:
        font.setPointSizeF(font.pointSizeF() * .9)
    return font


def _wrapped(text, font, width):
    preview = text[:1024].replace("\n", "\u2028")
    layout = QTextLayout(preview, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    height = 0.0
    end = 0
    layout.beginLayout()
    for index in range(_PREVIEW_LINES):
        line = layout.createLine()
        if not line.isValid():
            break
        reserve = QFontMetrics(font).horizontalAdvance("…") + 4 if index == _PREVIEW_LINES - 1 else 0
        line.setLineWidth(max(1, width - reserve))
        line.setLeadingIncluded(True)
        line.setPosition(QPointF(0, height))
        height += line.height()
        end = line.textStart() + line.textLength()
    layout.endLayout()
    # QTextLine offsets are UTF-16 positions. Do not create an extra line to
    # check overflow: QTextLayout.draw would paint it at the first line's origin.
    more = len(text) > len(preview) or end < len(preview.encode("utf-16-le")) // 2
    return layout, ceil(height), more


class _AnnotationCards(QStyledItemDelegate):
    def sizeHint(self, option, index):
        explicit = index.data(Qt.ItemDataRole.SizeHintRole)
        if explicit is not None:
            return explicit
        width = self.parent().viewport().width() - 2 * _PADDING
        _, height, _ = _wrapped(index.data(_PREVIEW) or "", option.font, width)
        author_height = QFontMetrics(_author_font(option.font)).height() if index.data(_AUTHOR) else 0
        return QSize(0, 2 * (_PADDING + _ROW_INSET) + option.fontMetrics.height()
                     + author_height + (4 + height if height else 0))

    def paint(self, painter, option, index):
        if index.data(Qt.ItemDataRole.SizeHintRole) is not None:
            return  # The expanded row provides its own background and contents.
        painter.save()
        painter.setClipRect(option.rect)
        rect = option.rect.adjusted(0, _ROW_INSET, 0, -_ROW_INSET)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(COLORS.active))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(rect, QColor(COLORS.hover))
        body = rect.adjusted(_PADDING, _PADDING, -_PADDING, -_PADDING)
        font = option.font
        metrics = option.fontMetrics
        painter.setFont(font)
        painter.setPen(QColor(COLORS.primary))
        title = metrics.elidedText(index.data(), Qt.TextElideMode.ElideRight, max(0, body.width() - 18))
        painter.drawText(body.left(), body.top() + metrics.ascent(), title)
        x, y = body.right() - 7, body.top() + metrics.height() // 2
        painter.drawLine(x - 4, y - 2, x, y + 2)
        painter.drawLine(x, y + 2, x + 4, y - 2)
        text = index.data(_PREVIEW) or ""
        layout, height, more = _wrapped(text, font, body.width())
        top = body.top() + metrics.height()
        author = index.data(_AUTHOR)
        if author:
            author_font = _author_font(font)
            author_metrics = QFontMetrics(author_font)
            painter.setFont(author_font)
            painter.setPen(QColor(COLORS.secondary))
            painter.drawText(body.left(), top + author_metrics.ascent(),
                             author_metrics.elidedText(author, Qt.TextElideMode.ElideRight, body.width()))
            top += author_metrics.height()
            painter.setPen(QColor(COLORS.primary))
            painter.setFont(font)
        top += 4
        painter.save()
        painter.setClipRect(QRectF(body.left(), top, body.width(), height), Qt.ClipOperation.IntersectClip)
        layout.draw(painter, QPointF(body.left(), top))
        painter.restore()
        if more:
            painter.drawText(body.right() - metrics.horizontalAdvance("…"), top + height - metrics.descent(), "…")
        painter.restore()


class _ExpandedCard(QWidget):
    def paintEvent(self, event) -> None:
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS.active))
            # Leave the row join to the viewport, as in the collapsed delegate.
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0, _ROW_INSET, 0, -_ROW_INSET), 6, 6)


class _AnnotationList(QListWidget):
    resized = Signal()
    expand_current = Signal()

    def paintEvent(self, event) -> None:
        color = QColor(COLORS.chrome)
        color.setAlpha(238)
        with QPainter(self.viewport()) as painter:
            painter.fillRect(event.rect(), color)
        super().paintEvent(event)
        first = self.indexAt(QPoint(self.viewport().width() // 2, event.rect().top()))
        if not first.isValid():
            return
        with QPainter(self.viewport()) as painter:
            transform = painter.deviceTransform()
            scale, offset = transform.m22(), transform.dy()
            pixel = 1 / scale
            color = QColor(COLORS.primary)
            color.setAlpha(26)
            for row in range(first.row(), self.count() - 1):
                rect = self.visualItemRect(self.item(row))
                if rect.top() > event.rect().bottom():
                    break
                edge = ceil((rect.top() + rect.height()) * scale + offset) - 1
                painter.fillRect(QRectF(rect.left() + _PADDING, (edge - offset) * pixel,
                                        rect.width() - 2 * _PADDING, pixel), color)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self.currentItem():
            self.expand_current.emit()
            return
        super().keyPressEvent(event)


class AnnotationSidebar(QDockWidget):
    annotation_activated = Signal(str)
    detail_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__("Annotazioni", parent)
        self.setObjectName("annotation_sidebar")
        self.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.list = _AnnotationList(self)
        self.list.setObjectName("annotation_list")
        self.list.viewport().setAutoFillBackground(False)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setItemDelegate(_AnnotationCards(self.list))
        self.detail = QTextEdit(self)
        self.detail.setObjectName("annotation_detail")
        self.detail.setReadOnly(True)
        self.detail.setAcceptRichText(False)
        self.detail.setFrameShape(QFrame.Shape.NoFrame)
        self.detail.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.detail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.detail.hide()
        self.setWidget(self.list)
        self._document_id: str | None = None
        self._expanded: QListWidgetItem | None = None
        self._updating = False
        self._card_resize = QTimer(self)
        self._card_resize.setSingleShot(True)
        self._card_resize.timeout.connect(self._resize_card)
        self.detail.document().documentLayout().documentSizeChanged.connect(lambda _: self._card_resize.start())
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.itemClicked.connect(self._toggle_card)
        self.list.expand_current.connect(lambda: self._toggle_card(self.list.currentItem()))
        self.list.resized.connect(self._resize_card)

    def set_session(self, session: DocumentSession | None) -> None:
        same_document = session is not None and self._document_id == session.document_id
        expanded_id = self._expanded.data(Qt.ItemDataRole.UserRole) if same_document and self._expanded else None
        scrollbar = self.list.verticalScrollBar()
        scroll = scrollbar.value() if same_document else 0
        self._collapse_card()
        self._document_id = session.document_id if session is not None else None
        self._updating = True
        self.list.clear()
        if not same_document or session.selected_annotation_id is None:
            self.detail.clear()
        if session is not None:
            summaries = sorted(session.annotation_inventory.values(),
                               key=lambda item: (item.page_index, item.geometry.bbox.y0,
                                                 item.geometry.bbox.x0, item.logical_id))
            for summary in summaries:
                if is_attached_reply(summary, session.annotation_inventory):
                    continue
                self.list.addItem(self._make_item(summary))
            if session.selected_annotation_id is not None:
                self.select_annotation(session, session.selected_annotation_id)
                if session.selected_annotation_id == expanded_id and self.list.currentItem() is not None:
                    self._toggle_card(self.list.currentItem())
        self._updating = False
        if same_document:
            scrollbar.setValue(scroll)

    def select_annotation(self, session: DocumentSession, logical_id: str) -> None:
        summary = session.annotation_inventory.get(logical_id)
        if summary is not None and is_attached_reply(summary, session.annotation_inventory):
            self.annotation_activated.emit(summary.reply.parent_logical_id)
            return
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == logical_id:
                if self._expanded is not None and self._expanded is not item:
                    self._collapse_card()
                updating = self._updating
                self._updating = True
                self.list.setCurrentItem(item)
                if not self.list.visualItemRect(item).intersects(self.list.viewport().rect()):
                    self.list.scrollToItem(item)
                self._updating = updating
                self._render_detail(session, logical_id)
                return

    def _toggle_card(self, item: QListWidgetItem) -> None:
        if item is None or item is not self.list.currentItem():
            return
        top = self.list.visualItemRect(item).top()
        expand = self._expanded is not item
        self._collapse_card()
        if expand:
            card = _ExpandedCard(self.list)
            card.setObjectName("annotation_card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(_PADDING, 4, _PADDING, _PADDING)
            layout.setSpacing(4)
            header = QHBoxLayout()
            title = QPushButton(item.text(), card)
            title.setFlat(True)
            title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            title.setToolTip("Vai all’annotazione")
            title.clicked.connect(lambda: self.annotation_activated.emit(item.data(Qt.ItemDataRole.UserRole)))
            header.addWidget(title, 1)
            collapse = QToolButton(card)
            collapse.setAutoRaise(True)
            collapse.setArrowType(Qt.ArrowType.UpArrow)
            collapse.setAccessibleName("Comprimi annotazione")
            collapse.setToolTip("Comprimi annotazione")
            collapse.clicked.connect(lambda: self._toggle_card(item))
            header.addWidget(collapse)
            layout.addLayout(header)
            if author := item.data(_AUTHOR):
                label = QLabel(card)
                label.setObjectName("annotation_author")
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setFont(_author_font(self.list.font()))
                label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                label.setText(author)
                label.setToolTip(f"<qt>{escape(author)}</qt>")
                layout.addWidget(label)
            layout.addWidget(self.detail, 1)
            self._expanded = item
            self.list.setItemWidget(item, card)
            self.detail.show()
            self._resize_card()
        else:
            self.list.setFocus()
        self.list.doItemsLayout()
        scroll = self.list.verticalScrollBar()
        scroll.setValue(scroll.value() + self.list.visualItemRect(item).top() - top)

    def _collapse_card(self) -> None:
        if self._expanded is not None:
            item, self._expanded = self._expanded, None
            # The list owns the card. Keep the shared detail editor on removal.
            self.detail.hide()
            self.detail.setParent(self)
            self.list.removeItemWidget(item)
            item.setData(Qt.ItemDataRole.SizeHintRole, None)

    def _resize_card(self) -> None:
        if self._expanded is None:
            return
        layout = self.list.itemWidget(self._expanded).layout()
        margins = layout.contentsMargins()
        header_items = layout.count() - 1
        chrome = margins.top() + margins.bottom() + header_items * layout.spacing()
        chrome += sum(layout.itemAt(index).sizeHint().height() for index in range(header_items))
        size = QSize(0, min(_MAX_CARD_HEIGHT, chrome + ceil(self.detail.document().size().height())))
        if self._expanded.sizeHint() != size:
            self._expanded.setSizeHint(size)

    def _render_detail(self, session: DocumentSession, logical_id: str) -> None:
        summary = session.annotation_inventory.get(logical_id)
        if summary is None:
            self.detail.clear()
            return
        pending: list[str] = []

        def contents(target_id: str) -> str:
            detail = session.annotation_details.get(target_id)
            if detail is not None:
                return detail.contents
            if target_id in session.annotation_detail_errors:
                return "Impossibile caricare il contenuto dell'annotazione."
            if target_id in session.annotation_inventory:
                pending.append(target_id)
                return "Caricamento contenuto…"
            return "Caricamento contenuto…" if not session.inventory_complete else "Risposta non disponibile."

        text = contents(logical_id)
        if summary.reply_ids:
            label = ("Contenuto editoriale" if summary.kind in ("delete_text", "insert_text", "replace_text")
                     else "Contenuto annotazione")
            text = label + "\n" + text + "\n\nMotivazioni / risposte"
            for number, reply_id in enumerate(summary.reply_ids, 1):
                reply = session.annotation_inventory.get(reply_id)
                author = reply.author if reply is not None and reply.author else "Autore non indicato"
                text += f"\n\n{number}. {author}\n{contents(reply_id)}"
        elif summary.reply is not None and summary.reply.status != "linked":
            text += "\n\nRisposta non associata: " + _REPLY_ISSUES.get(summary.reply.status, "riferimento non risolvibile")
        if summary.kind == "line":
            text += "\n\n" + _line_properties(summary)
            detail = session.annotation_details.get(logical_id)
            if detail is not None and detail.subject:
                text += "\nOggetto: " + detail.subject
        # Avoid resetting the selection/scroll position when an unrelated read completes.
        if self.detail.toPlainText() != text:
            self.detail.setPlainText(text)
            self._resize_card()
        current = self.list.currentItem()
        if current is not None:
            preview = text[:1024] + ("…" if len(text) > 1024 else "")
            if logical_id in pending and summary.preview:
                preview = summary.preview
            current.setData(_PREVIEW, preview)
        if pending:
            self.detail_requested.emit(pending[0])

    def apply_history_delta(self, session: DocumentSession, delta: HistoryDelta) -> None:
        """Replace only rows touched by Undo/Redo, preserving the rest in place."""
        if self._document_id != session.document_id:
            self.set_session(session)
            return
        changed = set(delta.changed_ids)
        if not changed:
            return
        removed = set(delta.inventory_removed_ids) | set(delta.removed_ids)
        if any(summary.reply is not None and summary.reply.parent_logical_id in removed
               for summary in session.annotation_inventory.values()):
            self.set_session(session)
            return
        selected = self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else None
        reopen = self._expanded is not None and selected in changed
        if reopen:
            self._collapse_card()
        self._updating = True
        for index in range(self.list.count() - 1, -1, -1):
            if self.list.item(index).data(Qt.ItemDataRole.UserRole) in changed:
                self.list.takeItem(index)
        for logical_id in changed:
            summary = session.annotation_inventory.get(logical_id)
            if summary is not None and not is_attached_reply(summary, session.annotation_inventory):
                self._insert_summary(session, summary)
        self._updating = False
        if isinstance(selected, str) and selected in session.annotation_inventory:
            self.select_annotation(session, selected)
            if reopen and self.list.currentItem() is not None:
                self._toggle_card(self.list.currentItem())
        elif selected in changed:
            self.detail.clear()

    def _insert_summary(self, session: DocumentSession, summary) -> None:
        order = (summary.page_index, summary.geometry.bbox.y0,
                 summary.geometry.bbox.x0, summary.logical_id)
        index = next((index for index in range(self.list.count())
                      if self._summary_order(self._summary_for_item(session, index)) > order),
                     self.list.count())
        self.list.insertItem(index, self._make_item(summary))

    @staticmethod
    def _make_item(summary) -> QListWidgetItem:
        label = _LABELS.get(summary.kind, summary.subtype or "Annotazione")
        item = QListWidgetItem(f"{label} · p. {summary.page_index + 1}")
        item.setData(Qt.ItemDataRole.UserRole, summary.logical_id)
        item.setData(_PREVIEW, summary.preview)
        if summary.author:
            item.setData(_AUTHOR, " ".join(summary.author.splitlines()).strip() or None)
        item.setToolTip("Espandi per leggere il contenuto completo")
        return item

    @staticmethod
    def _summary_order(summary) -> tuple:
        return (summary.page_index, summary.geometry.bbox.y0,
                summary.geometry.bbox.x0, summary.logical_id)

    def _summary_for_item(self, session: DocumentSession, index: int):
        # The session remains the source of truth; only existing item IDs are used.
        logical_id = self.list.item(index).data(Qt.ItemDataRole.UserRole)
        return session.annotation_inventory[logical_id]

    def update_detail(self, session: DocumentSession, logical_id: str) -> None:
        current = self.list.currentItem()
        selected = session.selected_annotation_id
        if (self._document_id == session.document_id and selected is not None and current is not None
                and current.data(Qt.ItemDataRole.UserRole) == selected):
            summary = session.annotation_inventory.get(selected)
            if summary is not None and (selected == logical_id or logical_id in summary.reply_ids):
                self._render_detail(session, selected)

    def show_detail_error(self, session: DocumentSession, logical_id: str) -> None:
        self.update_detail(session, logical_id)

    def _current_changed(self, current, _previous) -> None:
        if self._updating or current is None:
            return
        logical_id = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(logical_id, str):
            self._collapse_card()
            self.annotation_activated.emit(logical_id)
            self.detail_requested.emit(logical_id)
