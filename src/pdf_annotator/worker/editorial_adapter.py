"""M10 Acrobat-observed editorial objects, owned by one PDF worker.

Reference SHA-256 and the exact object graph are documented in M10_REPORT.md.
This adapter is a noninteractive spike, not an Acrobat/InDesign PASS claim.
"""

from contextlib import contextmanager
from datetime import datetime
from html import escape
import logging
from math import hypot
from typing import Any, Iterator

from ..domain.annotation_commands import CreateDeleteText, CreateInsertText, CreateReplaceText, DeleteAnnotation
from ..domain.annotation_models import AnnotationModel, AnnotationOwnership
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.geometry import PdfPoint
from ..domain.identifiers import PdfBackingReference
from ..domain.kinds import AnnotationKind
from ..domain.targets import TextInsertionTarget
from .annotation_dispatch import AppliedMutation
from .text_service import WorkerTextService

EditorialCreate = CreateDeleteText | CreateInsertText | CreateReplaceText
EDITORIAL_KINDS = (AnnotationKind.DELETE_TEXT, AnnotationKind.INSERT_TEXT, AnnotationKind.REPLACE_TEXT)
_RED = (.972549, .392151, .392151)
_PURPLE = (.752945, .215683, .768631)


def component_names(name: str, *, replacement: bool) -> tuple[str, ...]:
    """The Caret is the logical Replace root; xrefs never serve as identity."""
    return (name, name + ":strike") if replacement else (name,)


@contextmanager
def _page_transaction(document: Any, page: Any,
                      existing_components: tuple[int, ...] = ()) -> Iterator[None]:
    """Roll back only annotation objects/containers owned by this operation.

    Public StrikeOut/Caret APIs touch the page's /Annots container and allocate
    new independent objects; they never touch base page streams/resources.
    Snapshot an indirect /Annots array as well as the page (Acrobat uses one).
    No second Document, serialization of the whole PDF or journal is required.
    """
    first_new = document.xref_length()
    saved = {}
    for xref in (page.xref, *existing_components):
        try:
            saved[xref] = document.xref_object(xref)
        except RuntimeError as exc:
            # A Save while undone may turn an explicitly owned null slot into
            # a free slot. Its transaction-before state is still PDF null.
            if "cannot find object in xref" not in str(exc):
                raise
            saved[xref] = "null"
    kind, value = document.xref_get_key(page.xref, "Annots")
    if kind == "xref":
        xref = int(value.split()[0])
        saved[xref] = document.xref_object(xref)
    page_number = page.number
    # The outer dispatcher transaction must not retain a Page wrapper: on
    # 1.28.2 an extra reference prevents the adapter's reload_page() refresh.
    page = None
    try:
        yield
    except Exception:
        if document.is_closed:
            raise
        try:
            for xref, source in saved.items():
                document.update_object(xref, source)
            for xref in range(first_new, document.xref_length()):
                document.update_object(xref, "null")
            current_page = document.load_page(page_number)
            try:
                document.reload_page(current_page)
            except AssertionError:
                document.load_page(page_number)
        except Exception as rollback_error:
            # A damaged live transaction must never remain writable/saveable.
            logging.getLogger("pdf_annotator").error(
                "editorial rollback failed error=%s", type(rollback_error).__name__)
            if not document.is_closed:
                document.close()
            raise RuntimeError("Editorial rollback failed; document closed") from rollback_error
        raise


def _info(document: Any, annotation: Any, name: str, *, text: str | None,
          subject: str, color: tuple[float, float, float], stamp: str,
          author: str = "PDF Annotator") -> None:
    import pymupdf

    annotation.set_colors(stroke=color)
    annotation.set_flags(4)
    annotation.set_info(title=author, subject=subject,
                        creationDate=stamp, modDate=stamp)
    annotation.update()
    document.xref_set_key(annotation.xref, "NM", pymupdf.get_pdf_str(name))
    if text is not None:
        document.xref_set_key(annotation.xref, "Contents", pymupdf.get_pdf_str(text))
        # Mirror the reference's /RC companion without pretending to be Acrobat
        # or exposing rich authoring. /Contents is always the exact plain payload.
        xml_valid = all(c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF
                        or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF
                        for c in text)
        if xml_valid:
            escaped = escape(text).replace("\r", "&#13;").replace("\n", "&#10;")
            rich = ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml">'
                    '<p dir="ltr"><span dir="ltr" xml:space="preserve">'
                    + escaped + '</span></p></body>')
            document.xref_set_key(annotation.xref, "RC", pymupdf.get_pdf_str(rich))


