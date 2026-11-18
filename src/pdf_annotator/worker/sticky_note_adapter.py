"""Write a standalone native /Text note through the worker annotation adapter."""

from ..domain.annotation_commands import CreateStickyNote
from ..domain.annotation_models import AnnotationModel, AnnotationOwnership
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.geometry import PdfPoint, PdfRect
from ..domain.identifiers import PdfBackingReference
from ..domain.kinds import AnnotationKind
from ..domain.targets import RectTarget
from .annotation_dispatch import AppliedMutation

NOTE_SIZE = 16.0  # MuPDF 1.28.2 /Text icons have a fixed 16 pt footprint.
NOTE_AUTHOR = "Revisione IA"


def note_target(page, point: PdfPoint) -> RectTarget:
    bounds = page.rect * page.derotation_matrix
    if (not (0 <= point.x <= bounds.width and 0 <= point.y <= bounds.height)
            or min(bounds.width, bounds.height) < NOTE_SIZE):
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
    x = min(point.x, bounds.width - NOTE_SIZE)
    y = min(point.y, bounds.height - NOTE_SIZE)
    return RectTarget(page.number, PdfRect(x, y, x + NOTE_SIZE, y + NOTE_SIZE))


def create_sticky_note(document, command: CreateStickyNote) -> AppliedMutation:
    import pymupdf

    target = command.target
    page = document.load_page(target.page_index)
    rect = pymupdf.Rect(target.rect.x0, target.rect.y0, target.rect.x1, target.rect.y1)
    if note_target(page, PdfPoint(rect.x0, rect.y0)) != target:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
    name = command.annotation_id.value
    if any(name in other.annot_names() for other in document):
        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
    # /Text uses NoRotate: the point is the displayed top-left expressed in page coordinates.
    origin = (rect * page.rotation_matrix).tl * page.derotation_matrix
    note = page.add_text_annot(origin, command.text, icon="Note")
    note.set_info(title=NOTE_AUTHOR, subject="Nota di revisione")
    document.xref_set_key(note.xref, "NM", pymupdf.get_pdf_str(name))
    bounds = page.rect * page.derotation_matrix
    width, height = min(240, bounds.width), min(160, bounds.height)
    x = min(rect.x1, bounds.width - width)
    y = min(rect.y0, bounds.height - height)
    note.set_popup(pymupdf.Rect(x, y, x + width, y + height))
    if (any(abs(actual - expected) > .002 for actual, expected in zip(note.rect, rect, strict=True))
            or not note.has_popup or note.info["content"] != command.text):
        raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION)
    backing = PdfBackingReference(name, note.xref, (note.popup_xref,))
    document.reload_page(page)
    model = AnnotationModel(command.annotation_id, command.context.document_id,
                            command.context.document_generation, AnnotationKind.STICKY_NOTE,
                            target, command.text, None, AnnotationOwnership.CREATED_BY_APP,
                            command.context.source, backing)
    return AppliedMutation(annotation=model)
