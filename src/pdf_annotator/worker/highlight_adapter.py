"""Native PDF Highlight adapter behind the common M5 command boundary."""

import logging
from typing import Any

from ..domain.annotation_commands import AnnotationCommand, CreateHighlight, DeleteAnnotation
from ..domain.annotation_models import AnnotationModel, AnnotationOwnership
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.identifiers import PdfBackingReference
from ..domain.kinds import AnnotationKind
from .annotation_dispatch import AppliedMutation
from .text_service import WorkerTextService


def apply_annotation_command(document: Any, command: AnnotationCommand,
                             text_service: WorkerTextService) -> AppliedMutation:
    if not isinstance(command, CreateHighlight):
        raise DomainValidationError(MutationErrorCode.UNSUPPORTED_COMMAND)
    text_service.require_confirmed(command.target)
    return _create_highlight(document, command)


def _create_highlight(document: Any, command: CreateHighlight) -> AppliedMutation:
    import pymupdf

    page = document.load_page(command.target.page_index)
    quads = [pymupdf.Quad(
        (quad.upper_left.x, quad.upper_left.y),
        (quad.upper_right.x, quad.upper_right.y),
        (quad.lower_left.x, quad.lower_left.y),
        (quad.lower_right.x, quad.lower_right.y))
        for quad in command.target.quads]
    annotation = None
    try:
        annotation = page.add_highlight_annot(quads)
        color = command.style.color
        annotation.set_colors(stroke=(color.red, color.green, color.blue))
        annotation.update(opacity=command.style.opacity)
        if command.metadata is not None and command.metadata.author is not None:
            annotation.set_info(title=command.metadata.author)
        # PyMuPDF generates /NM but its public Annot.set_info intentionally
        # ignores id. Keep the identity write isolated in the adapter.
        pdf_name = command.annotation_id.value
        document.xref_set_key(annotation.xref, "NM", pymupdf.get_pdf_str(pdf_name))
        model = AnnotationModel(
            command.annotation_id, command.context.document_id,
            command.context.document_generation, AnnotationKind.HIGHLIGHT,
            command.target, command.target.original_text, command.style,
            AnnotationOwnership.CREATED_BY_APP, command.context.source,
            PdfBackingReference(pdf_name, annotation.xref), metadata=command.metadata)
        return AppliedMutation(annotation=model)
    except Exception:
        if annotation is not None:
            try:
                page.delete_annot(annotation)
            except Exception as rollback_error:
                logging.getLogger("pdf_annotator").warning(
                    "highlight rollback failed error=%s", type(rollback_error).__name__)
        raise


def delete_highlight(document: Any, command: DeleteAnnotation) -> AppliedMutation:
    """Delete one app-owned native Highlight by its stable PDF /NM identity."""
    page = document.load_page(command.page_index)
    for annotation in page.annots() or ():
        info = annotation.info or {}
        if annotation.type[1] == "Highlight" and info.get("id") == command.annotation_id.value:
            page.delete_annot(annotation)
            return AppliedMutation()
    raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
