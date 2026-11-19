"""Explicit primitive-only schema for commands, models and mutation results."""

import json
from typing import Any

from .annotation_commands import (AUTHORED_CREATIONS, COMMAND_SCHEMA_VERSION, AnnotationCommand, CommandContext,
                                  CommandSource, CreateCallout, CreateDeleteText,
                                  CreateFreeText, CreateHighlight, CreateInk,
                                  CreateInsertText, CreateReplaceText, CreateStickyNote, DeleteAnnotation, DeleteExternalAnnotation,
                                  MoveFreeText, MoveInk, ResizeFreeText, TransferFreeText,
                                  UpdateFreeText, MoveCallout, UpdateCallout, ResizeCallout,
                                  MoveCalloutAnchor, TransferCallout, Undo, Redo)
from .annotation_models import AnnotationModel, AnnotationOwnership
from .annotation_inventory import (AnnotationCapabilities, AnnotationGeometry,
                                   AnnotationReply, AnnotationSummary)
from .errors import DomainValidationError, MutationError, MutationErrorCode
from .geometry import PdfPoint, PdfQuad, PdfRect
from .identifiers import AnnotationId, PdfBackingReference
from .kinds import AnnotationKind, CommandKind
from .results import HistoryDelta, MutationOutcome, MutationResult
from .rich_text import RichText, RichTextRun
from .styles import FreeTextStyle, HighlightStyle, InkStyle, RgbColor
from .targets import (AnnotationTarget, BoundaryAffinity, CalloutTarget, InkTarget,
                      RectTarget, TextInsertionTarget, TextRangeTarget)
from .text import (TextCharacter, TextChunk, TextChunkDescriptor, TextLine,
                   TextPageIndex)

from .external_edits import EditExternalAnnotation, ExternalEditKind
from .editorial_metadata import EditorialMetadata

Payload = dict[str, Any]


def _editorial_metadata(value: Any) -> EditorialMetadata | None:
    if value is None:
        return None
    data = _dict(value, "editorial metadata")
    _exact(data, {"reason", "author"}, "editorial metadata")
    return EditorialMetadata(data["reason"], data["author"])


def _metadata_payload(value: EditorialMetadata) -> Payload:
    return {"reason": value.reason, "author": value.author}


def canonical_json(payload: Payload) -> str:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _dict(value: Any, name: str) -> Payload:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, f"Invalid {name}")
    return value


def _exact(data: Payload, fields: set[str], name: str) -> None:
    if set(data) != fields:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, f"Invalid {name} fields")


def point_to_payload(point: PdfPoint) -> list[float]:
    return [point.x, point.y]


def point_from_payload(payload: Any) -> PdfPoint:
    if not isinstance(payload, list) or len(payload) != 2:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
    try:
        return PdfPoint(*payload)
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY) from exc


def rect_to_payload(rect: PdfRect) -> list[float]:
    return [rect.x0, rect.y0, rect.x1, rect.y1]


def rect_from_payload(payload: Any) -> PdfRect:
    if not isinstance(payload, list) or len(payload) != 4:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
    try:
        return PdfRect(*payload)
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY) from exc


def quad_to_payload(quad: PdfQuad) -> list[list[float]]:
    return [point_to_payload(point) for point in quad.points]


def quad_from_payload(payload: Any) -> PdfQuad:
    if not isinstance(payload, list) or len(payload) != 4:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
    try:
        return PdfQuad(*(point_from_payload(point) for point in payload))
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY) from exc


def color_to_payload(color: RgbColor) -> list[float]:
    return [color.red, color.green, color.blue]


def color_from_payload(payload: Any) -> RgbColor:
    if not isinstance(payload, list) or len(payload) != 3:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid color")
    try:
        return RgbColor(*payload)
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid color") from exc


def rich_text_to_payload(value: RichText) -> list[Payload]:
    if not isinstance(value, RichText):
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid rich text")
    return [{"text": run.text, "font_family": run.font_family, "font_size": run.font_size,
             "color": color_to_payload(run.color), "bold": run.bold, "italic": run.italic}
            for run in value.runs]


