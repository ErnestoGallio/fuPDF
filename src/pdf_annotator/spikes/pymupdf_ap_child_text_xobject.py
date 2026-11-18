"""Final isolated M15.1 probe: retain PyMuPDF's callout AP, replace only text.

This module is intentionally not imported by the product.  It accepts only the
single, observed PyMuPDF 1.28.2 FreeTextCallout text-object shape.  Any change
to that shape is a deliberate ``NotFeasible`` result: this is not a general
PDF content-stream parser or a callout compositor.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any


ASSETS = Path(__file__).parents[1] / "assets" / "fonts"
FONT_FILES = {
    "Source Sans 3": ASSETS / "SourceSans3-Regular.ttf",
    "Courier Prime": ASSETS / "CourierPrime-Regular.ttf",
}
FONT_RESOURCE = {"Source Sans 3": "FSource", "Courier Prime": "FCourier"}
FILL = (1.0, 0.95, 0.30)
BORDER = (0.90, 0.10, 0.10)
TEXT = (0.0, 0.0, 0.0)
OPACITY = 0.82
BORDER_WIDTH = 2.0
RECT = (260.0, 175.0, 570.0, 306.0)
CALLOUT = ((104.0, 408.0), (190.0, 321.0), (260.0, 264.0))
RUNS = (("Source Sans 3", "Run "), ("Courier Prime", "render_invoice()"),
        ("Source Sans 3", " now."))
PLAIN_TEXT = "".join(text for _family, text in RUNS)


class NotFeasible(RuntimeError):
    """The pinned writer's stream is not the narrow form this spike proved."""


@dataclass(frozen=True)
class NativeTextSlot:
    """The sole stable parent text object and its derived local coordinate space."""

    source: str
    start: int
    end: int
    matrix: tuple[float, float, float, float, float, float]
    local_bbox: tuple[float, float, float, float]


_TEXT_RUN = (
    r"/(?P<font>F\d+)\s+(?P<size>-?\d+(?:\.\d+)?)\s+Tf\s*\n"
    r"(?P<matrix>-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+"
    r"-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?)\s+Tm\s*\n"
    r"\[[^\]]+\]TJ\s*\n"
)
_TEXT_OBJECT = re.compile(r"(?P<text>BT\s*\n(?P<body>(?:" + _TEXT_RUN + r")+)ET)", re.ASCII)
_TEXT_RUN_RE = re.compile(_TEXT_RUN, re.ASCII)
_CHILD_RUN = re.compile(
    r"/(FSource|FCourier)\s+14\s+Tf\s*\n"
    r"1\s+0\s+0\s+-1\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Tm\s*\n"
    r"<[0-9A-F]+>\s+Tj",
    re.ASCII,
)
_LOCAL_TRANSFORM = re.compile(
    r"1\s+0\s+(?:-?0)\s+1\s+(?P<x>-?\d+(?:\.\d+)?)\s+(?P<y>-?\d+(?:\.\d+)?)\s+cm\s*\n"
    r"1\s+0\s+0\s+-1\s+(?:-?0)\s+(?P<h>-?\d+(?:\.\d+)?)\s+cm\s*\n"
    r"q\s*\n"
    r"(?P<clip_x>-?\d+(?:\.\d+)?)\s+(?P<clip_y>-?\d+(?:\.\d+)?)\s+"
    r"(?P<clip_w>\d+(?:\.\d+)?)\s+(?P<clip_h>\d+(?:\.\d+)?)\s+re\s*\nW\s+n\s*\n"
    r"0\s+0\s+0\s+rg\s*\n$",
    re.ASCII,
)


def _number(value: float) -> str:
    return f"{value:.5f}".rstrip("0").rstrip(".") or "0"


def _pdf_string(value: str) -> str:
    import pymupdf

    return pymupdf.get_pdf_str(value)


def _normal_xref(document: Any, annotation_xref: int) -> int:
    value = document.xref_get_key(annotation_xref, "AP")[1]
    match = re.search(r"/N\s+(\d+)\s+0\s+R", value)
    if match is None:
        raise NotFeasible("PyMuPDF did not create a normal appearance Form")
    return int(match.group(1))


