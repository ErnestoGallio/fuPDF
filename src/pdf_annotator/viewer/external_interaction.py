"""Capability-driven native annotation gestures; no PDF API or encoding here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF

from ..domain.annotation_commands import CommandContext
from ..domain.annotation_inventory import AnnotationSummary, external_command_annotation_id
from ..domain.callout_geometry import callout_line, move_callout_box
from ..domain.external_edits import EditExternalAnnotation, ExternalEditKind
from ..domain.geometry import PdfPoint, PdfRect
from ..domain.results import MutationResult
from ..domain.rich_text import RichText
from ..domain.styles import FreeTextStyle, RgbColor
from ..domain.targets import RectTarget
from .coordinate_mapper import CoordinateMapper
from .raster_cache import RenderKey

if TYPE_CHECKING:
    from .pdf_view import PdfView


@dataclass
class NativeGesture:
    summary: AnnotationSummary
    start: PdfPoint
    action: str
    box: PdfRect
    anchor: PdfPoint | None
    command: EditExternalAnnotation | None = None
    appearance_key: RenderKey | None = None


class ExternalInteraction:
    def __init__(self, view: PdfView) -> None:
        self.view = view
        self.gesture: NativeGesture | None = None
        self.pending: EditExternalAnnotation | None = None
        self.pending_preview: NativeGesture | None = None
        # Gesture state is only for the in-progress pointer preview.  Once a
        # native edit commits, its previous AP is no longer valid to paint.
        self.transition: NativeGesture | None = None
        self.pending_editor: tuple[RectTarget, str, FreeTextStyle, str, RichText] | None = None
        self.waiting_editor: tuple[str, int] | None = None

    def cancel_gesture(self) -> None:
        self.gesture = None
        self.waiting_editor = None
        self.view.viewport().update()

    def command(self, summary: AnnotationSummary, edit: ExternalEditKind,
                **kwargs: Any) -> EditExternalAnnotation:
        session = self.view.session
        return EditExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
            external_command_annotation_id(summary.logical_id), summary.logical_id, summary.page_index, edit, **kwargs)

    def _anchor_hit(self, summary: AnnotationSummary, point: QPointF) -> bool:
        if not summary.capabilities.movable_anchor or not summary.geometry.callout_line:
            return False
        anchor = summary.geometry.callout_line[0]
        mapped = self.view.coordinate_mapper(summary.page_index).canonical_to_viewport((anchor.x, anchor.y))
        return (mapped[0]-point.x())**2 + (mapped[1]-point.y())**2 <= 64

    def action_at(self, summary: AnnotationSummary, point: QPointF) -> str | None:
        box = summary.geometry.text_rect or summary.geometry.bbox
        if self._anchor_hit(summary, point):
            return "anchor"
        if summary.capabilities.resizable:
            corner = self.view._resize_corner(point, summary.page_index, box)
            if corner:
                return corner
        return "move" if summary.capabilities.movable else None

    def press(self, summary: AnnotationSummary, point: QPointF) -> None:
        if self.pending is not None:
            return
        action = self.action_at(summary, point)
        if action is None:
            return
        box = summary.geometry.text_rect or summary.geometry.bbox
        self.gesture = NativeGesture(summary, self.view._canonical_clamped(summary.page_index, point),
            action, box, summary.geometry.callout_line[0] if summary.geometry.callout_line else None)
        self.gesture.appearance_key = self.view._appearance_key(summary)

    def move(self, point: QPointF) -> bool:
        gesture = self.gesture
        if gesture is None:
            return False
        summary = gesture.summary
        current = self.view._canonical_clamped(summary.page_index, point)
        box = summary.geometry.text_rect or summary.geometry.bbox
        dx, dy = current.x-gesture.start.x, current.y-gesture.start.y
        if abs(dx)+abs(dy) < 1:
            gesture.command = None
            return True
        try:
            if gesture.action == "move":
                gesture.box = PdfRect(box.x0+dx, box.y0+dy, box.x1+dx, box.y1+dy)
                gesture.command = self.command(summary, ExternalEditKind.MOVE, delta=(dx, dy))
            elif gesture.action == "anchor":
                gesture.anchor = current
                gesture.command = self.command(summary, ExternalEditKind.ANCHOR, anchor=current)
            else:
                gesture.box = PdfRect(min(box.x1-12, current.x) if "l" in gesture.action else box.x0,
                    min(box.y1-12, current.y) if "t" in gesture.action else box.y0,
                    max(box.x0+12, current.x) if "r" in gesture.action else box.x1,
                    max(box.y0+12, current.y) if "b" in gesture.action else box.y1)
                gesture.command = self.command(summary, ExternalEditKind.RESIZE, rect=gesture.box)
        except ValueError:
            gesture.command = None
        self.view.viewport().update()
        return True

    def release(self, point: QPointF) -> bool:
        if self.gesture is None:
            return False
        self.move(point)
        gesture, self.gesture = self.gesture, None
        if gesture.command is not None:
            self.pending, self.pending_preview = gesture.command, gesture
            self.view.text_box_requested.emit(gesture.command)
        self.view.viewport().update()
        return True

    def request_editor(self, summary: AnnotationSummary) -> bool:
        if not summary.capabilities.editable_text or self.pending is not None:
            return False
        self.gesture = None
        self.waiting_editor = (summary.logical_id, self.view.session.document_generation)
        self.view._set_selected_inventory(summary.logical_id)
        self.detail_ready(summary.logical_id)
        if self.waiting_editor:
            self.view.inventory_annotation_selected.emit(summary.logical_id)
        return True

    def detail_ready(self, logical_id: str) -> None:
        if self.waiting_editor != (logical_id, self.view.session.document_generation):
            return
        summary = self.view.session.annotation_inventory.get(logical_id)
        detail = self.view.session.annotation_details.get(logical_id)
        if (summary is None or detail is None or detail.edit_rich_text is None
                or detail.edit_style is None or not summary.capabilities.editable_text):
            return
        self.waiting_editor = None
        # The existing text editor needs only the box. Native leader geometry
        # remains in the inventory and is never replaced by an app CalloutTarget.
        self.view._begin_text_box_editor(RectTarget(summary.page_index, summary.geometry.text_rect),
            logical_id, detail.edit_style, detail.contents, detail.edit_rich_text)

    def confirm_editor(self, logical_id: str, rich_text: RichText, style: FreeTextStyle) -> bool:
        summary = self.view.session.annotation_inventory.get(logical_id)
        detail = self.view.session.annotation_details.get(logical_id)
        if summary is None or detail is None or self.pending is not None:
            return False
        target = RectTarget(summary.page_index, summary.geometry.text_rect)
        # Imported FreeText/Callout may already overflow their native Rect.
        # Text fitting is never a precondition for an external edit: the
        # operation changes text only and must retain the complete content.
        changed_style = style if style != detail.edit_style else None
        if changed_style is not None and not summary.capabilities.editable_style:
            return False
        if rich_text == detail.edit_rich_text and changed_style is None:
            self.view.cancel_text_box_edit()
            return True
        command = self.command(summary, ExternalEditKind.TEXT, rich_text=rich_text, style=changed_style)
        self.pending = command
        self.pending_editor = (target, logical_id, style, rich_text.plain, rich_text)
        self.view.cancel_text_box_edit()
        self.view.text_box_requested.emit(command)
        return self.pending is not None

    def result(self, result: MutationResult) -> bool:
        if (self.pending is None or self.pending.context.command_id != result.command_id
                or result.document_id != self.view.session.document_id):
            return False
        editor = self.pending_editor
        self.pending = self.pending_preview = self.pending_editor = None
        self.transition = None
        if not result.success and editor is not None and self.view._text_editor is None:
            self.view._begin_text_box_editor(*editor, restoring=True)
        self.view.viewport().update()
        return True

    def history_delta(self, delta) -> None:
        """A worker-authoritative restore must never replay a live gesture."""
        affected = set(delta.changed_ids) | set(delta.appearance_invalidated_ids)
        if self.transition is not None and self.transition.summary.logical_id in affected:
            self.transition = None

    def hover_cursor(self, point: QPointF) -> bool:
        summary = self.view.session.annotation_inventory.get(self.view.selected_inventory_id)
        if summary is None or not self.view._display_rect(summary.page_index,
                summary.geometry.bbox).adjusted(-8, -8, 8, 8).contains(point):
            return False
        if (summary.kind == "line" and len(summary.geometry.line) == 2
                and not self.view._hit_test_native_line(summary, point)):
            return False
        action = self.action_at(summary, point)
        if action is None:
            return False
        cursor = (Qt.CursorShape.SizeFDiagCursor if action in ("tl", "br") else
                  Qt.CursorShape.SizeBDiagCursor if action in ("tr", "bl") else
                  Qt.CursorShape.SizeAllCursor)
        self.view.viewport().setCursor(cursor)
        return True

    def paint(self, painter: QPainter, mapper: CoordinateMapper,
              summary: AnnotationSummary, selected: bool) -> None:
        if self.view._editing_text_box_id == summary.logical_id:
            # The QTextEdit is the sole text renderer during an external edit.
            # Keeping the cached native AP underneath a transparent/metrics-
            # different overlay produces two superimposed texts.  This is UI
            # state only: no PDF object or AP cache is changed.  A Callout
            # still needs its native /CL leader while its box is occupied by
            # the editor.
            if summary.geometry.callout_line:
                color = self.view._inventory_color(summary.style.get("stroke"), RgbColor(.15, .15, .15))
                self.view._paint_external_callout_leader(
                    painter, mapper, summary.geometry.callout_line,
                    QColor.fromRgbF(color.red, color.green, color.blue), max(1.0, mapper.zoom))
            return
        if self.pending_editor is not None and self.pending_editor[1] == summary.logical_id:
            # The command has already committed to the normalized rich text.
            # Do not flash the old imported AP back while the worker creates
            # the replacement appearance: that made the old text persist
            # visually after Ctrl+Enter.  This is a transient preview of the
            # exact native state being requested, never a PDF mutation.
            target, _logical_id, style, _plain, rich = self.pending_editor
            if summary.geometry.callout_line:
                self.view._paint_external_callout(
                    painter, mapper, target.rect, summary.geometry.callout_line,
                    rich, style, selected=selected)
            else:
                self.view._paint_text_box(painter, mapper, target.rect, rich, style, selected=selected)
            return
        if (self.transition is not None and self.transition.summary.logical_id == summary.logical_id
                and self.view._has_current_native_appearance(summary)):
            self.transition = None
        gesture = self.gesture or self.pending_preview or self.transition
        if gesture is None or gesture.summary.logical_id != summary.logical_id or gesture.command is None:
            self.view._paint_inventory_annotation(painter, mapper, summary, selected)
        elif gesture.action == "move":
            source = gesture.summary
            dx, dy = gesture.command.delta
            if not source.geometry.callout_line:
                # FreeText without a leader and native Line both translate
                # their whole authored appearance rigidly.
                origin = mapper.canonical_to_viewport((0, 0))
                shifted = mapper.canonical_to_viewport((dx, dy))
                painter.save()
                painter.translate(shifted[0]-origin[0], shifted[1]-origin[1])
                self.view._paint_inventory_annotation(
                    painter, mapper, source, selected, retained_appearance_key=gesture.appearance_key)
                painter.restore()
            else:
                # A Callout box move leaves /CL's arrow endpoint fixed. Draw
                # only its box/text AP portion at the translated box, then
                # overlay the same deterministic leader the worker will use.
                text_rect = source.geometry.text_rect or source.geometry.bbox
                origin = mapper.canonical_to_viewport((0, 0))
                shifted = mapper.canonical_to_viewport((dx, dy))
                painter.save()
                painter.translate(shifted[0]-origin[0], shifted[1]-origin[1])
                self.view._paint_native_appearance(
                    painter, mapper, source, clip_to=text_rect, retained_key=gesture.appearance_key)
                painter.restore()
                _box, line = move_callout_box(text_rect, source.geometry.callout_line, dx, dy)
                color = self.view._inventory_color(source.style.get("stroke"), RgbColor(.15, .15, .15))
                self.view._paint_external_callout_leader(
                    painter, mapper, line, QColor.fromRgbF(color.red, color.green, color.blue),
                    max(1.0, mapper.zoom))
        elif gesture.action == "anchor":
            # A Callout AP can contain both box and leader. Keep the AP's box
            # pixels in their original content rectangle, then paint the one
            # authoritative /CL overlay at the proposed anchor. In particular
            # do not scale the old combined raster to a new leader envelope.
            source = gesture.summary
            text_rect = source.geometry.text_rect or source.geometry.bbox
            self.view._paint_native_appearance(
                painter, mapper, source, clip_to=text_rect, retained_key=gesture.appearance_key)
            line = source.geometry.callout_line
            if len(line) == 3 and gesture.anchor is not None:
                line = callout_line(text_rect, gesture.anchor)
            color = self.view._inventory_color(source.style.get("stroke"), RgbColor(.15, .15, .15))
            self.view._paint_external_callout_leader(
                painter, mapper, line, QColor.fromRgbF(color.red, color.green, color.blue),
                max(1.0, mapper.zoom))
            painter.save()
            painter.setPen(QPen(QColor(45, 120, 230), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.view._display_rect(source.page_index, text_rect))
            painter.restore()
        else:
            # Do not synthesize a FreeText appearance from summary fields while
            # dragging.  In particular /RC can carry a text colour and rich
            # formatting that the bounded summary cannot reproduce.  Keep the
            # verified native appearance unchanged and overlay only proposed
            # geometry; the worker remains the sole source of the final AP.
            self.view._paint_inventory_annotation(painter, mapper, summary, False)
            geometry = summary.geometry
            line = geometry.callout_line
            box = geometry.text_rect or geometry.bbox
            if line:
                if gesture.action == "anchor":
                    line = (gesture.anchor, *line[1:])
                else:
                    attach = line[-1]
                    new_x = gesture.box.x0 + (attach.x-box.x0)*(gesture.box.x1-gesture.box.x0)/(box.x1-box.x0)
                    new_y = gesture.box.y0 + (attach.y-box.y0)*(gesture.box.y1-gesture.box.y0)/(box.y1-box.y0)
                    line = (line[0], *(PdfPoint(p.x+new_x-attach.x, p.y+new_y-attach.y) for p in line[1:]))
            painter.save()
            painter.setPen(QPen(QColor(45, 120, 230), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            display = self.view._display_rect(summary.page_index, gesture.box)
            painter.drawRect(display)
            if len(line) >= 2:
                painter.drawPolyline(QPolygonF([QPointF(*mapper.canonical_to_viewport((item.x, item.y)))
                                                for item in line]))
            painter.restore()
        if selected:
            box = gesture.box if gesture and gesture.summary.logical_id == summary.logical_id else summary.geometry.text_rect
            painter.save()
            painter.setPen(QPen(QColor(45, 120, 230), 1))
            painter.setBrush(QColor("white"))
            if summary.capabilities.resizable and box is not None:
                for x, y in ((box.x0, box.y0), (box.x1, box.y0), (box.x0, box.y1), (box.x1, box.y1)):
                    px, py = mapper.canonical_to_viewport((x, y))
                    painter.drawRect(QRectF(px-3, py-3, 6, 6))
            if summary.capabilities.movable_anchor and summary.geometry.callout_line:
                anchor = gesture.anchor if gesture else summary.geometry.callout_line[0]
                px, py = mapper.canonical_to_viewport((anchor.x, anchor.y))
                painter.drawEllipse(QPointF(px, py), 4, 4)
            painter.restore()
