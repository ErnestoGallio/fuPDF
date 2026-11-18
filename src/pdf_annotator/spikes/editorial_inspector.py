"""Offline, read-only M10 reference inspection; never a product mutation path.

Run this module in its own CLI process on a closed fixture. No Qt, worker IPC,
PDF writes, semantic classification or inferred correction grouping is exposed.
"""

import argparse
import base64
from collections import deque
from collections.abc import Iterator
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
import sys
from typing import Any


EDITORIAL_KEYS = (
    "Subtype", "Rect", "QuadPoints", "Contents", "NM", "T", "M",
    "CreationDate", "IT", "RT", "IRT", "Popup", "Open", "State",
    "StateModel", "RC", "DA", "LE", "Parent", "AP", "P",
    "RD", "Sy", "Subj", "C", "F",
)
_DELIMITERS = "()<>[]{}/%\x00\t\n\f\r "
_INTEGER = re.compile(r"[0-9]+\Z")


def _tokens(source: str) -> Iterator[str]:
    """Lex serialized object syntax, excluding strings/names from ref matching.

    This only locates indirect references in MuPDF's parsed object serialization;
    it does not parse original PDF bytes or alter dictionaries. In particular,
    comment text such as '(42 0 R)' must never become a graph edge.
    """
    index = 0
    while index < len(source):
        char = source[index]
        if char in "\x00\t\n\f\r ":
            index += 1
        elif char == "%":
            while index < len(source) and source[index] not in "\r\n":
                index += 1
        elif char == "(":
            depth = 1
            index += 1
            while index < len(source) and depth:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
                index += 1
            if depth:
                raise ValueError("Unterminated serialized PDF string")
            yield "string"
        elif char == "<" and source[index:index + 2] != "<<":
            end = source.find(">", index + 1)
            if end == -1:
                raise ValueError("Unterminated serialized PDF hex string")
            index = end + 1
            yield "string"
        elif char == "/":
            index += 1
            while index < len(source) and source[index] not in _DELIMITERS:
                index += 1
            yield "name"
        elif source[index:index + 2] in ("<<", ">>"):
            yield source[index:index + 2]
            index += 2
        elif char in _DELIMITERS:
            yield char
            index += 1
        else:
            start = index
            while index < len(source) and source[index] not in _DELIMITERS:
                index += 1
            yield source[start:index]


def indirect_references(source: str) -> tuple[tuple[int, int], ...]:
    """Return unique (object number, generation) references in encounter order."""
    recent: deque[str] = deque(maxlen=3)
    found: dict[tuple[int, int], None] = {}
    for token in _tokens(source):
        recent.append(token)
        if (len(recent) == 3 and recent[2] == "R"
                and _INTEGER.fullmatch(recent[0]) and _INTEGER.fullmatch(recent[1])):
            found[(int(recent[0]), int(recent[1]))] = None
    return tuple(found)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _object(document: Any, xref: int, include_streams: bool) -> dict[str, Any]:
    source = document.xref_object(xref, compressed=False, ascii=True)
    dictionary = source.lstrip().startswith("<<")
    keys = ({key: {"type": kind, "value": value}
             for key in document.xref_get_keys(xref)
             for kind, value in (document.xref_get_key(xref, key),)}
            if dictionary else {})
    record: dict[str, Any] = {
        "xref": xref, "serialized_object": source, "keys": keys,
        "references": [{"xref": ref, "generation": generation}
                       for ref, generation in indirect_references(source)],
    }
    if document.xref_is_stream(xref):
        decoded = document.xref_stream(xref)
        raw = document.xref_stream_raw(xref)
        record["stream"] = {
            "decoded_bytes": len(decoded), "decoded_sha256": sha256(decoded).hexdigest(),
            "raw_bytes": len(raw), "raw_sha256": sha256(raw).hexdigest(),
        }
        if include_streams:
            record["stream"]["decoded_base64"] = base64.b64encode(decoded).decode("ascii")
            record["stream"]["raw_base64"] = base64.b64encode(raw).decode("ascii")
    if keys.get("Type", {}).get("value") in ("/Page", "/Pages", "/Catalog"):
        # Retain the actual object and all edges, but do not walk from a comment
        # back through /P into the whole document's unrelated content tree.
        record["traversal_boundary"] = "document_structure"
    return record


