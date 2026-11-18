"""Versioned fuPDF correction ownership and strict, read-only graph validation.

The private PDF text string is a bounded JSON manifest of native /NM identities.
It is a structural claim, not authentication. Unknown/mixed graphs fail closed.
Only newly created motivated corrections receive it; external PDFs are not tagged.
"""

from dataclasses import dataclass
import json
from math import isfinite
from typing import Any

from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.identifiers import AnnotationId

MARKER_KEY = "fuPDFCorrection"
MAX_MANIFEST_CHARS = 8192
MAX_MANAGED_REPLIES = 32
_COMMON_KEYS = frozenset((
    "Type Subtype Rect C CA P F AP NM T CreationDate M Subj Contents RC RD Popup IT RT IRT "
    "QuadPoints Name Open Parent Border BS Sy"
).split())


@dataclass(frozen=True, slots=True)
class ManagedCorrection:
    page_index: int
    root: int
    kind: str
    group_id: str
    auxiliaries: tuple[int, ...]
    replies: tuple[int, ...]
    components: tuple[int, ...]

    @property
    def logical_id(self) -> str:
        return f"native:{self.page_index}:{self.root}"


def has_marker(document: Any, root: int) -> bool:
    return document.xref_get_key(root, MARKER_KEY)[0] != "null"


def _require(value: bool) -> None:
    if not value:
        raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest field")
        result[key] = value
    return result


def write_manifest(document: Any, root: int, group_id: str, kind: str,
                   auxiliaries: tuple[str, ...], replies: tuple[str, ...]) -> None:
    import pymupdf

    manifest = {"v": 1, "group": group_id, "kind": kind, "root": group_id,
                "auxiliaries": list(auxiliaries), "replies": list(replies)}
    value = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"))
    if len(value) > MAX_MANIFEST_CHARS:
        raise ValueError("Manifest exceeds limit")
    document.xref_set_key(root, MARKER_KEY, pymupdf.get_pdf_str(value))


def add_reason(document: Any, page: Any, root: int, name: str, text: str,
               author: str, stamp: str) -> tuple[int, int]:
    import pymupdf

    annotation = page.load_annot(root)
    top = annotation.rect.y0
    # A renderer without threaded-comment presentation may show the Note icon.
    # Keep it away from the editorial range without changing the parent geometry.
    reply = page.add_text_annot((max(0, page.cropbox.width - 48), top), text, icon="Note")
    reply.set_info(title=author, subject="Motivazione", content=text,
                   creationDate=stamp, modDate=stamp)
    reply.set_popup(pymupdf.Rect(page.cropbox.width * .5, 0,
                                 page.cropbox.width * .95, page.cropbox.height * .25))
    reply.set_open(False)
    reply.set_irt_xref(root)
    document.xref_set_key(reply.xref, "RT", "/R")
    document.xref_set_key(reply.xref, "NM", pymupdf.get_pdf_str(name))
    return reply.xref, reply.popup_xref


def collect_managed(document: Any, page_index: int, root: int) -> ManagedCorrection:
    """Verify identities, native roles, Popup pairs and every incoming edge.

    No author/content/proximity heuristic grants ownership. A foreign response,
    including one to a managed reply or on another page, blocks the entire delete.
    """
    try:
        return _collect(document, page_index, root)
    except DomainValidationError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, RuntimeError, OverflowError) as exc:
        raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION) from exc