def rich_text_from_payload(payload: Any) -> RichText:
    if not isinstance(payload, list) or not payload:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid rich text")
    try:
        runs = []
        for item in payload:
            data = _dict(item, "rich-text run")
            _exact(data, {"text", "font_family", "font_size", "color", "bold", "italic"},
                   "rich-text run")
            runs.append(RichTextRun(data["text"], data["font_family"], data["font_size"],
                                    color_from_payload(data["color"]), data["bold"], data["italic"]))
        return RichText(tuple(runs))
    except DomainValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid rich text") from exc


def style_to_payload(style: InkStyle | HighlightStyle | FreeTextStyle) -> Payload:
    if isinstance(style, InkStyle):
        return {"type": "ink", "color": color_to_payload(style.color),
                "width": style.width, "opacity": style.opacity}
    if isinstance(style, HighlightStyle):
        return {"type": "highlight", "color": color_to_payload(style.color),
                "opacity": style.opacity}
    if isinstance(style, FreeTextStyle):
        return {"type": "free_text", "border": color_to_payload(style.border),
                "fill": color_to_payload(style.fill) if style.fill is not None else None,
                "text": color_to_payload(style.text), "opacity": style.opacity,
                "border_width": style.border_width, "font_size": style.font_size,
                "font_family": style.font_family}
    raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid style")


def style_from_payload(payload: Any) -> InkStyle | HighlightStyle | FreeTextStyle:
    data = _dict(payload, "style")
    try:
        kind = data.get("type")
        if kind == "ink":
            _exact(data, {"type", "color", "width", "opacity"}, "ink style")
            return InkStyle(color_from_payload(data["color"]), data["width"], data["opacity"])
        if kind == "highlight":
            _exact(data, {"type", "color", "opacity"}, "highlight style")
            return HighlightStyle(color_from_payload(data["color"]), data["opacity"])
        if kind == "free_text":
            _exact(data, {"type", "border", "fill", "text", "opacity", "border_width",
                          "font_size", "font_family"}, "FreeText style")
            fill = color_from_payload(data["fill"]) if data["fill"] is not None else None
            return FreeTextStyle(color_from_payload(data["border"]), fill,
                                 color_from_payload(data["text"]), data["opacity"],
                                 data["border_width"], data["font_size"], data["font_family"])
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid style") from exc
    raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Unknown style")


def target_to_payload(target: AnnotationTarget) -> Payload:
    if isinstance(target, RectTarget):
        return {"type": "rect", "page_index": target.page_index,
                "rect": rect_to_payload(target.rect)}
    if isinstance(target, InkTarget):
        return {"type": "ink", "page_index": target.page_index,
                "paths": [[point_to_payload(point) for point in path] for path in target.paths]}
    if isinstance(target, CalloutTarget):
        return {"type": "callout", "page_index": target.page_index,
                "text_rect": rect_to_payload(target.text_rect),
                "anchor": point_to_payload(target.anchor),
                "knee": point_to_payload(target.knee) if target.knee is not None else None}
    if isinstance(target, TextRangeTarget):
        return {"type": "text_range", "target_id": target.target_id,
                "page_index": target.page_index,
                "quads": [quad_to_payload(quad) for quad in target.quads],
                "original_text": target.original_text,
                "snapshot_id": target.snapshot_id,
                "start_char_index": target.start_char_index,
                "end_char_index": target.end_char_index}
    if isinstance(target, TextInsertionTarget):
        return {"type": "text_insertion", "target_id": target.target_id,
                "page_index": target.page_index, "anchor": point_to_payload(target.anchor),
                "affinity": target.affinity.value, "context_before": target.context_before,
                "context_after": target.context_after, "snapshot_id": target.snapshot_id,
                "char_index": target.char_index,
                "boundary_top": point_to_payload(target.boundary_top) if target.boundary_top else None}
    raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid target")


