"""App-owned PDF FreeText adapter, confined to the document worker process."""

from html import escape
from typing import Any

from ..domain.annotation_commands import (CreateFreeText, DeleteAnnotation, MoveFreeText,
                                           ResizeFreeText, TransferFreeText, UpdateFreeText)
from ..domain.annotation_models import AnnotationModel, AnnotationOwnership
from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.geometry import PdfRect
from ..domain.identifiers import PdfBackingReference
from ..domain.kinds import AnnotationKind
from ..domain.rich_text import RichText, RichTextRun
from ..domain.styles import FreeTextStyle
from ..domain.targets import RectTarget
from .annotation_dispatch import AppliedMutation


def create_free_text(document: Any, command: CreateFreeText) -> AppliedMutation:
    page = document.load_page(command.target.page_index)
    annotation = _add(document, page, command.target.rect, command.rich_text, command.style,
                      command.annotation_id.value, author=command.metadata.author if command.metadata else None)
    model = AnnotationModel(
        command.annotation_id, command.context.document_id, command.context.document_generation,
        AnnotationKind.TEXT_BOX, command.target, command.text, command.style,
        AnnotationOwnership.CREATED_BY_APP, command.context.source,
        PdfBackingReference(command.annotation_id.value, annotation.xref), command.rich_text, command.metadata)
    return AppliedMutation(annotation=model)


def update_free_text(document: Any, command: UpdateFreeText,
                     model: AnnotationModel) -> AppliedMutation:
    return _replace(document, model, model.target.rect, command.rich_text, command.style)


def move_free_text(document: Any, command: MoveFreeText,
                   model: AnnotationModel) -> AppliedMutation:
    rect = _translate_clamped(model.target.rect, document.load_page(command.page_index),
                              command.delta_x, command.delta_y)
    if rect == model.target.rect:
        raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
    return _replace(document, model, rect, _model_rich_text(model), model.style)


def resize_free_text(document: Any, command: ResizeFreeText,
                     model: AnnotationModel) -> AppliedMutation:
    rich_text = _model_rich_text(model)
    return _replace(document, model, command.target.rect, rich_text, model.style)


def transfer_free_text(document: Any, command: TransferFreeText,
                       model: AnnotationModel) -> AppliedMutation:
    if (model.kind != AnnotationKind.TEXT_BOX or not isinstance(model.target, RectTarget)
            or not isinstance(model.style, FreeTextStyle) or model.text is None):
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    rich_text = _model_rich_text(model)
    source_page = document.load_page(command.source_page_index)
    existing = _find(source_page, model.annotation_id.value)
    if existing is None:
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    destination_page = document.load_page(command.target.page_index)
    replacement = None
    try:
        replacement = _add(document, destination_page, command.target.rect, rich_text,
                           model.style, model.annotation_id.value, author=existing.info.get("title") or None)
        source_page.delete_annot(existing)
    except Exception:
        if replacement is not None:
            destination_page.delete_annot(replacement)
        raise
    updated = AnnotationModel(
        model.annotation_id, model.document_id, model.document_generation,
        AnnotationKind.TEXT_BOX, command.target, model.text, model.style,
        model.ownership, model.source,
        PdfBackingReference(model.annotation_id.value, replacement.xref), rich_text, model.metadata)
    return AppliedMutation(annotation=updated)


def delete_free_text(document: Any, command: DeleteAnnotation) -> AppliedMutation:
    page = document.load_page(command.page_index)
    annotation = _find(page, command.annotation_id.value)
    if annotation is None:
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    page.delete_annot(annotation)
    return AppliedMutation()


def _replace(document: Any, model: AnnotationModel, rect: PdfRect, rich_text: RichText,
             style: FreeTextStyle) -> AppliedMutation:
    if (model.kind != AnnotationKind.TEXT_BOX or not isinstance(model.target, RectTarget)
            or not isinstance(model.style, FreeTextStyle) or model.text is None):
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    page = document.load_page(model.page_index)
    existing = _find(page, model.annotation_id.value)
    if existing is None:
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    replacement = None
    try:
        replacement = _add(document, page, rect, rich_text, style, model.annotation_id.value,
                           author=existing.info.get("title") or None)
        page.delete_annot(existing)
        target = RectTarget(model.page_index, rect)
        updated = AnnotationModel(
            model.annotation_id, model.document_id, model.document_generation,
            AnnotationKind.TEXT_BOX, target, rich_text.plain, style, model.ownership, model.source,
            PdfBackingReference(model.annotation_id.value, replacement.xref), rich_text, model.metadata)
        return AppliedMutation(annotation=updated)
    except Exception:
        if replacement is not None:
            page.delete_annot(replacement)
        raise


