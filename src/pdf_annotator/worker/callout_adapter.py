"""Native FreeTextCallout adapter, confined to the document-owning worker."""

from typing import Any

from ..domain.annotation_commands import (CreateCallout, MoveCallout, MoveCalloutAnchor,
                                           ResizeCallout, TransferCallout, UpdateCallout,
                                           DeleteAnnotation)
from ..domain.annotation_models import AnnotationModel, AnnotationOwnership
from ..domain.callout_geometry import callout_line, move_callout_box
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.identifiers import PdfBackingReference
from ..domain.kinds import AnnotationKind
from ..domain.rich_text import RichText
from ..domain.targets import CalloutTarget
from .annotation_dispatch import AppliedMutation
from .freetext_adapter import _add, _find


def create_callout(document: Any, command: CreateCallout) -> AppliedMutation:
    target = _with_knee(command.target)
    page = document.load_page(target.page_index)
    annot = _add(document, page, target.text_rect, command.rich_text, command.style,
                 command.annotation_id.value, callout=_points(target),
                 author=command.metadata.author if command.metadata else None)
    return AppliedMutation(annotation=_model(command, target, annot.xref))


def apply_callout(document: Any, command, model: AnnotationModel) -> AppliedMutation:
    if model.kind != AnnotationKind.CALLOUT or not isinstance(model.target, CalloutTarget):
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    target, rich, style = model.target, model.rich_text, model.style
    if isinstance(command, UpdateCallout): target, rich, style = command.target, command.rich_text, command.style
    elif isinstance(command, ResizeCallout): target = command.target
    elif isinstance(command, MoveCalloutAnchor): target = command.target
    elif isinstance(command, MoveCallout): target = _translated(target, document.load_page(model.page_index), command.delta_x, command.delta_y)
    elif isinstance(command, TransferCallout): target = command.target
    else: raise DomainValidationError(MutationErrorCode.UNSUPPORTED_COMMAND)
    target = _with_knee(target)
    if not isinstance(rich, RichText):
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    old_page = document.load_page(model.page_index)
    old = _find(old_page, model.annotation_id.value)
    if old is None: raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    page = document.load_page(target.page_index)
    replacement = _add(document, page, target.text_rect, rich, style, model.annotation_id.value,
                       callout=_points(target), author=old.info.get("title") or None)
    try:
        old_page.delete_annot(old)
    except Exception:
        page.delete_annot(replacement); raise
    return AppliedMutation(annotation=AnnotationModel(model.annotation_id, model.document_id,
        model.document_generation, AnnotationKind.CALLOUT, target, rich.plain, style,
        model.ownership, model.source, PdfBackingReference(model.annotation_id.value, replacement.xref),
        rich, model.metadata))


def delete_callout(document: Any, command: DeleteAnnotation) -> AppliedMutation:
    page = document.load_page(command.page_index); annot = _find(page, command.annotation_id.value)
    if annot is None: raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    page.delete_annot(annot); return AppliedMutation()


def _model(command: CreateCallout, target: CalloutTarget, xref: int) -> AnnotationModel:
    return AnnotationModel(command.annotation_id, command.context.document_id, command.context.document_generation,
        AnnotationKind.CALLOUT, target, command.rich_text.plain, command.style,
        AnnotationOwnership.CREATED_BY_APP, command.context.source,
        PdfBackingReference(command.annotation_id.value, xref), command.rich_text, command.metadata)


def _points(target: CalloutTarget):
    return tuple((point.x, point.y) for point in callout_line(target.text_rect, target.anchor))


def _with_knee(target: CalloutTarget) -> CalloutTarget:
    """The knee is derived geometry, never an independently editable value."""
    return CalloutTarget(target.page_index, target.text_rect, target.anchor,
                         callout_line(target.text_rect, target.anchor)[1])


def _translated(target: CalloutTarget, page: Any, dx: float, dy: float) -> CalloutTarget:
    width, height = page.rect.width, page.rect.height
    dx = min(width - target.text_rect.x1, max(-target.text_rect.x0, dx)); dy = min(height-target.text_rect.y1, max(-target.text_rect.y0, dy))
    r = target.text_rect
    moved, _line = move_callout_box(r, callout_line(r, target.anchor), dx, dy)
    return _with_knee(CalloutTarget(target.page_index, moved, target.anchor))