def _native_text_slot(stream: str) -> NativeTextSlot:
    """Recognize the one 1.28.2 callout text block, or stop.

    This deliberately does not tokenize arbitrary content streams.  The tested
    writer emits a single ``BT ... ET`` containing exactly the three semantic
    Source / Courier / Source runs, each with a Tf, Tm and TJ. Its preceding
    graphics-state setup has two simple cm transforms and a clip.
    The child's local BBox comes from those generated transforms, not a visual
    compensation.
    """
    matches = list(_TEXT_OBJECT.finditer(stream))
    if len(matches) != 1:
        raise NotFeasible("Expected exactly one narrow PyMuPDF BT/ET text object")
    match = matches[0]
    runs = list(_TEXT_RUN_RE.finditer(match.group("body")))
    if [item.group("font") for item in runs] != ["F0", "F1", "F0"]:
        raise NotFeasible("Expected the pinned writer's three Source/Courier/Source text runs")
    before = stream[:match.start()]
    transform = _LOCAL_TRANSFORM.search(before)
    if transform is None:
        raise NotFeasible("Expected the known PyMuPDF local callout transform and clip")
    if transform.end() != len(before):
        raise NotFeasible("Text object is not immediately after the known generated clip")
    matrix = tuple(float(value) for value in runs[0].group("matrix").split())
    # The child inherits the two existing cm transforms.  Its local extent is
    # therefore the generated flipped-coordinate height and callout width.
    height = float(transform.group("h"))
    clip_width = float(transform.group("clip_w"))
    if height <= 0 or clip_width <= 0:
        raise NotFeasible("Generated local text coordinate system is degenerate")
    return NativeTextSlot(match.group("text"), match.start(), match.end(), matrix,
                          (0.0, 0.0, clip_width + 10.0, height))


def _encode(document: Any, font_xref: int, text: str) -> str:
    widths = document.get_char_widths(font_xref, max((ord(char) for char in text), default=0) + 1)
    cids: list[str] = []
    for char in text:
        glyph, _width = widths[ord(char)]
        if glyph <= 0:
            raise NotFeasible(f"Embedded font lacks U+{ord(char):04X}")
        cids.append(f"{glyph:04X}")
    return "<" + "".join(cids) + ">"


def _advance(document: Any, font_xref: int, text: str, size: float) -> float:
    widths = document.get_char_widths(font_xref, max((ord(char) for char in text), default=0) + 1)
    result = 0.0
    for char in text:
        glyph, width = widths[ord(char)]
        if glyph <= 0:
            raise NotFeasible(f"Embedded font lacks U+{ord(char):04X}")
        result += width * size
    return result


def _child_form(document: Any, fonts: dict[str, int], slot: NativeTextSlot) -> int:
    """Make a self-contained text-only Form in the parent's local coordinates."""
    a, b, c, d, x, y = slot.matrix
    if (a, b, c, d) != (1.0, 0.0, 0.0, -1.0):
        raise NotFeasible("Unexpected PyMuPDF text matrix orientation")
    size = 14.0
    pieces = ["BT"]
    current_x = x
    for family, text in RUNS:
        pieces.extend((f"/{FONT_RESOURCE[family]} {_number(size)} Tf",
                       f"1 0 0 -1 {_number(current_x)} {_number(y)} Tm",
                       f"{_encode(document, fonts[family], text)} Tj"))
        current_x += _advance(document, fonts[family], text, size)
    pieces.extend(("ET", ""))
    stream = "\n".join(pieces).encode("ascii")
    xref = document.get_new_xref()
    resources = " ".join(f"/{FONT_RESOURCE[family]} {fonts[family]} 0 R" for family in fonts)
    document.update_object(
        xref,
        "<< /Type /XObject /Subtype /Form /FormType 1 "
        f"/BBox [{' '.join(_number(value) for value in slot.local_bbox)}] "
        f"/Resources << /Font << {resources} >> >> /Length {len(stream)} >>",
    )
    document.update_stream(xref, stream)
    return xref


def _parent_resources_with_child(parent_object: str, child_xref: int) -> str:
    """Add the only permitted parent-resource delta: the child XObject name."""
    marker = "/Resources <<"
    if marker not in parent_object:
        raise NotFeasible("PyMuPDF AP is missing its Resources dictionary")
    return parent_object.replace(marker, marker + f" /XObject << /CanonicalText {child_xref} 0 R >>", 1)