def target_from_payload(payload: Any) -> AnnotationTarget:
    data = _dict(payload, "target")
    try:
        kind = data.get("type")
        if kind == "rect":
            _exact(data, {"type", "page_index", "rect"}, "rectangle target")
            return RectTarget(data["page_index"], rect_from_payload(data["rect"]))
        if kind == "ink":
            _exact(data, {"type", "page_index", "paths"}, "ink target")
            paths = data["paths"]
            if not isinstance(paths, list):
                raise ValueError
            return InkTarget(data["page_index"], tuple(tuple(point_from_payload(point)
                                                              for point in path)
                                                       for path in paths))
        if kind == "callout":
            _exact(data, {"type", "page_index", "text_rect", "anchor", "knee"},
                   "callout target")
            knee = point_from_payload(data["knee"]) if data["knee"] is not None else None
            return CalloutTarget(data["page_index"], rect_from_payload(data["text_rect"]),
                                 point_from_payload(data["anchor"]), knee)
        if kind == "text_range":
            _exact(data, {"type", "target_id", "page_index", "quads", "original_text",
                          "snapshot_id", "start_char_index", "end_char_index"},
                   "text range target")
            quads = data["quads"]
            if not isinstance(quads, list):
                raise ValueError
            return TextRangeTarget(data["target_id"], data["page_index"],
                                   tuple(quad_from_payload(quad) for quad in quads),
                                   data["original_text"], data["snapshot_id"],
                                   data["start_char_index"], data["end_char_index"])
        if kind == "text_insertion":
            _exact(data, {"type", "target_id", "page_index", "anchor", "affinity",
                          "context_before", "context_after", "snapshot_id", "char_index",
                          "boundary_top"}, "insertion target")
            return TextInsertionTarget(data["target_id"], data["page_index"],
                                       point_from_payload(data["anchor"]),
                                       BoundaryAffinity(data["affinity"]),
                                       data["context_before"], data["context_after"],
                                       data["snapshot_id"], data["char_index"],
                                       point_from_payload(data["boundary_top"])
                                       if data["boundary_top"] is not None else None)
    except DomainValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid target") from exc
    raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Unknown target")


def _context_payload(context: CommandContext) -> Payload:
    payload = {"schema_version": context.schema_version, "command_id": context.command_id,
               "document_id": context.document_id,
               "document_generation": context.document_generation, "source": context.source.value}
    if context.review_snapshot_id is not None:
        payload["review_snapshot_id"] = context.review_snapshot_id
    return payload


def context_from_payload(payload: Any) -> CommandContext:
    data = _dict(payload, "command")
    required = {"schema_version", "command_id", "document_id", "document_generation", "source"}
    if not required <= set(data):
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Missing command identity")
    try:
        return CommandContext(data["command_id"], data["document_id"],
                              data["document_generation"], CommandSource(data["source"]),
                              data["schema_version"], data.get("review_snapshot_id"))
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND, "Invalid command identity") from exc


def command_to_payload(command: AnnotationCommand) -> Payload:
    context = _context_payload(command.context)
    if isinstance(command, (Undo, Redo)):
        return {**context, "kind": command.kind.value}
    payload = {**context, "kind": command.kind.value,
               "annotation_id": command.annotation_id.value}
    if isinstance(command, EditExternalAnnotation):
        return {**payload, "logical_id": command.logical_id, "page_index": command.page_index,
                "edit": command.edit.value, "delta": list(command.delta) if command.delta else None,
                "rect": rect_to_payload(command.rect) if command.rect else None,
                "anchor": point_to_payload(command.anchor) if command.anchor else None,
                "rich_text": rich_text_to_payload(command.rich_text) if command.rich_text else None,
                "style": style_to_payload(command.style) if command.style else None,
                **({"color": color_to_payload(command.color)} if command.color is not None else {})}
    if isinstance(command, DeleteAnnotation):
        return {**payload, "page_index": command.page_index}
    if isinstance(command, DeleteExternalAnnotation):
        return {**payload, "page_index": command.page_index, "logical_id": command.logical_id}
    if isinstance(command, MoveInk):
        return {**payload, "page_index": command.page_index,
                "delta_x": command.delta_x, "delta_y": command.delta_y}
    if isinstance(command, MoveFreeText):
        return {**payload, "page_index": command.page_index,
                "delta_x": command.delta_x, "delta_y": command.delta_y}
    if isinstance(command, UpdateFreeText):
        return {**payload, "page_index": command.page_index, "text": command.text,
                "style": style_to_payload(command.style),
                "rich_text": rich_text_to_payload(command.rich_text)}
    if isinstance(command, ResizeFreeText):
        return {**payload, "target": target_to_payload(command.target)}
    if isinstance(command, TransferFreeText):
        return {**payload, "source_page_index": command.source_page_index,
                "target": target_to_payload(command.target)}
    if isinstance(command, MoveCallout):
        return {**payload, "page_index": command.page_index,
                "delta_x": command.delta_x, "delta_y": command.delta_y}
    if isinstance(command, UpdateCallout):
        return {**payload, "target": target_to_payload(command.target), "text": command.text,
                "style": style_to_payload(command.style),
                "rich_text": rich_text_to_payload(command.rich_text)}
    if isinstance(command, (ResizeCallout, MoveCalloutAnchor)):
        return {**payload, "target": target_to_payload(command.target)}
    if isinstance(command, TransferCallout):
        return {**payload, "source_page_index": command.source_page_index,
                "target": target_to_payload(command.target)}
    payload["target"] = target_to_payload(command.target)
    if isinstance(command, (CreateInk, CreateHighlight, CreateFreeText, CreateCallout)):
        payload["style"] = style_to_payload(command.style)
    if isinstance(command, (CreateFreeText, CreateCallout, CreateInsertText, CreateReplaceText, CreateStickyNote)):
        payload["text"] = command.text
    if isinstance(command, (CreateFreeText, CreateCallout)):
        payload["rich_text"] = rich_text_to_payload(command.rich_text)
    if isinstance(command, AUTHORED_CREATIONS) and command.metadata is not None:
        payload["metadata"] = _metadata_payload(command.metadata)
    return payload