def _add(document: Any, page: Any, rect: PdfRect, rich_text: RichText, style: FreeTextStyle,
         annotation_id: str, *, callout=None, author: str | None = None) -> Any:
    import pymupdf

    annotation = page.add_freetext_annot(
        # ``add_freetext_annot`` creates the native rich-text FreeText shell
        # that is required by the pinned API for independent fill and border.
        # Its built-in /RC wrapper has a misspelled XHTML namespace, however.
        # Install our fixed, escaped Acrobat-compatible /RC before the final
        # appearance update so both Acrobat editing and the generated /AP use
        # the same semantic rich text.
        (rect.x0, rect.y0, rect.x1, rect.y1), "", richtext=True,
        style="margin:0;", fill_color=_color(style.fill), border_color=_color(style.border),
        border_width=style.border_width, opacity=style.opacity,
        **({"callout": callout, "line_end": pymupdf.PDF_ANNOT_LE_OPEN_ARROW} if callout else {}))
    if author is not None:
        try:
            annotation.set_info(title=author)
        except Exception:
            page.delete_annot(annotation)
            raise
    # In an Acrobat-authored rich FreeText, /C is the fill and /DA supplies the
    # fallback/border color; actual rich text color belongs to /RC. Keeping
    # that split prevents a colored first run from changing the border.
    default = rich_text.runs[0]
    _family, resource = _pdf_font(style.font_family)
    da = f"{_pdf_rgb(style.border)} rg /{resource} {style.font_size:g} Tf"
    document.xref_set_key(annotation.xref, "RC", pymupdf.get_pdf_str(_rich_markup(rich_text)))
    document.xref_set_key(annotation.xref, "DS", pymupdf.get_pdf_str(_default_style(style)))
    document.xref_set_key(annotation.xref, "DA", pymupdf.get_pdf_str(da))
    if callout is None:
        document.xref_set_key(annotation.xref, "CL", "null")
    document.xref_set_key(annotation.xref, "NM", pymupdf.get_pdf_str(annotation_id))
    annotation.update(opacity=style.opacity, text_color=_color(style.border),
                      fill_color=_color(style.fill))
    document.xref_set_key(annotation.xref, "Contents", pymupdf.get_pdf_str(rich_text.plain))
    return annotation


def _find(page: Any, annotation_id: str) -> Any | None:
    for annotation in page.annots() or ():
        info = annotation.info or {}
        if annotation.type[1] == "FreeText" and info.get("id") == annotation_id:
            return annotation
    return None


def _rich_markup(rich_text: RichText) -> str:
    """Generate the only Acrobat-compatible XHTML accepted by the worker.

    User input never becomes markup. All text is escaped before its small,
    deterministic ``/RC`` representation is assembled.
    """
    first = rich_text.runs[0]
    body_style = _run_css(first) + "text-align:left;font-stretch:normal;"
    return ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" '
            'xfa:APIVersion="Acrobat:26.1.0" xfa:spec="2.0.2" '
            f'style="{body_style}"><p dir="ltr">' + "".join(
        f'<span style="{_run_css(run)}">'
        # XML parsers normalize a literal CR to LF. Encode every app-visible
        # paragraph separator so a canonical /RC round-trip compares exactly
        # to /Contents (including imported Acrobat CR separators).
        + escape(run.text, quote=True).replace("\r\n", "&#13;").replace("\r", "&#13;")
        .replace("\n", "&#13;") + "</span>"
        for run in rich_text.runs) + "</p></body>")


def _run_css(run: RichTextRun) -> str:
    family = _acrobat_css_family(run.font_family)
    return (f"font-family:{family};font-size:{run.font_size:g}pt;color:{_css_rgb(run.color)};"
            f"font-weight:{'bold' if run.bold else 'normal'};"
            f"font-style:{'italic' if run.italic else 'normal'};")


def _default_style(style: FreeTextStyle) -> str:
    """Match the semantic /DS shape used by Acrobat-authored FreeText."""
    family = _acrobat_css_family(style.font_family)
    return (f"font: {family},sans-serif {style.font_size:.1f}pt; "
            f"text-align:left; color:{_css_rgb(style.border)} ")


def _color(color) -> tuple[float, float, float] | None:
    return None if color is None else (color.red, color.green, color.blue)


def _css_rgb(color) -> str:
    return "#%02x%02x%02x" % tuple(round(value * 255) for value in (color.red, color.green, color.blue))


def _pdf_rgb(color) -> str:
    return " ".join(f"{value:g}" for value in (color.red, color.green, color.blue))


def _pdf_font(family: str) -> tuple[str, str]:
    return {"Helvetica": ("Helvetica", "Helv"), "Times": ("Times-Roman", "TiRo"),
            "Courier": ("Courier", "Cour")}[family]


def _acrobat_css_family(family: str) -> str:
    """Return Acrobat's editable-XHTML family, not the PDF resource name.

    Acrobat's own FreeText profile uses ``/TiRo`` and ``/CourierStd`` in
    ``/AP``, but serializes their editor state as ``Times`` and ``'Courier
    Std'`` in ``/RC``. Supplying PostScript names such as ``Times-Roman`` in
    ``/RC`` makes Acrobat substitute a different editor font on activation.
    """
    return {"Helvetica": "Helvetica", "Times": "Times",
            "Courier": "'Courier Std'"}[family]


def _model_rich_text(model: AnnotationModel) -> RichText:
    if model.rich_text is not None:
        return model.rich_text
    if not isinstance(model.style, FreeTextStyle) or model.text is None:
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    return RichText.plain_text(model.text, font_family=model.style.font_family,
                               font_size=model.style.font_size, color=model.style.text)


def _translate_clamped(rect: PdfRect, page: Any, delta_x: float, delta_y: float) -> PdfRect:
    width, height = page.rect.width, page.rect.height
    if page.rotation in (90, 270):
        width, height = height, width
    dx = min(width - rect.x1, max(-rect.x0, float(delta_x)))
    dy = min(height - rect.y1, max(-rect.y0, float(delta_y)))
    return PdfRect(rect.x0 + dx, rect.y0 + dy, rect.x1 + dx, rect.y1 + dy)