def _rich_content() -> str:
    spans = "".join(
        f'<span style="font-family:{family};font-size:14pt;color:#000000;">{escape(text)}</span>'
        for family, text in RUNS
    )
    return ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" '
            'xfa:APIVersion="Acrobat:26.1.0" xfa:spec="2.0.2" '
            'style="font-family:Source Sans 3;font-size:14pt;color:#000000;text-align:left;">'
            f'<p dir="ltr">{spans}</p></body>')


def _native_callout(document: Any, page: Any) -> Any:
    import pymupdf

    annotation = page.add_freetext_annot(
        RECT, "", richtext=True, style="margin:0;", fill_color=FILL,
        border_color=BORDER, border_width=BORDER_WIDTH, opacity=OPACITY,
        callout=CALLOUT, line_end=pymupdf.PDF_ANNOT_LE_OPEN_ARROW,
    )
    # Establish the semantic state *before* requesting the baseline PyMuPDF
    # appearance.  The AP snapshot below is consequently the exact native AP
    # Acrobat will be able to regenerate from these keys.
    document.xref_set_key(annotation.xref, "NM", _pdf_string("pymupdf-ap-child-text-xobject"))
    document.xref_set_key(annotation.xref, "Contents", _pdf_string(PLAIN_TEXT))
    document.xref_set_key(annotation.xref, "RC", _pdf_string(_rich_content()))
    document.xref_set_key(annotation.xref, "DA", _pdf_string("0 0 0 rg /Helv 14 Tf"))
    document.xref_set_key(annotation.xref, "DS", _pdf_string(
        "font: Source Sans 3 14pt; text-align:left; color:#000000"))
    annotation.update(opacity=OPACITY, fill_color=FILL, text_color=BORDER, border_color=BORDER)
    return annotation