def command_from_payload(payload: Any) -> AnnotationCommand:
    data = _dict(payload, "command")
    context = context_from_payload(data)
    base = {"schema_version", "command_id", "document_id", "document_generation", "source",
            "kind", "annotation_id"}
    if context.review_snapshot_id is not None:
        base.add("review_snapshot_id")
    try:
        kind = CommandKind(data["kind"])
    except (KeyError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.UNSUPPORTED_COMMAND) from exc
    try:
        if kind == CommandKind.UNDO:
            _exact(data, base - {"annotation_id"}, "undo command")
            return Undo(context)
        if kind == CommandKind.REDO:
            _exact(data, base - {"annotation_id"}, "redo command")
            return Redo(context)
        annotation_id = AnnotationId(data["annotation_id"])
        if kind == CommandKind.EDIT_EXTERNAL_ANNOTATION:
            _exact(data, base | {"logical_id", "page_index", "edit", "delta", "rect", "anchor",
                                 "rich_text", "style"} | ({"color"} if data.get("edit") == "color" else set()), "external edit")
            return EditExternalAnnotation(context, annotation_id, data["logical_id"], data["page_index"],
                ExternalEditKind(data["edit"]), tuple(data["delta"]) if data["delta"] is not None else None,
                rect_from_payload(data["rect"]) if data["rect"] is not None else None,
                point_from_payload(data["anchor"]) if data["anchor"] is not None else None,
                rich_text_from_payload(data["rich_text"]) if data["rich_text"] is not None else None,
                style_from_payload(data["style"]) if data["style"] is not None else None,
                color_from_payload(data["color"]) if data.get("color") is not None else None)
        if kind == CommandKind.DELETE_ANNOTATION:
            _exact(data, base | {"page_index"}, "delete command")
            return DeleteAnnotation(context, annotation_id, data["page_index"])
        if kind == CommandKind.DELETE_EXTERNAL_ANNOTATION:
            _exact(data, base | {"page_index", "logical_id"}, "external delete command")
            return DeleteExternalAnnotation(context, annotation_id, data["logical_id"], data["page_index"])
        if kind == CommandKind.MOVE_INK:
            _exact(data, base | {"page_index", "delta_x", "delta_y"}, "Ink move command")
            return MoveInk(context, annotation_id, data["page_index"],
                           data["delta_x"], data["delta_y"])
        if kind == CommandKind.MOVE_FREE_TEXT:
            _exact(data, base | {"page_index", "delta_x", "delta_y"}, "FreeText move command")
            return MoveFreeText(context, annotation_id, data["page_index"],
                                data["delta_x"], data["delta_y"])
        if kind == CommandKind.UPDATE_FREE_TEXT:
            _exact(data, base | {"page_index", "text", "style", "rich_text"}, "FreeText update command")
            return UpdateFreeText(context, annotation_id, data["page_index"], data["text"],
                                  style_from_payload(data["style"]),
                                  rich_text_from_payload(data["rich_text"]))
        if kind == CommandKind.RESIZE_FREE_TEXT:
            _exact(data, base | {"target"}, "FreeText resize command")
            return ResizeFreeText(context, annotation_id, target_from_payload(data["target"]))
        if kind == CommandKind.TRANSFER_FREE_TEXT:
            _exact(data, base | {"source_page_index", "target"}, "FreeText transfer command")
            return TransferFreeText(context, annotation_id, data["source_page_index"],
                                    target_from_payload(data["target"]))
        if kind == CommandKind.MOVE_CALLOUT:
            _exact(data, base | {"page_index", "delta_x", "delta_y"}, "callout move command")
            return MoveCallout(context, annotation_id, data["page_index"], data["delta_x"], data["delta_y"])
        if kind == CommandKind.UPDATE_CALLOUT:
            _exact(data, base | {"target", "text", "style", "rich_text"}, "callout update command")
            return UpdateCallout(context, annotation_id, target_from_payload(data["target"]), data["text"],
                                 style_from_payload(data["style"]), rich_text_from_payload(data["rich_text"]))
        if kind == CommandKind.RESIZE_CALLOUT:
            _exact(data, base | {"target"}, "callout resize command")
            return ResizeCallout(context, annotation_id, target_from_payload(data["target"]))
        if kind == CommandKind.MOVE_CALLOUT_ANCHOR:
            _exact(data, base | {"target"}, "callout anchor command")
            return MoveCalloutAnchor(context, annotation_id, target_from_payload(data["target"]))
        if kind == CommandKind.TRANSFER_CALLOUT:
            _exact(data, base | {"source_page_index", "target"}, "callout transfer command")
            return TransferCallout(context, annotation_id, data["source_page_index"], target_from_payload(data["target"]))
        target = target_from_payload(data["target"])
        metadata_field = {"metadata"} if "metadata" in data else set()
        if kind == CommandKind.CREATE_INK:
            _exact(data, base | {"target", "style"} | metadata_field, "ink command")
            return CreateInk(context, target, style_from_payload(data["style"]), annotation_id,
                             _editorial_metadata(data.get("metadata")))
        if kind == CommandKind.CREATE_HIGHLIGHT:
            _exact(data, base | {"target", "style"} | metadata_field, "highlight command")
            return CreateHighlight(context, target, style_from_payload(data["style"]), annotation_id,
                                   _editorial_metadata(data.get("metadata")))
        if kind == CommandKind.CREATE_FREE_TEXT:
            _exact(data, base | {"target", "style", "text", "rich_text"} | metadata_field, "FreeText command")
            return CreateFreeText(context, target, data["text"],
                                  style_from_payload(data["style"]), annotation_id,
                                  rich_text_from_payload(data["rich_text"]), _editorial_metadata(data.get("metadata")))
        if kind == CommandKind.CREATE_CALLOUT:
            _exact(data, base | {"target", "style", "text", "rich_text"} | metadata_field, "callout command")
            return CreateCallout(context, target, data["text"],
                                 style_from_payload(data["style"]), annotation_id,
                                 rich_text_from_payload(data["rich_text"]), _editorial_metadata(data.get("metadata")))
        if kind == CommandKind.CREATE_DELETE_TEXT:
            _exact(data, base | {"target"} | metadata_field, "delete-text command")
            return CreateDeleteText(context, target, annotation_id, _editorial_metadata(data.get("metadata")))
        if kind == CommandKind.CREATE_INSERT_TEXT:
            _exact(data, base | {"target", "text"} | metadata_field, "insert-text command")
            return CreateInsertText(context, target, data["text"], annotation_id, _editorial_metadata(data.get("metadata")))
        if kind == CommandKind.CREATE_REPLACE_TEXT:
            _exact(data, base | {"target", "text"} | metadata_field, "replace-text command")
            return CreateReplaceText(context, target, data["text"], annotation_id, _editorial_metadata(data.get("metadata")))
        if kind == CommandKind.CREATE_STICKY_NOTE:
            _exact(data, base | {"target", "text"}, "sticky-note command")
            return CreateStickyNote(context, target, data["text"], annotation_id)
    except DomainValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND) from exc
    raise DomainValidationError(MutationErrorCode.UNSUPPORTED_COMMAND)


