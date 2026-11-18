"""M15.1 isolated fixture/inspection and pre-gate candidate test worker.

This is a test seam, not a product release switch or automation interface.
"""

import json
from pathlib import Path
import sys

from tests.m151_fixtures import BOX, BOX_PAGE, CALLOUT, CALLOUT_PAGE, annotation_xref


def candidate_worker(conn, document_id, source):
    from pdf_annotator.worker import external_profiles
    from pdf_annotator.domain.external_edits import ExternalEditKind
    from pdf_annotator.worker.document_service import run_document_worker
    external_profiles.VERIFIED_EXTERNAL_EDITS = frozenset(
        (profile, edit.value) for profile in ("freetext-translation-v1", "freetext-rich-v1",
            "freetext-plain-v1", "callout-rich-v1", "ink-translation-v1") for edit in ExternalEditKind)
    run_document_worker(conn, document_id, source)


def fail_once_external_adoption_worker(conn, document_id, source):
    """Exercise rollback after a native write but before worker acknowledgement."""
    from pdf_annotator.worker import external_profiles
    from pdf_annotator.domain.external_edits import ExternalEditKind
    from pdf_annotator.worker.annotation_inventory import NativeAnnotationInventory
    from pdf_annotator.worker.document_service import run_document_worker

    external_profiles.VERIFIED_EXTERNAL_EDITS = frozenset(
        (profile, edit.value) for profile in ("freetext-translation-v1", "freetext-rich-v1",
            "freetext-plain-v1", "callout-rich-v1", "ink-translation-v1") for edit in ExternalEditKind)
    original = NativeAnnotationInventory.summary_for_edit
    calls = 0

    def fail_second_summary(self, logical_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced post-write inventory failure")
        return original(self, logical_id)

    NativeAnnotationInventory.summary_for_edit = fail_second_summary
    run_document_worker(conn, document_id, source)


def no_rescan_worker(conn, document_id, source):
    from pdf_annotator.worker.annotation_inventory import NativeAnnotationInventory
    original = NativeAnnotationInventory._scan
    seen = []
    def scan(self):
        if any(document is self._document for document in seen):
            raise RuntimeError("Unexpected full inventory rescan after external edit")
        seen.append(self._document)
        return original(self)
    NativeAnnotationInventory._scan = scan
    candidate_worker(conn, document_id, source)


def shape_rejection_worker(conn, document_id, source):
    """Force the strict AP guard through the real worker rollback boundary."""
    from pdf_annotator.worker import canonical_text_ap, external_edit_adapter
    original = external_edit_adapter.patch_native_freetext_appearance

    def reject(*_args, **_kwargs):
        raise canonical_text_ap.CanonicalTextPatchError("unsupported generated AP shape")

    external_edit_adapter.patch_native_freetext_appearance = reject
    try:
        candidate_worker(conn, document_id, source)
    finally:
        external_edit_adapter.patch_native_freetext_appearance = original


def callout_layout_failure_worker(conn, document_id, source):
    """Force Callout resize failure after PyMuPDF has made its fresh shell."""
    from pdf_annotator.worker import canonical_text_ap
    original = canonical_text_ap._advance

    def reject(*_args, **_kwargs):
        raise canonical_text_ap.CanonicalTextPatchError("forced Callout child-layout failure")

    canonical_text_ap._advance = reject
    try:
        candidate_worker(conn, document_id, source)
    finally:
        canonical_text_ap._advance = original


def fixture(path):
    import pymupdf
    from pdf_annotator.worker.freetext_adapter import _add
    from pdf_annotator.domain.geometry import PdfRect
    from pdf_annotator.domain.rich_text import RichText, RichTextRun
    from pdf_annotator.domain.styles import FreeTextStyle, RgbColor
    doc = pymupdf.open()
    style = FreeTextStyle(RgbColor(.8, .1, .2), RgbColor(1, 1, .8), font_size=10)
    small_style = FreeTextStyle(RgbColor(.8, .1, .2), RgbColor(1, 1, .8), font_size=6)
    rich = RichText((RichTextRun("Native ", "Helvetica", 10, RgbColor(0, 0, 0)),
                     RichTextRun("rich", "Courier", 10, RgbColor(0, 0, .8), True, True)))
    for rotation in (0, 90, 180, 270):
        page = doc.new_page(width=650, height=850)
        page.set_cropbox(pymupdf.Rect(20, 30, 620, 830))
        page.insert_text((40, 750), "Base page content must remain untouched")
        plain = page.add_freetext_annot((40, 40, 270, 120), "Plain external", fontsize=10, border_width=1)
        doc.xref_set_key(plain.xref, "NM", pymupdf.get_pdf_str("plain"))
        no_border = page.add_freetext_annot((300, 650, 520, 710), "No border external",
                                             fontsize=10, fill_color=(1, 1, .8), border_width=0)
        no_border.update(fill_color=(1, 1, .8))
        doc.xref_set_key(no_border.xref, "NM", pymupdf.get_pdf_str("no-border"))
        plain_full = _add(doc, page, PdfRect(40, 255, 270, 300),
                          RichText.plain_text("Plain full", font_family="Helvetica", font_size=10,
                                              color=RgbColor(0, 0, 0)), style, "plain-full")
        # Native plain FreeText: no /RC or /DS, with a simple Base-14 normal
        # appearance that remains fully representable by the profile.
        doc.xref_set_key(plain_full.xref, "RC", "null")
        doc.xref_set_key(plain_full.xref, "DS", "null")
        _add(doc, page, PdfRect(40, 160, 270, 240), rich, style, "rich")
        unknown_font = _add(doc, page, PdfRect(310, 40, 560, 120), rich, style, "font")
        rc = doc.xref_get_key(unknown_font.xref, "RC")[1].replace("Helvetica", "Myriad Pro")
        doc.xref_set_key(unknown_font.xref, "RC", pymupdf.get_pdf_str(rc))
        single_font = _add(doc, page, PdfRect(310, 125, 560, 150),
                           RichText.plain_text("Single imported family", font_family="Helvetica", font_size=10,
                                               color=RgbColor(0, 0, 0)), style, "single-font")
        rc = doc.xref_get_key(single_font.xref, "RC")[1].replace("Helvetica", "Myriad Pro")
        doc.xref_set_key(single_font.xref, "RC", pymupdf.get_pdf_str(rc))
        multi = RichText((RichTextRun("Dominant family text ", "Helvetica", 10, RgbColor(0, 0, 0)),
                          RichTextRun("B", "Courier", 11, RgbColor(.1, .2, .8), True),
                          RichTextRun(" C", "Times", 9, RgbColor(.8, .1, .2), False, True)))
        multi_font = _add(doc, page, PdfRect(310, 250, 560, 280), multi, style, "multi-font")
        rc = doc.xref_get_key(multi_font.xref, "RC")[1]
        rc = rc.replace("Helvetica", "Myriad Pro").replace("'Courier Std'", "Consolas").replace("Times", "Fira Code")
        doc.xref_set_key(multi_font.xref, "RC", pymupdf.get_pdf_str(rc))
        callout = _add(doc, page, PdfRect(310, 300, 560, 400), rich, style, "callout",
                       callout=((220, 480), (288, 341), (310, 350)))
        _add(doc, page, PdfRect(150, 430, 172, 452),
             RichText.plain_text("OK", font_family="Helvetica", font_size=6,
                                 color=RgbColor(0, 0, 0)), small_style, "callout-small",
             callout=((100, 500), (132, 441), (150, 441)))
        ink = page.add_ink_annot([[(50, 510), (90, 545), (170, 520)], [(45, 560), (80, 570)]])
        ink.set_colors(stroke=(.1, .3, .8))
        ink.set_border(width=2.5)
        ink.update(opacity=.65)
        doc.xref_set_key(ink.xref, "NM", pymupdf.get_pdf_str("ink"))
        ink.set_popup(pymupdf.Rect(400, 600, 590, 740))
        reply = page.add_text_annot((200, 530), "Reply preserved")
        reply.set_irt_xref(ink.xref)
        unsupported = _add(doc, page, PdfRect(310, 160, 560, 240), rich, style, "unknown")
        doc.xref_set_key(unsupported.xref, "PrivateData", "<</Version 7 /Payload (preserve me)>>")
        for annot in page.annots():
            annot.set_info(title="External Author", creationDate="D:20200101000000Z",
                           modDate="D:20200202000000Z", subject="Metadata")
            annot.set_flags(4)
        page.set_rotation(rotation)
    doc.save(path)
    doc.close()


def inspect(path):
    import pymupdf
    from hashlib import sha256
    from pdf_annotator.worker.external_profiles import inspect_profile
    from pdf_annotator.worker.external_edit_adapter import NativeCoordinates, text_box
    doc = pymupdf.open(path)
    objects = {str(xref): {"source": doc.xref_object(xref),
        "stream": sha256(doc.xref_stream(xref)).hexdigest() if doc.xref_is_stream(xref) else None}
        for xref in range(1, doc.xref_length())}
    rows = []

    def appearance_font_identity(xref):
        """Small semantic AP probe used only by the subprocess test helper."""
        try:
            ap = doc.xref_get_key(xref, "AP")[1]
            normal = int(ap.split("/N", 1)[1].split("0 R", 1)[0].strip())
            form = doc.xref_object(normal)
            parent_stream = doc.xref_stream(normal).decode("ascii")
            resources = form.split("/Font", 1)[1].split(">>", 1)[0]
            refs = []
            import re
            for name, font_xref in re.findall(r"/(\S+)\s+(\d+)\s+0\s+R", resources):
                refs.append((name, int(font_xref), doc.xref_object(int(font_xref))))
            result = {"normal": normal, "fonts": refs,
                      "stream": sha256(parent_stream.encode("ascii")).hexdigest(),
                      "bbox": doc.xref_get_key(normal, "BBox")[1],
                      "matrix": doc.xref_get_key(normal, "Matrix")[1],
                      "resources": doc.xref_get_key(normal, "Resources")[1],
                      "paint": {
                          "stroke": bool(re.search(r"(?m)^\s*(?:B|B\*|S|s|b|b\*)\s*$", parent_stream)),
                          "widths": re.findall(r"([+-]?(?:\d+\.?\d*|\.\d+))\s+w", parent_stream),
                          "stroke_colors": re.findall(
                              r"([+-]?(?:\d+\.?\d*|\.\d+))\s+([+-]?(?:\d+\.?\d*|\.\d+))\s+"
                              r"([+-]?(?:\d+\.?\d*|\.\d+))\s+RG", parent_stream),
                      }}
            child_match = re.search(r"/CanonicalText\s+(\d+)\s+0\s+R", form)
            if child_match:
                child_xref = int(child_match.group(1))
                child = doc.xref_object(child_xref)
                child_resources = doc.xref_get_key(child_xref, "Resources")[1]
                child_fonts = []
                for name, font_xref in re.findall(r"/(F(?:Source|Courier)\S*)\s+(\d+)\s+0\s+R", child_resources):
                    font = doc.xref_object(int(font_xref))
                    descendant = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", font)
                    descendant_object = (doc.xref_object(int(descendant.group(1))) if descendant else "")
                    descriptor = re.search(r"/FontDescriptor\s+(\d+)\s+0\s+R", descendant_object)
                    descriptor_object = (doc.xref_object(int(descriptor.group(1))) if descriptor else "")
                    child_fonts.append((name, font, descendant_object, descriptor_object))
                result["canonical_text"] = {
                    "xref": child_xref, "form": child,
                    "stream": doc.xref_stream(child_xref).decode("ascii"),
                    "resources": child_resources, "fonts": child_fonts,
                }
            return result
        except (IndexError, ValueError, RuntimeError):
            return None

    for page in doc:
        for annot in page.annots() or ():
            profile = inspect_profile(doc, page, annot)
            data = {"page": page.number, "xref": annot.xref, "nm": annot.info.get("id"),
                    "subtype": annot.type[1], "keys": {key: doc.xref_get_key(annot.xref, key)
                    for key in doc.xref_get_keys(annot.xref)}, "rect": list(annot.rect),
                    "vertices": annot.vertices, "profile": profile.name,
                    "reason": profile.reason,
                    "candidate": {key: getattr(profile.candidate, key)
                                  for key in profile.candidate.__dataclass_fields__},
                    "native_paint": {"colors": annot.colors, "border": annot.border,
                                     "opacity": annot.opacity}}
            appearance = appearance_font_identity(annot.xref)
            if appearance is not None:
                data["appearance"] = appearance
            if annot.type[1] == "FreeText" and min(annot.rect) >= 0:
                box = text_box(doc, annot, NativeCoordinates.observe(doc, annot))
                data["box"] = [box.x0, box.y0, box.x1, box.y1]
            if profile.rich_text:
                from pdf_annotator.domain.serialization import rich_text_to_payload, style_to_payload
                data["rich_text"] = rich_text_to_payload(profile.rich_text)
                data["edit_style"] = style_to_payload(profile.style)
            rows.append(data)
    doc.close()
    return {"objects": objects, "annotations": rows}


def trace_imported_box(path):
    """Capture the five required normalization stages via the production adapter."""
    import pymupdf
    from pdf_annotator.domain.annotation_commands import CommandContext
    from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
    from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
    from pdf_annotator.domain.geometry import PdfRect
    from pdf_annotator.worker import external_profiles
    from pdf_annotator.worker.external_edit_adapter import (NativeCoordinates, apply_external_edit,
                                                            text_box)
    doc = pymupdf.open(path)
    try:
        external_profiles.VERIFIED_EXTERNAL_EDITS = frozenset(
            ("freetext-rich-v1", operation.value) for operation in ExternalEditKind)
        page = doc.load_page(BOX_PAGE)
        root = annotation_xref(page, BOX)
        logical_id = f"native:{BOX_PAGE}:{root}"
        context = CommandContext.create("imported-box-style-trace", 1)
        move = EditExternalAnnotation(context, external_command_annotation_id(logical_id), logical_id,
                                      BOX_PAGE, ExternalEditKind.MOVE, delta=(8, 6))
        apply_external_edit(doc, move)
        page = doc.load_page(BOX_PAGE)
        annot = page.load_annot(root)
        transform = NativeCoordinates.observe(doc, annot)
        box = text_box(doc, annot, transform)
        resize = EditExternalAnnotation(CommandContext.create("imported-box-style-trace", 1),
                                        external_command_annotation_id(logical_id), logical_id, BOX_PAGE,
                                        ExternalEditKind.RESIZE,
                                        rect=PdfRect(box.x0, box.y0, box.x1 + 12, box.y1 + 8))
        trace = []
        apply_external_edit(doc, resize, normalization_trace=trace)
        return trace
    finally:
        doc.close()


def trace_imported_callout(path):
    """Run each synthetic imported Callout operation in an isolated document and retain failure evidence.

    This deliberately calls the production adapter, not a bespoke PDF mutation
    path.  Its JSON is diagnostic-only and avoids returning annotation text.
    """
    import pymupdf
    from hashlib import sha256
    import re
    import traceback
    from pdf_annotator.domain.annotation_commands import CommandContext
    from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
    from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
    from pdf_annotator.domain.geometry import PdfPoint, PdfRect
    from pdf_annotator.worker import external_profiles
    from pdf_annotator.worker.external_edit_adapter import (_cl, NativeCoordinates, apply_external_edit,
                                                            text_box)

    page_index = CALLOUT_PAGE

    def parent_state(document):
        ap_kind, ap_value = document.xref_get_key(root, "AP")
        match = re.search(r"/N\s+(\d+)\s+0\s+R", ap_value) if ap_kind == "dict" else None
        result = {"normal": int(match.group(1)) if match else None}
        if match:
            normal = int(match.group(1))
            stream = document.xref_stream(normal).decode("latin1")
            result.update(
                bbox=document.xref_get_key(normal, "BBox")[1],
                matrix=document.xref_get_key(normal, "Matrix")[1],
                resources=document.xref_get_key(normal, "Resources")[1],
                stream_sha256=sha256(stream.encode("latin1")).hexdigest(),
                canonical_child="/CanonicalText" in document.xref_object(normal),
                # Conservative shape evidence: this is intentionally not a PDF parser.
                operator_counts={operator: len(re.findall(pattern, stream)) for operator, pattern in {
                    "BT": r"(?m)^BT$", "ET": r"(?m)^ET$", "Tj": r"(?m)^.*\sTj$",
                    "TJ": r"(?m)^.*\sTJ$", "Td": r"(?m)^.*\sTd$", "re": r"(?m)^.*\sre$",
                    "stroke": r"(?m)^(?:S|s|B|B\*|b|b\*)$",
                }.items()},
            )
        return result

    def native_state(document):
        return {
            "Rect": document.xref_get_key(root, "Rect")[1],
            "RD": document.xref_get_key(root, "RD")[1],
            "CL": document.xref_get_key(root, "CL")[1],
            "root_sha256": sha256(document.xref_object(root).encode("latin1")).hexdigest(),
            "parent": parent_state(document),
        }

    def command_for(document, edit):
        page = document.load_page(page_index)
        annot = page.load_annot(root)
        profile = external_profiles.inspect_profile(document, page, annot)
        context = CommandContext.create("imported-callout-trace", 1)
        identity = external_command_annotation_id(logical_id)
        if edit == ExternalEditKind.MOVE:
            kwargs = {"delta": (8, 6)}
        elif edit == ExternalEditKind.ANCHOR:
            line = _cl(document, root, NativeCoordinates.observe(document, annot))
            kwargs = {"anchor": PdfPoint(line[0].x + 8, line[0].y + 6)}
        elif edit == ExternalEditKind.RESIZE:
            box = text_box(document, annot, NativeCoordinates.observe(document, annot))
            kwargs = {"rect": PdfRect(box.x0, box.y0, box.x1 + 12, box.y1 + 8)}
        elif edit == ExternalEditKind.TEXT:
            # Confirming the complete selection is itself the first layout edit.
            # Keeping it byte-for-byte semantically equal isolates AP conversion.
            kwargs = {"rich_text": profile.rich_text}
        else:
            kwargs = {"style": profile.style}
        return EditExternalAnnotation(context, identity, logical_id, page_index, edit, **kwargs)

    results = []
    for edit in ExternalEditKind:
        document = pymupdf.open(path)
        external_profiles.VERIFIED_EXTERNAL_EDITS = frozenset(
            ("callout-rich-v1", operation.value) for operation in ExternalEditKind)
        root = annotation_xref(document[page_index], CALLOUT)
        logical_id = f"native:{page_index}:{root}"
        before = native_state(document)
        trace = []
        try:
            command = command_for(document, edit)
            apply_external_edit(document, command, normalization_trace=trace)
        except Exception as error:
            after = native_state(document)
            results.append({
                "command_type": edit.value, "page_index": page_index, "xref": root,
                "normalization_requested": edit in (ExternalEditKind.RESIZE, ExternalEditKind.TEXT,
                                                      ExternalEditKind.STYLE),
                "before": before, "after": after, "normalization_trace": trace,
                "annot_update_ran": any(item["stage"] == "D_after_annot_update" for item in trace),
                "canonical_text_ap_patch_ran": any(item["stage"] == "E_after_canonical_patch" for item in trace),
                "exception_type": type(error).__name__, "exception": str(error),
                "traceback": traceback.format_exc(),
                "rollback": {
                    "root_restored": after["root_sha256"] == before["root_sha256"],
                    "geometry_restored": all(after[key] == before[key] for key in ("Rect", "RD", "CL")),
                    "canonical_child_reachable": after["parent"]["canonical_child"],
                },
            })
        else:
            after = native_state(document)
            results.append({
                "command_type": edit.value, "page_index": page_index, "xref": root,
                "normalization_requested": edit in (ExternalEditKind.RESIZE, ExternalEditKind.TEXT,
                                                      ExternalEditKind.STYLE),
                "before": before, "after": after, "normalization_trace": trace,
                "annot_update_ran": any(item["stage"] == "D_after_annot_update" for item in trace),
                "canonical_text_ap_patch_ran": any(item["stage"] == "E_after_canonical_patch" for item in trace),
                "exception_type": None, "exception": None, "traceback": None,
            })
        finally:
            document.close()
    return results


def trace_imported_callout_repeated_resize(path):
    """Capture canonical rich/style state across two imported Callout resizes."""
    import pymupdf
    import re
    from pdf_annotator.domain.annotation_commands import CommandContext
    from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
    from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
    from pdf_annotator.domain.geometry import PdfRect
    from pdf_annotator.domain.serialization import rich_text_to_payload, style_to_payload
    from pdf_annotator.worker import external_profiles
    from pdf_annotator.worker.external_edit_adapter import NativeCoordinates, apply_external_edit, text_box

    page_index = CALLOUT_PAGE

    def state(document):
        page = document.load_page(page_index)
        annot = page.load_annot(root)
        profile = external_profiles.inspect_profile(document, page, annot)
        ap_kind, ap = document.xref_get_key(root, "AP")
        normal = (int(match.group(1)) if ap_kind == "dict"
                  and (match := re.search(r"/N\s+(\d+)\s+0\s+R", ap)) else None)
        parent = document.xref_object(normal) if normal is not None else ""
        return {
            "keys": {key: document.xref_get_key(root, key) for key in
                     ("Contents", "RC", "DA", "DS", "C", "BS", "CA", "Rect", "RD", "CL")},
            "profile": profile.name, "reason": profile.reason,
            "rich_text": rich_text_to_payload(profile.rich_text) if profile.rich_text else None,
            "edit_style": style_to_payload(profile.style) if profile.style else None,
            "parent_normal": normal, "canonical_child": "/CanonicalText" in parent,
            "parent_bbox": document.xref_get_key(normal, "BBox")[1] if normal else None,
            "parent_matrix": document.xref_get_key(normal, "Matrix")[1] if normal else None,
            "parent_resources": document.xref_get_key(normal, "Resources")[1] if normal else None,
        }

    def resize(document, dx, dy):
        page = document.load_page(page_index)
        annot = page.load_annot(root)
        box = text_box(document, annot, NativeCoordinates.observe(document, annot))
        command = EditExternalAnnotation(
            CommandContext.create("imported-callout-repeat-trace", 1), external_command_annotation_id(logical_id),
            logical_id, page_index, ExternalEditKind.RESIZE,
            rect=PdfRect(box.x0, box.y0, box.x1 + dx, box.y1 + dy))
        trace = []
        apply_external_edit(document, command, normalization_trace=trace)
        return trace

    document = pymupdf.open(path)
    try:
        external_profiles.VERIFIED_EXTERNAL_EDITS = frozenset(
            ("callout-rich-v1", operation.value) for operation in ExternalEditKind)
        root = annotation_xref(document[page_index], CALLOUT)
        logical_id = f"native:{page_index}:{root}"
        native = state(document)
        first_trace = resize(document, 12, 8)
        first = state(document)
        second_trace = resize(document, 56, 32)
        second = state(document)
        return {"native": native, "first_trace": first_trace, "first": first,
                "second_trace": second_trace, "second": second}
    finally:
        document.close()


def trace_canonical_word_wrap():
    """Exercise the production child Form compositor in a PyMuPDF subprocess."""
    import pymupdf
    import re
    from pdf_annotator.domain.rich_text import RichText, RichTextRun
    from pdf_annotator.domain.styles import RgbColor
    from pdf_annotator.worker.canonical_text_ap import patch_native_freetext_appearance

    black = RgbColor(0, 0, 0)
    prose = RichText((
        RichTextRun("Controlla la map", "Helvetica", 10, black, True, False),
        RichTextRun("pa. Il registro del museo elenca i campioni esposti e indica il percorso per ogni sala.",
                    "Helvetica", 10, black, False, False),
    ))
    long_token = "render_invoice_with_a_very_long_identifier()"

    def layout(rich, width):
        document = pymupdf.open()
        try:
            page = document.new_page()
            annot = page.add_freetext_annot((50, 50, 50 + width, 280), "shell", richtext=True,
                                             style="margin:0;", border_width=1)
            annot.update()
            patch_native_freetext_appearance(document, page, annot.xref, rich)
            normal = int(re.search(r"/N\s+(\d+)\s+0\s+R", document.xref_get_key(annot.xref, "AP")[1]).group(1))
            child_xref = int(re.search(r"/CanonicalText\s+(\d+)\s+0\s+R",
                                       document.xref_object(normal)).group(1))
            child = document.xref_object(child_xref)
            stream = document.xref_stream(child_xref).decode("ascii")
            resources = {name: int(xref) for name, xref in
                         re.findall(r"/(\w+)\s+(\d+)\s+0\s+R", document.xref_get_key(child_xref, "Resources")[1])}
            characters = {character for run in rich.runs for character in run.text if character not in "\r\n"}
            reverse = {}
            for resource, xref in resources.items():
                widths = document.get_char_widths(xref, max(map(ord, characters), default=0) + 1)
                reverse[resource] = {glyph: character for character in characters
                                     if (glyph := widths[ord(character)][0]) > 0}

            runs = []
            expression = (r"/(\w+)\s+[^\s]+\s+Tf\s+1\s+0\s+0\s+-1\s+([^\s]+)\s+([^\s]+)\s+Tm"
                          r"\s+<([0-9A-F]+)>\s+Tj")
            for resource, x, y, encoded in re.findall(expression, stream):
                text = "".join(reverse[resource][int(encoded[index:index + 4], 16)]
                               for index in range(0, len(encoded), 4))
                runs.append({"resource": resource, "x": float(x), "y": float(y), "text": text})
            lines = {}
            for run in runs:
                lines.setdefault(run["y"], []).append(run)
            return {"clip_width": width, "stream": stream, "runs": runs,
                    "lines": ["".join(run["text"] for run in line) for _y, line in sorted(lines.items())]}
        finally:
            document.close()

    return {"prose": [layout(prose, width) for width in (250, 140, 90)],
            "long_token": layout(RichText.plain_text(long_token, font_family="Helvetica", font_size=10,
                                                       color=black), 80),
            "text": prose.plain}


def patch_guard(path):
    """Subprocess-only structural guard probe; no app process imports PyMuPDF."""
    import pymupdf
    from pdf_annotator.domain.rich_text import RichText
    from pdf_annotator.domain.styles import RgbColor
    from pdf_annotator.worker.canonical_text_ap import CanonicalTextPatchError, patch_native_freetext_appearance

    doc = pymupdf.open()
    page = doc.new_page()
    annotation = page.add_freetext_annot((50, 50, 250, 120), "guard", richtext=True,
                                         style="margin:0;", border_width=1)
    annotation.update()
    root_before = doc.xref_object(annotation.xref)
    normal = int(doc.xref_get_key(annotation.xref, "AP")[1].split()[1])
    parent_before = doc.xref_object(normal)
    stream = doc.xref_stream(normal)
    doc.update_stream(normal, stream.replace(b"BT", b"BT\nunsupported", 1))
    unsupported_parent = doc.xref_object(normal)
    try:
        patch_native_freetext_appearance(
            doc, page, annotation.xref,
            RichText.plain_text("guard", font_family="Helvetica", font_size=10, color=RgbColor(0, 0, 0)))
    except CanonicalTextPatchError:
        rejected = True
    else:
        rejected = False
    result = {"rejected": rejected, "root_unchanged": doc.xref_object(annotation.xref) == root_before,
              "parent_not_heuristically_changed": doc.xref_object(normal) == unsupported_parent,
              "parent_was_mutated_only_for_fixture": parent_before != unsupported_parent}
    doc.save(path)
    doc.close()
    return result


if __name__ == "__main__":
    if sys.argv[1] == "fixture":
        fixture(sys.argv[2])
    elif sys.argv[1] == "inspect":
        print(json.dumps(inspect(sys.argv[2]), ensure_ascii=True))
    elif sys.argv[1] == "patch_guard":
        print(json.dumps(patch_guard(sys.argv[2]), ensure_ascii=True))
    elif sys.argv[1] == "trace_imported_box":
        print(json.dumps(trace_imported_box(sys.argv[2]), ensure_ascii=True))
    elif sys.argv[1] == "trace_imported_callout":
        print(json.dumps(trace_imported_callout(sys.argv[2]), ensure_ascii=True))
    elif sys.argv[1] == "trace_imported_callout_repeated_resize":
        print(json.dumps(trace_imported_callout_repeated_resize(sys.argv[2]), ensure_ascii=True))
    elif sys.argv[1] == "trace_canonical_word_wrap":
        print(json.dumps(trace_canonical_word_wrap(), ensure_ascii=True))