def _popup(document: Any, page: Any, annotation: Any) -> int:
    import pymupdf

    # Popup position is presentation metadata. Use relative page dimensions,
    # keeping its parent/back-reference exactly as in the Acrobat reference.
    width, height = page.cropbox.width, page.cropbox.height
    annotation.set_popup(pymupdf.Rect(width, 0, width * 1.3, height * .15))
    annotation.set_open(False)
    popup = annotation.popup_xref
    document.xref_set_key(popup, "F", "28")
    return popup


def _caret_geometry(document: Any, annotation: Any,
                    top: PdfPoint, bottom: PdfPoint) -> None:
    """Anchor the inner bottom centre on the confirmed text boundary.

    Reference Replace: inner width ~= .70 of text height, height ~= .55,
    /RD ~= .055 on each side. These are measured appearance proportions, not
    offsets in screen pixels or a claim about InDesign's placement rules.
    PyMuPDF 1.28.2 update() resets a Caret to 20x14, so write /Rect and /RD
    after its public appearance generation. The existing /AP scales to /Rect.
    """
    import pymupdf

    height = hypot(bottom.x - top.x, bottom.y - top.y)
    width, inner_height, padding = .70 * height, .55 * height, .055 * height
    rect = pymupdf.Rect(bottom.x - width / 2 - padding, bottom.y - inner_height - padding,
                        bottom.x + width / 2 + padding, bottom.y + padding)
    # Recover the native conversion from this very annotation's public geometry
    # and native Rect. This respects CropBox/UserUnit even on rotated pages,
    # where Page.transformation_matrix has special-case behaviour in 1.28.2.
    current = annotation.rect
    raw = [float(item) for item in document.xref_get_key(annotation.xref, "Rect")[1].strip("[]").split()]
    sx, sy = (raw[2] - raw[0]) / current.width, (raw[3] - raw[1]) / current.height
    dx, dy = raw[0] - current.x0 * sx, raw[3] + current.y0 * sy
    native = (rect.x0 * sx + dx, dy - rect.y1 * sy,
              rect.x1 * sx + dx, dy - rect.y0 * sy)
    document.xref_set_key(annotation.xref, "Rect", "[" + " ".join(f"{v:.8f}" for v in native) + "]")
    document.xref_set_key(annotation.xref, "RD", "[" + " ".join(
        f"{v:.8f}" for v in (padding * sx, padding * sy, padding * sx, padding * sy)) + "]")
    # Include the same /RD inset in the public icon's appearance bounds, so
    # the drawn caret's bottom centre agrees with the effective text boundary.
    appearance = int(document.xref_get_key(annotation.xref, "AP/N")[1].split()[0])
    bbox = [float(item) for item in document.xref_get_key(appearance, "BBox")[1].strip("[]").split()]
    pad_x = (bbox[2] - bbox[0]) * padding / width
    pad_y = (bbox[3] - bbox[1]) * padding / inner_height
    bounds = (bbox[0] - pad_x, bbox[1] - pad_y, bbox[2] + pad_x, bbox[3] + pad_y)
    document.xref_set_key(appearance, "BBox", "[" + " ".join(f"{v:.8f}" for v in bounds) + "]")


