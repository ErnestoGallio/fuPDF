"""Read-only verification after the live worker has closed the saved PDF."""

from collections import Counter
from hashlib import sha256
from math import dist, isclose
from pathlib import Path
from typing import Any

import pymupdf

from pdf_annotator.domain.serialization import target_from_payload
from pdf_annotator.domain.targets import RectTarget, TextInsertionTarget, TextRangeTarget
from pdf_annotator.worker.managed_corrections import collect_managed
from pdf_annotator.worker.save_validation import base_page_text, inspect_document


class VerificationFailure(ValueError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise VerificationFailure(code)


def base_state(document: pymupdf.Document) -> tuple[str, ...]:
    states = []
    for page in document:
        digest = sha256()
        digest.update(repr((tuple(page.rect), tuple(page.cropbox), tuple(page.mediabox), page.rotation)).encode())
        digest.update(base_page_text(page).encode("utf-8"))
        for xref in page.get_contents():
            digest.update(sha256(document.xref_stream(xref)).digest())
        states.append(digest.hexdigest())
    return tuple(states)


def annotation_state(document: pymupdf.Document) -> dict[tuple[int, int], str]:
    return {(page.number, xref): document.xref_object(xref)
            for page in document for xref, _kind, _name in page.annot_xrefs()}


def close_points(actual, expected) -> bool:
    return len(actual) == len(expected) and all(dist(a, b) <= .005 for a, b in zip(actual, expected))


def note_popup_inside(document, note, bounds) -> bool:
    # Popup.rect has different rotation semantics in MuPDF. Derive the
    # page-local translation and scale from the note's matching native Rect.
    kind, raw_note = document.xref_get_key(note.xref, "Rect")
    popup_kind, raw_popup = document.xref_get_key(note.popup_xref, "Rect")
    if kind != "array" or popup_kind != "array":
        return False
    x0, y0, x1, y1 = map(float, raw_note.strip("[]").split())
    px0, py0, px1, py1 = map(float, raw_popup.strip("[]").split())
    rect = note.rect
    sx, sy = rect.width / (x1 - x0), rect.height / (y1 - y0)
    popup = pymupdf.Rect(rect.x0 + (px0 - x0) * sx, rect.y0 + (y1 - py1) * sy,
                         rect.x0 + (px1 - x0) * sx, rect.y0 + (y1 - py0) * sy)
    return not popup.is_empty and bounds.contains(popup)


def verify_one(document: pymupdf.Document, row: dict, proposal: dict) -> tuple[int, ...]:
    page = document[row["page_index"]]
    root = row["root_xref"]
    require(document.xref_get_key(root, "NM") == ("string", row["pdf_nm"]), "wrong_annotation_identity")
    target = target_from_payload(row["target"])
    actual = page.load_annot(root)
    require(actual is not None and target.page_index == page.number, "wrong_annotation_page")
    operation = proposal["operation"]
    if operation == "comment":
        require(isinstance(target, RectTarget), "invalid_note_target")
        rect = target.rect
        bounds = page.rect * page.derotation_matrix + (-.005, -.005, .005, .005)
        require(actual.type[1] == "Text" and actual.info["content"] == proposal["reason"]
                and document.xref_get_key(root, "Name") == ("name", "/Note")
                and document.xref_get_key(root, "IRT")[0] == "null"
                and document.xref_get_key(root, "RT")[0] == "null"
                and close_points((actual.rect.tl, actual.rect.br), ((rect.x0, rect.y0), (rect.x1, rect.y1)))
                and bounds.contains(actual.rect) and note_popup_inside(document, actual, bounds),
                "note_structure_or_geometry_mismatch")
        components = (root, actual.popup_xref)
    else:
        group = collect_managed(document, page.number, root)
        require(group.kind == operation + "_text" and len(group.replies) == 1
                and document.xref_get_key(group.replies[0], "Contents") == ("string", proposal["reason"])
                and document.xref_get_key(group.replies[0], "RT") == ("name", "/R"), "editorial_reason_mismatch")
        if isinstance(target, TextRangeTarget):
            strike = page.load_annot(group.auxiliaries[0]) if operation == "replace" else actual
            points = [(p.x, p.y) for quad in target.quads for p in quad.points]
            require(close_points(strike.vertices, points), "strikeout_geometry_mismatch")
        if operation in ("insert", "replace"):
            require(actual.info["content"] == proposal["replacement_text"], "editorial_payload_mismatch")
            if isinstance(target, TextInsertionTarget):
                top, bottom = target.boundary_top, target.anchor
            else:
                top, bottom = target.quads[-1].upper_right, target.quads[-1].lower_right
            pad = .055 * dist((top.x, top.y), (bottom.x, bottom.y))
            require(isclose((actual.rect.x0 + actual.rect.x1) / 2, bottom.x, abs_tol=.005)
                    and isclose(actual.rect.y1 - pad, bottom.y, abs_tol=.005), "caret_geometry_mismatch")
        components = group.components

    require(set(components) == {root, *row["associated_xrefs"]}, "annotation_graph_mismatch")
    for xref in components:
        subtype = document.xref_get_key(xref, "Subtype")[1]
        if subtype == "/Popup":
            parent_kind, parent_ref = document.xref_get_key(xref, "Parent")
            require(parent_kind == "xref", "missing_popup_parent")
            parent = int(parent_ref.split()[0])
            require(parent in components and document.xref_get_key(parent, "Popup") == ("xref", f"{xref} 0 R"),
                    "invalid_popup_parent")
        else:
            kind, appearance = document.xref_get_key(xref, "AP/N")
            require(kind == "xref" and bool(document.xref_stream(int(appearance.split()[0]))),
                    "missing_native_appearance")
    return components


def verify_saved(source: Path, output: Path, rows: list[dict], proposals: list[dict]) -> dict[str, Any]:
    with pymupdf.open(source) as original:
        original_pages = base_state(original)
        existing = annotation_state(original)
    with pymupdf.open(output) as document:
        inspect_document(document)
        require(base_state(document) == original_pages, "base_pdf_content_changed")
        annotated = annotation_state(document)
        require(all(annotated.get(key) == value for key, value in existing.items()), "existing_annotations_changed")
        created = set()
        counts: Counter[str] = Counter()
        for row in rows:
            if row["status"] != "applied":
                continue
            for xref in verify_one(document, row, proposals[row["proposal_index"]]):
                key = row["page_index"], xref
                require(key not in created, "shared_annotation_component")
                created.add(key)
                counts[document.xref_get_key(xref, "Subtype")[1]] += 1
        require(created == annotated.keys() - existing.keys(), "unexpected_native_annotations")
    return {"base_page_text_and_streams_unchanged": True, "existing_annotations_unchanged": True,
            "logical_annotations": sum(row["status"] == "applied" for row in rows),
            "native_objects": dict(counts), "native_structure_verified": True,
            "acrobat_desktop": "NOT RUN", "indesign": "NOT RUN"}