def error_to_payload(error: MutationError) -> Payload:
    return {"code": error.code.value, "message": error.message}


def error_from_payload(payload: Any) -> MutationError:
    data = _dict(payload, "mutation error")
    _exact(data, {"code", "message"}, "mutation error")
    try:
        return MutationError(MutationErrorCode(data["code"]), data["message"])
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc


def result_to_payload(result: MutationResult) -> Payload:
    return {"schema_version": COMMAND_SCHEMA_VERSION, "command_id": result.command_id,
            "document_id": result.document_id, "document_generation": result.document_generation,
            "outcome": result.outcome.value, "revision_before": result.revision_before,
            "document_revision": result.document_revision,
            "annotation_id": result.annotation_id.value if result.annotation_id else None,
            "annotation": annotation_to_payload(result.annotation) if result.annotation else None,
            "error": error_to_payload(result.error) if result.error else None,
            "history_position": result.history_position,
            "history_length": result.history_length,
            "history_state_id": result.history_state_id,
            "refresh_inventory": result.refresh_inventory,
            "history_delta": history_delta_to_payload(result.history_delta)}


def result_from_payload(payload: Any) -> MutationResult:
    data = _dict(payload, "mutation result")
    _exact(data, {"schema_version", "command_id", "document_id", "document_generation",
                  "outcome", "revision_before", "document_revision", "annotation_id",
                  "annotation", "error", "history_position", "history_length", "history_state_id", "refresh_inventory",
                  "history_delta"},
           "mutation result")
    if data["schema_version"] != COMMAND_SCHEMA_VERSION:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR)
    try:
        return MutationResult(data["command_id"], data["document_id"], data["document_generation"],
                              MutationOutcome(data["outcome"]), data["revision_before"],
                              data["document_revision"],
                              AnnotationId(data["annotation_id"]) if data["annotation_id"] else None,
                              annotation_from_payload(data["annotation"])
                              if data["annotation"] else None,
                              error_from_payload(data["error"]) if data["error"] else None,
                              data["history_position"], data["history_length"],
                              data["history_state_id"], data["refresh_inventory"],
                              history_delta_from_payload(data["history_delta"]))
    except DomainValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc


def history_delta_to_payload(delta: HistoryDelta | None) -> Payload | None:
    if delta is None:
        return None
    return {
        "added": [annotation_to_payload(item) for item in delta.added],
        "removed_ids": list(delta.removed_ids),
        "updated": [annotation_to_payload(item) for item in delta.updated],
        "inventory_added": [_summary_to_payload(item) for item in delta.inventory_added],
        "inventory_removed_ids": list(delta.inventory_removed_ids),
        "inventory_updated": [_summary_to_payload(item) for item in delta.inventory_updated],
        "appearance_invalidated_ids": list(delta.appearance_invalidated_ids),
    }


def history_delta_from_payload(payload: Any) -> HistoryDelta | None:
    if payload is None:
        return None
    data = _dict(payload, "history delta")
    _exact(data, {"added", "removed_ids", "updated", "inventory_added", "inventory_removed_ids",
                  "inventory_updated", "appearance_invalidated_ids"}, "history delta")
    try:
        return HistoryDelta(
            tuple(annotation_from_payload(item) for item in data["added"]),
            tuple(data["removed_ids"]),
            tuple(annotation_from_payload(item) for item in data["updated"]),
            tuple(_summary_from_payload(item) for item in data["inventory_added"]),
            tuple(data["inventory_removed_ids"]),
            tuple(_summary_from_payload(item) for item in data["inventory_updated"]),
            tuple(data["appearance_invalidated_ids"]),
        )
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc


def _summary_to_payload(summary: AnnotationSummary) -> Payload:
    geometry = summary.geometry
    return {"logical_id": summary.logical_id, "page_index": summary.page_index,
            "kind": summary.kind, "subtype": summary.subtype,
            "geometry": {"bbox": rect_to_payload(geometry.bbox),
                         "quads": [quad_to_payload(item) for item in geometry.quads],
                         "paths": [[point_to_payload(point) for point in path]
                                   for path in geometry.paths],
                         "callout_line": [point_to_payload(point) for point in geometry.callout_line],
                         "caret": ([point_to_payload(point) for point in geometry.caret]
                                   if geometry.caret is not None else None),
                         "text_rect": (rect_to_payload(geometry.text_rect)
                                       if geometry.text_rect is not None else None),
                         "line": [point_to_payload(point) for point in geometry.line]},
            "preview": summary.preview, "author": summary.author, "modified": summary.modified,
            "created": summary.created, "style": summary.style,
            "capabilities": {name: getattr(summary.capabilities, name)
                             for name in AnnotationCapabilities.__dataclass_fields__},
            "component_xrefs": list(summary.component_xrefs),
            "appearance_available": summary.appearance_available, "provenance": summary.provenance,
            "native_name": summary.native_name, "reply_ids": list(summary.reply_ids),
            "reply": ({name: getattr(summary.reply, name) for name in AnnotationReply.__dataclass_fields__}
                      if summary.reply is not None else None)}


def _summary_from_payload(payload: Any) -> AnnotationSummary:
    data = _dict(payload, "annotation summary")
    _exact(data, {"logical_id", "page_index", "kind", "subtype", "geometry", "preview", "author",
                  "modified", "created", "style", "capabilities", "component_xrefs",
                  "appearance_available", "provenance", "native_name", "reply", "reply_ids"}, "annotation summary")
    geometry = _dict(data["geometry"], "annotation summary geometry")
    _exact(geometry, {"bbox", "quads", "paths", "callout_line", "caret", "text_rect", "line"},
           "annotation summary geometry")
    try:
        caret = (tuple(point_from_payload(point) for point in geometry["caret"])
                 if geometry["caret"] is not None else None)
        if caret is not None and len(caret) != 2:
            raise ValueError
        return AnnotationSummary(
            str(data["logical_id"]), int(data["page_index"]), str(data["kind"]), str(data["subtype"]),
            AnnotationGeometry(rect_from_payload(geometry["bbox"]),
                               tuple(quad_from_payload(item) for item in geometry["quads"]),
                               tuple(tuple(point_from_payload(point) for point in path)
                                     for path in geometry["paths"]),
                               tuple(point_from_payload(point) for point in geometry["callout_line"]),
                               caret,
                               rect_from_payload(geometry["text_rect"])
                               if geometry["text_rect"] is not None else None,
                               tuple(point_from_payload(point) for point in geometry["line"])),
            str(data["preview"]), data["author"] if isinstance(data["author"], str) else None,
            data["modified"] if isinstance(data["modified"], str) else None,
            data["created"] if isinstance(data["created"], str) else None,
            dict(data["style"]), AnnotationCapabilities(**_dict(data["capabilities"], "capabilities")),
            tuple(int(item) for item in data["component_xrefs"]), bool(data["appearance_available"]),
            str(data["provenance"]), data["native_name"] if isinstance(data["native_name"], str) else None,
            AnnotationReply(**_dict(data["reply"], "annotation reply")) if data["reply"] is not None else None,
            tuple(str(item) for item in data["reply_ids"]))
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc


def backing_to_payload(backing: PdfBackingReference | None) -> Payload | None:
    return (None if backing is None else
            {"pdf_nm": backing.pdf_nm, "xref": backing.xref,
             "associated_xrefs": list(backing.associated_xrefs)})


def backing_from_payload(payload: Any) -> PdfBackingReference | None:
    if payload is None:
        return None
    data = _dict(payload, "PDF backing")
    _exact(data, {"pdf_nm", "xref", "associated_xrefs"}, "PDF backing")
    try:
        return PdfBackingReference(data["pdf_nm"], data["xref"], tuple(data["associated_xrefs"]))
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc


def annotation_to_payload(model: AnnotationModel) -> Payload:
    payload = {"schema_version": COMMAND_SCHEMA_VERSION, "annotation_id": model.annotation_id.value,
            "document_id": model.document_id, "document_generation": model.document_generation,
            "kind": model.kind.value, "target": target_to_payload(model.target), "text": model.text,
            "style": style_to_payload(model.style) if model.style else None,
            "ownership": model.ownership.value,
            "source": model.source.value if model.source else None,
            "backing": backing_to_payload(model.backing),
            "rich_text": rich_text_to_payload(model.rich_text) if model.rich_text else None}
    if model.metadata is not None:
        payload["metadata"] = _metadata_payload(model.metadata)
    return payload