def _font_records(document: Any, form_object: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for resource, xref_text in re.findall(r"/(F(?:Source|Courier))\s+(\d+)\s+0\s+R", form_object):
        xref = int(xref_text)
        source = document.xref_object(xref)
        descendant = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", source)
        child = document.xref_object(int(descendant.group(1))) if descendant else ""
        descriptor = re.search(r"/FontDescriptor\s+(\d+)\s+0\s+R", child)
        descriptor_source = document.xref_object(int(descriptor.group(1))) if descriptor else ""
        embedded = re.search(r"/FontFile[23]?\s+(\d+)\s+0\s+R", descriptor_source)
        records.append({
            "resource": resource,
            "xref": xref,
            "subtype": re.search(r"/Subtype\s+/([^\s]+)", source).group(1),
            "base_font": re.search(r"/BaseFont\s+/([^\s]+)", source).group(1),
            "to_unicode": bool(re.search(r"/ToUnicode\s+\d+\s+0\s+R", source)),
            "font_file_xref": int(embedded.group(1)) if embedded else None,
        })
    return records


def _dict_keys(document: Any, xref: int, keys: tuple[str, ...]) -> dict[str, list[str]]:
    return {key: list(document.xref_get_key(xref, key)) for key in keys}


def _geometry_stream(stream: str, *, before: bool) -> str:
    if before:
        return _TEXT_OBJECT.sub("<TEXT>", stream)
    return stream.replace("q\n/CanonicalText Do\nQ", "<TEXT>")


def _child_bounds_sanity(document: Any, child_object: str, child_stream: str,
                         fonts: list[dict[str, Any]]) -> dict[str, Any]:
    """Prove our own child has no geometric font scaling and fits its BBox."""
    bbox_match = re.search(r"/BBox\s*\[\s*([^\]]+)\]", child_object)
    if bbox_match is None:
        return {"sane": False, "reason": "missing BBox"}
    bbox = tuple(float(value) for value in bbox_match.group(1).split())
    runs = list(_CHILD_RUN.finditer(child_stream))
    expected_resources = [FONT_RESOURCE[family] for family, _text in RUNS]
    if len(bbox) != 4 or [item.group(1) for item in runs] != expected_resources:
        return {"sane": False, "reason": "unexpected controlled child stream"}
    font_xrefs = {item["resource"]: item["xref"] for item in fonts}
    positions = [(float(item.group(2)), float(item.group(3))) for item in runs]
    right = max(x + _advance(document, font_xrefs[resource], text, 14.0)
                for (resource, (x, _y)), (_family, text) in zip(
                    zip(expected_resources, positions), RUNS, strict=True))
    left = min(x for x, _y in positions)
    baseline = positions[0][1]
    sane = bbox[0] <= left <= right <= bbox[2] and bbox[1] <= baseline <= bbox[3]
    return {"sane": sane, "bbox": list(bbox), "text_x_range": [left, right], "baseline": baseline}


def _render_checks(path: Path) -> dict[str, Any]:
    import pymupdf

    document = pymupdf.open(path)
    pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
    samples = pixmap.samples
    nonwhite = sum(1 for offset in range(0, len(samples), pixmap.n)
                   if samples[offset:offset + 3] != b"\xff\xff\xff")
    document.close()
    poppler = shutil.which("pdftoppm")
    poppler_ok = False
    if poppler:
        with TemporaryDirectory() as temporary:
            prefix = Path(temporary) / "page"
            run = subprocess.run([poppler, "-png", "-f", "1", "-l", "1", str(path), str(prefix)],
                                 check=False, capture_output=True, text=True, timeout=30)
            poppler_ok = run.returncode == 0 and any(Path(temporary).glob("page-*.png"))
    return {"mupdf_nonwhite_pixels": nonwhite, "mupdf_render_sensible": nonwhite > 2_000,
            "poppler_available": bool(poppler), "poppler_render_sensible": poppler_ok}


def create(path: Path) -> dict[str, Any]:
    """Create the one-callout artifact and return its structural inspection."""
    import pymupdf

    for font_file in FONT_FILES.values():
        if not font_file.is_file():
            raise NotFeasible(f"Missing static font asset: {font_file}")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    annotation = _native_callout(document, page)
    parent_xref = _normal_xref(document, annotation.xref)
    before_object = document.xref_object(parent_xref)
    before_stream = document.xref_stream(parent_xref).decode("ascii")
    slot = _native_text_slot(before_stream)
    parent_before = _dict_keys(document, parent_xref, ("Type", "Subtype", "BBox", "Matrix", "Resources"))

    # These are page-registered public Type0 fonts, but the child Form owns
    # their complete resource namespace and never references the parent /Font.
    fonts = {family: page.insert_font(fontname=FONT_RESOURCE[family], fontfile=str(font_file))
             for family, font_file in FONT_FILES.items()}
    # Preserve the semantic embedded-font default already established by the
    # preceding font spike. The PyMuPDF-generated AP remains the baseline;
    # only this annotation dictionary default is restored after update().
    document.xref_set_key(annotation.xref, "DA", _pdf_string(".9 .1 .1 rg /FSource 14 Tf"))
    document.xref_set_key(annotation.xref, "DS", _pdf_string(
        "font: Source Sans 3 14pt; text-align:left; color:#E61A1A"))
    annotation_before = _dict_keys(document, annotation.xref,
                                   ("Contents", "RC", "DA", "DS", "C", "BS", "CA", "CL", "RD", "IT", "LE"))
    child_xref = _child_form(document, fonts, slot)
    after_stream = before_stream[:slot.start] + "q\n/CanonicalText Do\nQ" + before_stream[slot.end:]
    document.update_object(parent_xref, _parent_resources_with_child(before_object, child_xref))
    document.update_stream(parent_xref, after_stream.encode("ascii"))
    document.save(path)
    document.close()
    return inspect(path, before_stream=before_stream, parent_before=parent_before,
                   annotation_before=annotation_before, slot=slot, child_xref=child_xref)


def inspect(path: Path, *, before_stream: str | None = None,
            parent_before: dict[str, list[str]] | None = None,
            annotation_before: dict[str, list[str]] | None = None,
            slot: NativeTextSlot | None = None, child_xref: int | None = None) -> dict[str, Any]:
    import pymupdf

    document = pymupdf.open(path)
    page = document[0]
    annotation = next((item for item in page.annots() or ()
                       if item.info.get("id") == "pymupdf-ap-child-text-xobject"), None)
    if annotation is None:
        raise NotFeasible("Artifact does not contain the expected callout")
    parent_xref = _normal_xref(document, annotation.xref)
    parent_object = document.xref_object(parent_xref)
    after_stream = document.xref_stream(parent_xref).decode("ascii")
    if child_xref is None:
        match = re.search(r"/CanonicalText\s+(\d+)\s+0\s+R", parent_object)
        if match is None:
            raise NotFeasible("Parent AP is missing /CanonicalText")
        child_xref = int(match.group(1))
    child_object = document.xref_object(child_xref)
    child_stream = document.xref_stream(child_xref).decode("ascii")
    parent_after = _dict_keys(document, parent_xref, ("Type", "Subtype", "BBox", "Matrix", "Resources"))
    annotation_after = _dict_keys(document, annotation.xref,
                                  ("Contents", "RC", "DA", "DS", "C", "BS", "CA", "CL", "RD", "IT", "LE"))
    child = {
        "xref": child_xref,
        "form": _dict_keys(document, child_xref, ("Type", "Subtype", "BBox", "Matrix", "Resources")),
        "stream": child_stream,
        "fonts": _font_records(document, child_object),
    }
    child["bounds_sanity"] = _child_bounds_sanity(document, child_object, child_stream, child["fonts"])
    result = {
        "scope": "isolated M15.1 AP surgery; not production code",
        "writer": {"pymupdf_version": pymupdf.VersionBind},
        "callout": {
            "annotation_xref": annotation.xref,
            "semantic": annotation_after,
            "parent_ap_xref": parent_xref,
        },
        "parent_ap": {
            "before": {"form": parent_before, "stream": before_stream},
            "after": {"form": parent_after, "stream": after_stream},
            "text_slot": None if slot is None else {
                "source": slot.source, "matrix": list(slot.matrix), "local_bbox": list(slot.local_bbox)},
            "geometry_stream_unchanged": (before_stream is not None and
                                            _geometry_stream(before_stream, before=True) ==
                                            _geometry_stream(after_stream, before=False)),
            "bbox_unchanged": parent_before is not None and parent_before["BBox"] == parent_after["BBox"],
            "matrix_unchanged": parent_before is not None and parent_before["Matrix"] == parent_after["Matrix"],
            "only_added_xobject_resource": ("/CanonicalText" in parent_after["Resources"][1]
                                               and "/CanonicalText" not in (parent_before or {}).get("Resources", ["", ""])[1]),
            "old_base14_text_not_painted": not bool(re.search(r"/F\d+\s+\d+(?:\.\d+)?\s+Tf", after_stream)),
        },
        "child_form": child,
        "semantic_state_unchanged": annotation_before is not None and annotation_before == annotation_after,
    }
    document.close()
    result["render"] = _render_checks(path)
    result["checks"] = {
        "child_type_form": child["form"]["Type"][1] == "/XObject" and child["form"]["Subtype"][1] == "/Form",
        "child_resources_isolated": ("/FSource" in child["form"]["Resources"][1]
                                       and "/FCourier" in child["form"]["Resources"][1]
                                       and "/F0" not in child["form"]["Resources"][1]),
        "embedded_type0_fonts": ({font["resource"] for font in child["fonts"]} == {"FSource", "FCourier"}
                                  and all(font["subtype"] == "Type0" and font["to_unicode"]
                                          and font["font_file_xref"] for font in child["fonts"])),
        "parent_calls_child": bool(re.search(r"q\s*/CanonicalText\s+Do\s*Q", after_stream)),
        "natural_text_matrix": bool(re.search(r"1 0 0 -1", child_stream)),
        "bounding_box_sane": child["bounds_sanity"]["sane"],
        # MuPDF is the mandatory renderer. Poppler is an optional independent
        # probe: its absence is not evidence that the generated AP is wrong.
        "render_sensible": (result["render"]["mupdf_render_sensible"] and
                             (not result["render"]["poppler_available"] or
                              result["render"]["poppler_render_sensible"])),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = argv or sys.argv[1:]
    target = (Path(arguments[0]) if arguments else
              Path("output/m15_1/text_xobject_spike/M15.1-pymupdf-ap-child-text-xobject.pdf"))
    inspection = create(target)
    target.with_name("inspection.json").write_text(json.dumps(inspection, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