def inspect_reference(path: Path, *, include_streams: bool = False) -> dict[str, Any]:
    """Inspect every page annotation and its reachable objects without saving.

    Reports raw PDF geometry separately from canonical PyMuPDF API geometry.
    Caller must use an offline process, never the GUI or a live app session.
    """
    import pymupdf

    path = path.resolve(strict=True)
    before = _digest(path)
    annotations: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    pending: deque[int] = deque()
    objects: dict[int, dict[str, Any]] = {}
    annotation_arrays: set[int] = set()
    with pymupdf.open(path) as document:
        if (not document.is_pdf or document.needs_pass or document.is_encrypted
                or (document.metadata or {}).get("encryption")):
            raise ValueError("An unencrypted PDF reference is required")
        for page in document:
            api = {}
            for annotation in page.annots() or ():
                api[annotation.xref] = {
                    "type": list(annotation.type), "info": annotation.info,
                    "rect": list(annotation.rect), "vertices": annotation.vertices,
                    "colors": annotation.colors, "flags": annotation.flags,
                }
            roots = page.annot_xrefs()
            annots_entry = document.xref_get_key(page.xref, "Annots")
            if annots_entry[0] == "xref":
                array_xref = int(annots_entry[1].split()[0])
                annotation_arrays.add(array_xref)
                pending.append(array_xref)
            pages.append({
                "page_index": page.number, "xref": page.xref,
                "rotation": page.rotation, "cropbox": list(page.cropbox),
                "mediabox": list(page.mediabox), "display_rect": list(page.rect),
                "pdf_to_pymupdf_matrix": list(page.transformation_matrix),
                "annots_entry": list(annots_entry),
            })
            # /Annots includes Popup/Widget/Link entries omitted by Page.annots().
            for xref, type_code, name in roots:
                annotations.append({
                    "page_index": page.number, "xref": xref,
                    "api_type_code": type_code, "api_nm": name,
                    "api": api.get(xref),
                    "editorial_keys": {
                        key: {"type": kind, "value": value}
                        for key in EDITORIAL_KEYS
                        for kind, value in (document.xref_get_key(xref, key),)},
                })
                pending.append(xref)
        while pending:
            xref = pending.popleft()
            if xref in objects:
                continue
            if not 0 < xref < document.xref_length():
                objects[xref] = {"xref": xref, "error": "reference_outside_xref_table"}
                continue
            record = _object(document, xref, include_streams)
            if xref in annotation_arrays:
                record["role"] = "page_annotation_array"
            objects[xref] = record
            if "traversal_boundary" not in record:
                pending.extend(ref["xref"] for ref in record["references"])
        report = {
            "schema_version": 1, "path": str(path), "sha256": before,
            "pymupdf_version": pymupdf.VersionBind, "metadata": document.metadata,
            "is_repaired": document.is_repaired,
            "semantics": "unclassified; Adobe authorship and interoperability require manual evidence",
            "geometry": "API rect/vertices: unrotated PyMuPDF; dictionary geometry: native PDF",
            "serialization": "MuPDF-parsed object representation, not original byte offsets or spelling",
            "traversal": "all annotation roots; cycle-safe references; stop after recording Page/Pages/Catalog",
            "pages": pages, "annotations": annotations,
            "objects": [objects[xref] for xref in sorted(objects)],
        }
    if _digest(path) != before:
        raise ValueError("Reference changed during inspection; discard the report")
    report["source_unchanged"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(
        "Read-only M10 inspection of a closed reference PDF. Output includes comment text; "
        "no semantic/Adobe compatibility classification is performed."))
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, help="New UTF-8 JSON file; existing files are refused")
    parser.add_argument("--include-streams", action="store_true",
                        help="Include full raw/decoded linked stream bytes as base64")
    args = parser.parse_args(argv)
    try:
        report = inspect_reference(args.pdf, include_streams=args.include_streams)
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
        else:
            # ASCII escaping keeps stdout lossless on Windows console encodings.
            print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Inspection failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