def annotation_from_payload(payload: Any) -> AnnotationModel:
    data = _dict(payload, "annotation")
    _exact(data, {"schema_version", "annotation_id", "document_id", "document_generation",
                  "kind", "target", "text", "style", "ownership", "source", "backing", "rich_text"}
                  | ({"metadata"} if "metadata" in data else set()),
           "annotation")
    if data["schema_version"] != COMMAND_SCHEMA_VERSION:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR)
    try:
        return AnnotationModel(AnnotationId(data["annotation_id"]), data["document_id"],
                               data["document_generation"], AnnotationKind(data["kind"]),
                               target_from_payload(data["target"]), data["text"],
                               style_from_payload(data["style"]) if data["style"] else None,
                               AnnotationOwnership(data["ownership"]),
                               CommandSource(data["source"]) if data["source"] else None,
                               backing_from_payload(data["backing"]),
                               rich_text_from_payload(data["rich_text"])
                               if data["rich_text"] is not None else None,
                               _editorial_metadata(data.get("metadata")))
    except DomainValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc


def text_chunk_to_payload(chunk: TextChunk) -> Payload:
    return {"schema_version": COMMAND_SCHEMA_VERSION,
            "document_id": chunk.document_id,
            "document_generation": chunk.document_generation,
            "page_index": chunk.page_index,
            "page_text_id": chunk.page_text_id,
            "chunk_index": chunk.chunk_index,
            "chunk_id": chunk.chunk_id,
            "characters": [[char.index, char.text, quad_to_payload(char.quad),
                            char.block_index, char.line_index]
                           for char in chunk.characters],
            "lines": [[line.line_index, line.block_index, line.start, line.end,
                       [line.direction[0], line.direction[1]]]
                      for line in chunk.lines]}


def text_chunk_from_payload(payload: Any) -> TextChunk:
    data = _dict(payload, "text chunk")
    _exact(data, {"schema_version", "document_id", "document_generation", "page_index",
                  "page_text_id", "chunk_index", "chunk_id", "characters", "lines"},
           "text chunk")
    if data["schema_version"] != COMMAND_SCHEMA_VERSION:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR)
    try:
        characters = tuple(TextCharacter(record[0], record[1], quad_from_payload(record[2]),
                                         record[3], record[4])
                           for record in data["characters"] if len(record) == 5)
        if len(characters) != len(data["characters"]):
            raise ValueError
        lines = tuple(TextLine(record[0], record[1], record[2], record[3],
                               tuple(record[4]))
                      for record in data["lines"] if len(record) == 5)
        if len(lines) != len(data["lines"]):
            raise ValueError
        return TextChunk(data["document_id"], data["document_generation"],
                         data["page_index"], data["page_text_id"], data["chunk_index"],
                         data["chunk_id"], characters, lines)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc


def text_page_index_to_payload(index: TextPageIndex) -> Payload:
    return {"schema_version": COMMAND_SCHEMA_VERSION,
            "document_id": index.document_id,
            "document_generation": index.document_generation,
            "page_index": index.page_index,
            "page_text_id": index.page_text_id,
            "character_count": index.character_count,
            "line_count": index.line_count,
            "chunks": [[chunk.chunk_index, chunk.chunk_id,
                        chunk.start_char_index, chunk.end_char_index,
                        chunk.start_line_index, chunk.end_line_index,
                        rect_to_payload(chunk.bounds), chunk.wire_bytes]
                       for chunk in index.chunks]}


def text_page_index_from_payload(payload: Any) -> TextPageIndex:
    data = _dict(payload, "page text index")
    _exact(data, {"schema_version", "document_id", "document_generation", "page_index",
                  "page_text_id", "character_count", "line_count", "chunks"},
           "page text index")
    if data["schema_version"] != COMMAND_SCHEMA_VERSION:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR)
    try:
        chunks = tuple(TextChunkDescriptor(
            record[0], record[1], record[2], record[3], record[4], record[5],
            rect_from_payload(record[6]), record[7])
            for record in data["chunks"] if len(record) == 8)
        if len(chunks) != len(data["chunks"]):
            raise ValueError
        return TextPageIndex(data["document_id"], data["document_generation"],
                             data["page_index"], data["page_text_id"],
                             data["character_count"], data["line_count"], chunks)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise DomainValidationError(MutationErrorCode.PROTOCOL_ERROR) from exc
