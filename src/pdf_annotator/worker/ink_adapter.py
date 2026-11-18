"""Native PDF Ink adapter behind the common M5 command boundary."""

import logging
from typing import Any

from ..domain.annotation_commands import CreateInk, DeleteAnnotation, MoveInk
from ..domain.annotation_models import AnnotationModel, AnnotationOwnership
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.identifiers import PdfBackingReference
from ..domain.kinds import AnnotationKind
from ..domain.targets import InkTarget, clamp_ink_translation, translate_ink_target
from .annotation_dispatch import AppliedMutation


def create_ink(document: Any, command: CreateInk) -> AppliedMutation:
    import pymupdf

    page = document.load_page(command.target.page_index)
    # The pinned 1.28.2 binding requires primitive float pairs here even though
    # the public docs describe generic point-like values.
    paths = [[(point.x, point.y) for point in path]
             for path in command.target.paths]
    annotation = None
    try:
        annotation = page.add_ink_annot(paths)
        color = command.style.color
        annotation.set_border(width=command.style.width)
        annotation.set_colors(stroke=(color.red, color.green, color.blue))
        annotation.update(opacity=command.style.opacity)
        if command.metadata is not None and command.metadata.author is not None:
            annotation.set_info(title=command.metadata.author)
        # Keep the app identity authoritative across reopen; xref is only a
        # generation-local technical locator.
        pdf_name = command.annotation_id.value
        document.xref_set_key(annotation.xref, "NM", pymupdf.get_pdf_str(pdf_name))
        model = AnnotationModel(
            command.annotation_id, command.context.document_id,
            command.context.document_generation, AnnotationKind.INK,
            command.target, None, command.style,
            AnnotationOwnership.CREATED_BY_APP, command.context.source,
            PdfBackingReference(pdf_name, annotation.xref), metadata=command.metadata)
        return AppliedMutation(annotation=model)
    except Exception:
        if annotation is not None:
            try:
                page.delete_annot(annotation)
            except Exception as rollback_error:
                logging.getLogger("pdf_annotator").warning(
                    "ink rollback failed error=%s", type(rollback_error).__name__)
        raise


def delete_ink(document: Any, command: DeleteAnnotation) -> AppliedMutation:
    """Delete one Ink already authenticated as app-created by the adapter."""
    page = document.load_page(command.page_index)
    expected_name = command.annotation_id.value
    for annotation in page.annots() or ():
        info = annotation.info or {}
        if annotation.type[1] == "Ink" and info.get("id") == expected_name:
            page.delete_annot(annotation)
            return AppliedMutation()
    raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)


def move_ink(document: Any, command: MoveInk, model: AnnotationModel) -> AppliedMutation:
    """Replace one app-owned `/Ink` while retaining its logical PDF identity.

    PyMuPDF 1.28 exposes Ink vertices as read-only and cannot translate an Ink
    rectangle. Recreating only this app-owned annotation preserves `/NM`, the
    complete path topology and style while applying the translation to `/InkList`.
    """
    if (model.kind != AnnotationKind.INK or not isinstance(model.target, InkTarget)
            or model.style is None or model.annotation_id != command.annotation_id):
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    page = document.load_page(command.page_index)
    width, height = page.rect.width, page.rect.height
    if page.rotation in (90, 270):
        width, height = height, width
    delta_x, delta_y = clamp_ink_translation(model.target, width, height,
                                              command.delta_x, command.delta_y)
    if delta_x == 0.0 and delta_y == 0.0:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
    target = translate_ink_target(model.target, delta_x, delta_y)
    existing = _find_ink(page, command.annotation_id.value)
    if existing is None:
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    replacement = None
    try:
        author = existing.info.get("title")
        replacement = _add_ink(document, page, target, model.style, command.annotation_id.value)
        if author:
            replacement.set_info(title=author)
        page.delete_annot(existing)
        moved = AnnotationModel(
            model.annotation_id, model.document_id, model.document_generation,
            AnnotationKind.INK, target, None, model.style,
            model.ownership, model.source,
            PdfBackingReference(command.annotation_id.value, replacement.xref), metadata=model.metadata)
        return AppliedMutation(annotation=moved)
    except Exception:
        if replacement is not None:
            try:
                page.delete_annot(replacement)
            except Exception as rollback_error:
                logging.getLogger("pdf_annotator").warning(
                    "Ink move rollback failed error=%s", type(rollback_error).__name__)
        raise


def _find_ink(page: Any, annotation_id: str) -> Any | None:
    for annotation in page.annots() or ():
        info = annotation.info or {}
        if annotation.type[1] == "Ink" and info.get("id") == annotation_id:
            return annotation
    return None


def _add_ink(document: Any, page: Any, target: InkTarget, style: Any, pdf_name: str) -> Any:
    import pymupdf

    paths = [[(point.x, point.y) for point in path] for path in target.paths]
    annotation = page.add_ink_annot(paths)
    color = style.color
    annotation.set_border(width=style.width)
    annotation.set_colors(stroke=(color.red, color.green, color.blue))
    annotation.update(opacity=style.opacity)
    document.xref_set_key(annotation.xref, "NM", pymupdf.get_pdf_str(pdf_name))
    return annotation