def create_editorial(document: Any, command: EditorialCreate,
                     text_service: WorkerTextService) -> AppliedMutation:
    import pymupdf

    text_service.require_confirmed(command.target)
    replacement = isinstance(command, CreateReplaceText)
    names = component_names(command.annotation_id.value, replacement=replacement)
    for page_index in range(document.page_count):
        if set(document[page_index].annot_names()) & set(names):
            raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
    page = document.load_page(command.target.page_index)
    stamp = datetime.now().astimezone().strftime("D:%Y%m%d%H%M%S%z")
    stamp = stamp[:-2] + "'" + stamp[-2:] + "'"
    metadata = command.metadata
    author = metadata.author if metadata is not None and metadata.author is not None else "PDF Annotator"
    with _page_transaction(document, page):
        strike = caret = None
        associated: list[int] = []
        if not isinstance(command, CreateInsertText):
            quads = [pymupdf.Quad(*[(p.x, p.y) for p in quad.points])
                     for quad in command.target.quads]
            strike = page.add_strikeout_annot(quads)
            _info(document, strike, names[-1], text=None,
                  subject="Strikethrough Text", color=_RED, stamp=stamp, author=author)
            associated.append(_popup(document, page, strike))
        if not isinstance(command, CreateDeleteText):
            if isinstance(command.target, TextInsertionTarget):
                top, bottom = command.target.boundary_top, command.target.anchor
            else:
                last = command.target.quads[-1]
                top, bottom = last.upper_right, last.lower_right
            caret = page.add_caret_annot((bottom.x, bottom.y))
            _info(document, caret, names[0], text=command.text,
                  subject="Inserted Text", color=_RED if replacement else _PURPLE, stamp=stamp, author=author)
            associated.append(_popup(document, page, caret))
        if replacement:
            # The direction is significant: the StrikeOut is a group member
            # referring to the Caret root, not the reverse and not a normal reply.
            document.xref_set_key(caret.xref, "IT", "/Replace")
            document.xref_set_key(strike.xref, "IT", "/StrikeOutTextEdit")
            document.xref_set_key(strike.xref, "RT", "/Group")
            strike.set_irt_xref(caret.xref)
            associated.insert(0, strike.xref)
        root = caret if caret is not None else strike
        kind = (AnnotationKind.REPLACE_TEXT if replacement else
                AnnotationKind.INSERT_TEXT if caret is not None else AnnotationKind.DELETE_TEXT)
        if metadata is not None and metadata.reason is not None:
            from .managed_corrections import add_reason, collect_managed, write_manifest
            reply_name = names[0] + ":reason"
            associated.extend(add_reason(document, page, root.xref, reply_name,
                                         metadata.reason, author, stamp))
            write_manifest(document, root.xref, names[0], kind.value,
                           names[1:] if replacement else (), (reply_name,))
            collect_managed(document, command.target.page_index, root.xref)
        # Adding a linked Text note refreshes existing appearances in MuPDF,
        # resetting the Caret to its default size. Set its boundary last.
        if caret is not None:
            _caret_geometry(document, caret, top, bottom)
        model = AnnotationModel(command.annotation_id, command.context.document_id,
                                command.context.document_generation, kind, command.target,
                                getattr(command, "text", None), None,
                                AnnotationOwnership.CREATED_BY_APP, command.context.source,
                                PdfBackingReference(names[0], root.xref, tuple(associated)),
                                metadata=metadata)
        document.reload_page(page)
    return AppliedMutation(annotation=model)


def delete_editorial(document: Any, command: DeleteAnnotation, model: AnnotationModel) -> AppliedMutation:
    """Remove all app-owned components atomically; never touch external replies."""
    if model.metadata is not None and model.metadata.reason is not None:
        from .external_delete import delete_external_annotation
        # The manifest and all incoming relationships are revalidated here,
        # independently of the GUI capability and the remembered backing xrefs.
        from .managed_corrections import collect_managed
        collect_managed(document, command.page_index, model.backing.xref)
        delete_external_annotation(document, f"native:{command.page_index}:{model.backing.xref}")
        return AppliedMutation()
    page = document.load_page(command.page_index)
    names = component_names(command.annotation_id.value,
                            replacement=model.kind == AnnotationKind.REPLACE_TEXT)
    matched = [(xref, name) for xref, _, name in page.annot_xrefs() if name in names]
    if len(matched) != len(names) or {name for _, name in matched} != set(names):
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    root = next(xref for xref, name in matched if name == names[0])
    primary = {xref for xref, _ in matched}
    if model.kind == AnnotationKind.REPLACE_TEXT:
        strike = next(xref for xref, name in matched if name == names[1])
        if (document.xref_get_key(root, "IT") != ("name", "/Replace")
                or document.xref_get_key(strike, "IT") != ("name", "/StrikeOutTextEdit")
                or document.xref_get_key(strike, "RT") != ("name", "/Group")
                or document.xref_get_key(strike, "IRT") != ("xref", f"{root} 0 R")):
            raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
    components = set(primary)
    for xref in primary:
        kind, value = document.xref_get_key(xref, "Popup")
        if kind == "xref":
            popup = int(value.split()[0])
            if document.xref_get_key(popup, "Parent") != ("xref", f"{xref} 0 R"):
                raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            components.add(popup)
    roots = [xref for xref, _, _ in page.annot_xrefs()]
    for xref in set(roots) - components:
        kind, value = document.xref_get_key(xref, "IRT")
        if kind == "xref" and int(value.split()[0]) in components:
            raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
    with _page_transaction(document, page, tuple(components)):
        remaining = "[" + " ".join(f"{xref} 0 R" for xref in roots if xref not in components) + "]"
        document.xref_set_key(page.xref, "Annots", remaining)
        for xref in components:
            document.update_object(xref, "null")
        document.reload_page(page)
    return AppliedMutation()