def _collect(document: Any, page_index: int, root: int) -> ManagedCorrection:
    kind, raw = document.xref_get_key(root, MARKER_KEY)
    _require(kind == "string" and len(raw) <= MAX_MANIFEST_CHARS)
    manifest = json.loads(raw, object_pairs_hook=_unique_object)
    _require(isinstance(manifest, dict) and set(manifest) == {
        "v", "group", "kind", "root", "auxiliaries", "replies"})
    _require(type(manifest["v"]) is int and manifest["v"] == 1)
    group_id = AnnotationId(manifest["group"]).value
    _require(manifest["group"] == group_id and manifest["root"] == group_id)
    correction_kind = manifest["kind"]
    _require(correction_kind in ("delete_text", "insert_text", "replace_text"))
    auxiliary_names = manifest["auxiliaries"]
    reply_names = manifest["replies"]
    _require(auxiliary_names == ([group_id + ":strike"] if correction_kind == "replace_text" else []))
    _require(isinstance(reply_names, list) and 1 <= len(reply_names) <= MAX_MANAGED_REPLIES)
    for name in reply_names:
        _require(isinstance(name, str) and (name == group_id + ":reason" or
                 (name.startswith(group_id + ":reason:") and
                  AnnotationId(name[len(group_id + ":reason:"):]).value == name[len(group_id + ":reason:"):])))
    all_names = [group_id, *auxiliary_names, *reply_names]
    _require(len(all_names) == len(set(all_names)))

    def value(xref: int, name: str) -> str:
        return document.xref_get_key(xref, name)[1]

    def matches(xref: int, name: str, kind: str, text: str) -> bool:
        return document.xref_get_key(xref, name) == (kind, text)

    def absent(xref: int, name: str) -> bool:
        return matches(xref, name, "null", "null")

    physical: dict[int, int] = {}
    names: dict[str, int] = {}
    duplicates: set[str] = set()
    page_xref = None
    for number in range(document.page_count):
        page = document.load_page(number)
        if number == page_index:
            page_xref = page.xref
        for xref, _code, _name in page.annot_xrefs():
            _require(xref not in physical)
            physical[xref] = number
            nm_kind, nm = document.xref_get_key(xref, "NM")
            if nm_kind == "string":
                if nm in names:
                    duplicates.add(nm)
                names[nm] = xref
    _require(all(name in names and name not in duplicates for name in all_names))
    _require(names[group_id] == root and page_xref is not None)
    primary = [names[name] for name in all_names]
    _require(all(physical[xref] == page_index for xref in primary))
    auxiliaries = tuple(names[name] for name in auxiliary_names)
    replies = tuple(names[name] for name in reply_names)
    _require(absent(root, "IRT") and absent(root, "RT"))
    _require(matches(root, "Subtype", "name", "/StrikeOut" if correction_kind == "delete_text" else "/Caret"))
    _require(matches(root, "IT", "name", "/Replace") if correction_kind == "replace_text" else absent(root, "IT"))
    if correction_kind == "delete_text":
        _require(absent(root, "Contents") or matches(root, "Contents", "string", ""))
    else:
        _require(document.xref_get_key(root, "Contents")[0] == "string" and bool(value(root, "Contents")))
    for xref in auxiliaries:
        _require(matches(xref, "Subtype", "name", "/StrikeOut") and matches(xref, "IT", "name", "/StrikeOutTextEdit")
                 and matches(xref, "IRT", "xref", f"{root} 0 R") and matches(xref, "RT", "name", "/Group"))
    for xref in replies:
        _require(matches(xref, "Subtype", "name", "/Text") and matches(xref, "IRT", "xref", f"{root} 0 R")
                 and (matches(xref, "RT", "name", "/R") or absent(xref, "RT")) and absent(xref, "IT"))
        _require(document.xref_get_key(xref, "Contents")[0] == "string")

    components = set(primary)
    expected_popups: dict[int, int] = {}
    for xref in primary:
        allowed = _COMMON_KEYS | ({MARKER_KEY} if xref == root else set())
        _require(set(document.xref_get_keys(xref)) <= allowed)
        _require(absent(xref, "P") or matches(xref, "P", "xref", f"{page_xref} 0 R"))
        _require(absent(xref, "Parent"))
        _require(absent(xref, "F") or document.xref_get_key(xref, "F")[0] == "int")
        flags = int(value(xref, "F")) if value(xref, "F") != "null" else 0
        _require(flags >= 0 and flags & ~28 == 0)
        _require(document.xref_get_key(xref, "Rect")[0] == "array")
        rect = [float(item) for item in value(xref, "Rect").strip("[]").split()]
        _require(len(rect) == 4 and all(isfinite(item) for item in rect)
                 and rect[2] > rect[0] and rect[3] > rect[1])
        ap_kind, ap = document.xref_get_key(xref, "AP/N")
        _require(ap_kind == "xref" and document.xref_is_stream(int(ap.split()[0])))
        if value(xref, "Subtype") == "/StrikeOut":
            _require(document.xref_get_key(xref, "QuadPoints")[0] == "array")
            quads = [float(item) for item in value(xref, "QuadPoints").strip("[]").split()]
            _require(bool(quads) and len(quads) % 8 == 0 and all(isfinite(item) for item in quads))
        popup_kind, popup_ref = document.xref_get_key(xref, "Popup")
        _require(popup_kind == "xref")
        popup = int(popup_ref.split()[0])
        _require(popup in physical and physical[popup] == page_index and popup not in components)
        _require(matches(popup, "Subtype", "name", "/Popup") and matches(popup, "Parent", "xref", f"{xref} 0 R"))
        _require(set(document.xref_get_keys(popup)) <= _COMMON_KEYS)
        components.add(popup)
        expected_popups[popup] = xref

    for xref in physical:
        irt_kind, irt = document.xref_get_key(xref, "IRT")
        if irt_kind == "xref" and int(irt.split()[0]) in components:
            _require(xref in auxiliaries or xref in replies)
        popup_kind, popup = document.xref_get_key(xref, "Popup")
        if popup_kind == "xref" and int(popup.split()[0]) in components:
            _require(expected_popups.get(int(popup.split()[0])) == xref)
        parent_kind, parent = document.xref_get_key(xref, "Parent")
        if parent_kind == "xref" and int(parent.split()[0]) in components:
            _require(expected_popups.get(xref) == int(parent.split()[0]))
    _require(all(absent(xref, "IRT") and absent(xref, "RT") for xref in expected_popups))
    return ManagedCorrection(page_index, root, correction_kind, group_id,
                             auxiliaries, replies, tuple(sorted(components)))
