"""Editor-local disjoint selections for character formatting, with one caret."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QMouseEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit


class FormattingTextEdit(QTextEdit):
    """Keep formatting ranges in Qt's UTF-16 positions, never Python indices."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._formatting_ranges: tuple[tuple[int, int], ...] = ()
        self._drag_base: tuple[tuple[int, int], ...] | None = None
        self._applying_format = False
        self.selectionChanged.connect(self._native_selection_changed)
        self.document().contentsChange.connect(self._document_changed)

    @property
    def formatting_ranges(self) -> tuple[tuple[int, int], ...]:
        return self._formatting_ranges

    def clear_formatting_ranges(self) -> None:
        """Drop temporary overlays without changing the native editing cursor."""
        self._drag_base = None
        self._formatting_ranges = ()
        self.setExtraSelections([])

    def _native_selection_changed(self) -> None:
        if self._applying_format:
            return
        cursor = self.textCursor()
        ranges = list(self._drag_base or ())
        if cursor.hasSelection():
            ranges.append((cursor.selectionStart(), cursor.selectionEnd()))
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        self._formatting_ranges = tuple(merged)
        self._show_formatting_ranges()

    def _show_formatting_ranges(self) -> None:
        selections = []
        for start, end in self._formatting_ranges:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(self.document())
            selection.cursor.setPosition(start)
            selection.cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.format.setBackground(self.palette().highlight())
            selection.format.setForeground(self.palette().highlightedText())
            selections.append(selection)
        self.setExtraSelections(selections)

    @staticmethod
    def _plain_drag_event(event: QMouseEvent) -> QMouseEvent:
        # Delegate selection, scrolling and bidi hit testing to QTextEdit.
        # Ctrl adds a range here; it must not invoke Qt's link/word gestures.
        modifiers = event.modifiers() & ~(Qt.KeyboardModifier.ControlModifier
                                          | Qt.KeyboardModifier.ShiftModifier)
        return QMouseEvent(event.type(), event.position(), event.globalPosition(),
                           event.button(), event.buttons(), modifiers, event.pointingDevice())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._drag_base = self._formatting_ranges
                # A press inside the native selection would otherwise start
                # Qt's text drag/drop instead of a new formatting selection.
                cursor = self.textCursor()
                cursor.clearSelection()
                self.setTextCursor(cursor)
            else:
                self.clear_formatting_ranges()
        super().mousePressEvent(self._plain_drag_event(event)
                               if self._drag_base is not None else event)
        self._native_selection_changed()

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(self._plain_drag_event(event)
                              if self._drag_base is not None else event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(self._plain_drag_event(event)
                                 if self._drag_base is not None else event)
        self._native_selection_changed()
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_base = None

    def mouseDoubleClickEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._drag_base = self._formatting_ranges
        else:
            self.clear_formatting_ranges()
        super().mouseDoubleClickEvent(self._plain_drag_event(event)
                                     if self._drag_base is not None else event)
        self._native_selection_changed()

    def keyPressEvent(self, event) -> None:
        # Modifiers alone must preserve the set while the user holds Ctrl to
        # add another range. Other keyboard actions use the one native cursor.
        if (event.key() not in (Qt.Key.Key_Control, Qt.Key.Key_Shift,
                               Qt.Key.Key_Alt, Qt.Key.Key_Meta)
                and not event.matches(QKeySequence.StandardKey.Copy)):
            self.clear_formatting_ranges()
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:
        self.clear_formatting_ranges()
        super().insertFromMimeData(source)

    def inputMethodEvent(self, event) -> None:
        self.clear_formatting_ranges()
        super().inputMethodEvent(event)

    def dropEvent(self, event) -> None:
        self.clear_formatting_ranges()
        super().dropEvent(event)

    def _document_changed(self, _position: int, removed: int, added: int) -> None:
        # Also covers native context-menu cut/undo and programmatic edits.
        # Own formatting changes preserve positions and keep the whole set.
        if not self._applying_format and (removed or added):
            self.clear_formatting_ranges()

    def apply_character_format(self, fmt: QTextCharFormat, cursor: QTextCursor) -> None:
        """Merge only requested properties; one toolbar action is one undo step."""
        ranges = self._formatting_ranges
        if not ranges and cursor.hasSelection():
            ranges = ((cursor.selectionStart(), cursor.selectionEnd()),)
        self._applying_format = True
        try:
            self.setTextCursor(cursor)
            if ranges:
                edit = QTextCursor(self.document())
                edit.beginEditBlock()
                try:
                    for start, end in ranges:
                        edit.setPosition(start)
                        edit.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                        edit.mergeCharFormat(fmt)
                finally:
                    edit.endEditBlock()
            else:
                self.mergeCurrentCharFormat(fmt)
        finally:
            self._applying_format = False
        self._formatting_ranges = ranges
        self._show_formatting_ranges()
