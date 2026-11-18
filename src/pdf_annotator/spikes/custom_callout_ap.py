"""Isolated M15.1 experiment: complete canonical FreeText /AP compositor.

This module deliberately has no production imports.  It owns the final normal
appearance only for three disposable annotations.  PyMuPDF is used as the
public font-object factory and to create the annotation roots; after that no
``Annot.update()`` is used to build the final appearances.
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
PADDING = 8.0


@dataclass(frozen=True)
class Run:
    family: str
    text: str
    size: float = 14.0


@dataclass(frozen=True)
class CalloutGeometry:
    """Canonical PyMuPDF points: anchor -> knee -> text-box attachment."""

    anchor: tuple[float, float]
    knee: tuple[float, float]
    attach: tuple[float, float]

    @property
    def points(self) -> tuple[tuple[float, float], ...]:
        return self.anchor, self.knee, self.attach


@dataclass(frozen=True)
class Specimen:
    name: str
    rect: tuple[float, float, float, float]
    runs: tuple[Run, ...]
    callout: CalloutGeometry | None = None

    @property
    def contents(self) -> str:
        return "".join(run.text for run in self.runs)


SIMPLE = Specimen(
    "simple-freetext",
    (48.0, 70.0, 330.0, 142.0),
    (Run("Source Sans 3", "FreeText custom AP - Source Sans 3\nfill, border and clipping."),),
)
CALLOUT = Specimen(
    "rich-callout",
    (270.0, 175.0, 548.0, 306.0),
    (
        Run("Source Sans 3", "Verificare la chiamata "),
        Run("Courier Prime", "render_invoice()", 14.0),
        Run("Source Sans 3", " prima della stampa.\nLeader obliqua, knee e OpenArrow."),
    ),
    CalloutGeometry((104.0, 408.0), (190.0, 321.0), (270.0, 264.0)),
)
EDGE_CALLOUT = Specimen(
    "edge-callout",
    (472.0, 62.0, 562.0, 117.0),
    (Run("Source Sans 3", "Box piccolo\nfuori CropBox", 11.0),),
    # The anchor is deliberately outside both /Rect and the visible CropBox.
    CalloutGeometry((630.0, -32.0), (600.0, 22.0), (562.0, 88.0)),
)


def _pdf_string(value: str) -> str:
    import pymupdf

    return pymupdf.get_pdf_str(value)


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _encode_text_for_embedded_font(document: Any, font_xref: int, text: str) -> str:
    """Encode ASCII/Latin text through the public Unicode-to-CID table."""
    widths = document.get_char_widths(font_xref, max((ord(char) for char in text), default=0) + 1)
    encoded: list[str] = []
    for char in text:
        glyph, _width = widths[ord(char)]
        if glyph <= 0:
            raise ValueError(f"Font lacks U+{ord(char):04X}")
        encoded.append(f"{glyph:04X}")
    return "<" + "".join(encoded) + ">"


def _glyph_width(widths: list[tuple[int, float]], char: str, size: float) -> float:
    glyph, width = widths[ord(char)]
    if glyph <= 0:
        raise ValueError(f"Font lacks U+{ord(char):04X}")
    return width * size


def _wrapped_segments(document: Any, fonts: dict[str, int], specimen: Specimen) -> list[tuple[Run, str, float, float]]:
    """Return positioned chunks in canonical top-left page coordinates.

    This is intentionally a small deterministic Latin-only layouter: newline,
    character wrapping, run changes and a conservative fixed line height.
    It is not a shaping engine.
    """
    x0, y0, x1, y1 = specimen.rect
    usable_width = x1 - x0 - 2 * PADDING
    x, baseline = x0 + PADDING, y1 - PADDING - 14.0
    line_height = 18.0
    width_tables = {
        family: document.get_char_widths(
            xref, max((ord(char) for run in specimen.runs if run.family == family for char in run.text), default=0) + 1
        )
        for family, xref in fonts.items()
    }
    result: list[tuple[Run, str, float, float]] = []
    for run in specimen.runs:
        chunk = ""
        chunk_x = x
        for char in run.text:
            if char == "\n":
                if chunk:
                    result.append((run, chunk, chunk_x, baseline))
                    chunk = ""
                x, baseline = x0 + PADDING, baseline - line_height
                chunk_x = x
                continue
            advance = _glyph_width(width_tables[run.family], char, run.size)
            if x > x0 + PADDING and x + advance > x0 + PADDING + usable_width:
                if chunk:
                    result.append((run, chunk, chunk_x, baseline))
                    chunk = ""
                x, baseline = x0 + PADDING, baseline - line_height
                chunk_x = x
            if baseline < y0 + PADDING + run.size:
                # The stream clipping remains authoritative, but this explicit
                # stop avoids emitting an unbounded off-box text object.
                return result
            chunk += char
            x += advance
        if chunk:
            result.append((run, chunk, chunk_x, baseline))
    return result


def _paint_bounds(specimen: Specimen) -> tuple[float, float, float, float]:
    points = [(specimen.rect[0], specimen.rect[1]), (specimen.rect[2], specimen.rect[3])]
    if specimen.callout:
        points.extend(specimen.callout.points)
    xs, ys = zip(*points)
    # Include arrow legs, stroke width and a small safety margin in the Form BBox.
    return min(xs) - 14.0, min(ys) - 14.0, max(xs) + 14.0, max(ys) + 14.0


def _open_arrow(anchor: tuple[float, float], knee: tuple[float, float]) -> list[str]:
    """Return an unfilled OpenArrow with its tip at the first /CL point."""
    from math import atan2, cos, pi, sin

    angle = atan2(knee[1] - anchor[1], knee[0] - anchor[0])
    length, spread = 12.0, pi / 6
    legs = [(anchor[0] + length * cos(angle + spread), anchor[1] + length * sin(angle + spread)),
            (anchor[0] + length * cos(angle - spread), anchor[1] + length * sin(angle - spread))]
    return [f"{_number(legs[0][0])} {_number(legs[0][1])} m {_number(anchor[0])} {_number(anchor[1])} l",
            f"{_number(anchor[0])} {_number(anchor[1])} m {_number(legs[1][0])} {_number(legs[1][1])} l"]


def _appearance(document: Any, fonts: dict[str, int], specimen: Specimen) -> tuple[int, list[tuple[Run, str, float, float]]]:
    """Create one complete normal appearance Form XObject.

    Form coordinates deliberately remain canonical PyMuPDF unrotated page
    coordinates.  The /BBox is their full paint envelope, including the leader
    outside /Rect; no implicit PyMuPDF appearance clipping is relied upon.
    """
    x0, y0, x1, y1 = specimen.rect
    paint = _paint_bounds(specimen)
    layout = _wrapped_segments(document, fonts, specimen)
    xref = document.get_new_xref()
    resources = " ".join(f"/{FONT_RESOURCE[family]} {fonts[family]} 0 R" for family in fonts)
    stream = ["q", "/GS1 gs", f"{_number(FILL[0])} {_number(FILL[1])} {_number(FILL[2])} rg",
              f"{_number(BORDER[0])} {_number(BORDER[1])} {_number(BORDER[2])} RG {_number(BORDER_WIDTH)} w",
              f"{_number(x0)} {_number(y0)} {_number(x1 - x0)} {_number(y1 - y0)} re B",
              "q", f"{_number(x0 + PADDING)} {_number(y0 + PADDING)} "
              f"{_number(x1 - x0 - 2 * PADDING)} {_number(y1 - y0 - 2 * PADDING)} re W n", "BT"]
    for run, text, x, y in layout:
        stream.extend((f"{_number(TEXT[0])} {_number(TEXT[1])} {_number(TEXT[2])} rg",
                       f"/{FONT_RESOURCE[run.family]} {_number(run.size)} Tf",
                       f"1 0 0 1 {_number(x)} {_number(y)} Tm",
                       f"{_encode_text_for_embedded_font(document, fonts[run.family], text)} Tj"))
    stream.extend(("ET", "Q"))
    if specimen.callout:
        anchor, knee, attach = specimen.callout.points
        stream.extend((f"{_number(BORDER[0])} {_number(BORDER[1])} {_number(BORDER[2])} RG",
                       f"{_number(BORDER_WIDTH)} w",
                       f"{_number(anchor[0])} {_number(anchor[1])} m {_number(knee[0])} {_number(knee[1])} l "
                       f"{_number(attach[0])} {_number(attach[1])} l S",
                       *_open_arrow(anchor, knee), "S"))
    stream.extend(("Q", ""))
    encoded = "\n".join(stream).encode("ascii")
    document.update_object(
        xref,
        "<< /Type /XObject /Subtype /Form /FormType 1 "
        f"/BBox [{' '.join(_number(value) for value in paint)}] "
        "/Resources << "
        f"/Font << {resources} >> /ExtGState << /GS1 << /Type /ExtGState /ca {_number(OPACITY)} /CA {_number(OPACITY)} >> >> "
        f">> /Length {len(encoded)} >>",
    )
    document.update_stream(xref, encoded)
    return xref, layout


def _rich_content(specimen: Specimen) -> str:
    spans = "".join(
        f'<span style="font-family:{run.family};font-size:{_number(run.size)}pt;color:#000000;">'
        f"{escape(run.text).replace(chr(10), '<br/>')}</span>"
        for run in specimen.runs
    )
    return ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" '
            'xfa:APIVersion="Acrobat:26.1.0" xfa:spec="2.0.2" '
            'style="font-family:Source Sans 3;font-size:14pt;color:#000000;text-align:left;">'
            f'<p dir="ltr">{spans}</p></body>')


def _array(values: tuple[float, ...] | list[float]) -> str:
    return "[" + " ".join(_number(value) for value in values) + "]"


def _add(document: Any, page: Any, fonts: dict[str, int], specimen: Specimen) -> dict[str, Any]:
    import pymupdf

    rect = pymupdf.Rect(specimen.rect)
    # This public constructor owns page annotation registration and baseline
    # dictionary lifecycle. Its provisional appearance is fully replaced below.
    annotation = page.add_freetext_annot(rect, "", richtext=True, fill_color=FILL,
                                         border_color=BORDER, border_width=BORDER_WIDTH,
                                         opacity=OPACITY)
    ap, layout = _appearance(document, fonts, specimen)
    root = annotation.xref
    document.xref_set_key(root, "NM", _pdf_string(specimen.name))
    document.xref_set_key(root, "Contents", _pdf_string(specimen.contents))
    document.xref_set_key(root, "RC", _pdf_string(_rich_content(specimen)))
    document.xref_set_key(root, "DA", _pdf_string("0 0 0 rg /FSource 14 Tf"))
    document.xref_set_key(root, "DS", _pdf_string("font: Source Sans 3 14pt; text-align:left; color:#000000"))
    document.xref_set_key(root, "C", _array(list(FILL)))
    document.xref_set_key(root, "BS", f"<</W {_number(BORDER_WIDTH)} /S /S>>")
    document.xref_set_key(root, "CA", _number(OPACITY))
    document.xref_set_key(root, "RD", "[.5 .5 .5 .5]")
    if specimen.callout:
        document.xref_set_key(root, "IT", "/FreeTextCallout")
        document.xref_set_key(root, "CL", _array([coordinate for point in specimen.callout.points for coordinate in point]))
        document.xref_set_key(root, "LE", "/OpenArrow")
    else:
        document.xref_set_key(root, "CL", "null")
        document.xref_set_key(root, "LE", "null")
    document.xref_set_key(root, "AP", f"<</N {ap} 0 R>>")
    return {"root": root, "ap": ap, "layout": layout, "paint_bounds": _paint_bounds(specimen)}


def create(path: Path) -> None:
    import pymupdf

    for font_file in FONT_FILES.values():
        if not font_file.is_file():
            raise RuntimeError(f"Missing static font asset: {font_file}")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    # Non-zero CropBox makes the edge leader a real coordinate-case, while the
    # AP itself remains in unrotated canonical PyMuPDF page coordinates.
    page.set_cropbox(pymupdf.Rect(20, 30, 592, 762))
    fonts = {family: page.insert_font(fontname=FONT_RESOURCE[family], fontfile=str(font_file))
             for family, font_file in FONT_FILES.items()}
    for specimen in (SIMPLE, CALLOUT, EDGE_CALLOUT):
        _add(document, page, fonts, specimen)
    document.save(path)
    document.close()


def _normal_xref(document: Any, root: int) -> int:
    match = re.search(r"/N\s+(\d+)\s+0\s+R", document.xref_get_key(root, "AP")[1])
    if match is None:
        raise ValueError("Missing normal appearance reference")
    return int(match.group(1))


def _font_records(document: Any, form: str) -> list[dict[str, Any]]:
    fonts: list[dict[str, Any]] = []
    for resource, xref in re.findall(r"/(F\w+)\s+(\d+)\s+0\s+R", form):
        source = document.xref_object(int(xref))
        descendant = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", source)
        child = document.xref_object(int(descendant.group(1))) if descendant else ""
        descriptor = re.search(r"/FontDescriptor\s+(\d+)\s+0\s+R", child)
        descriptor_source = document.xref_object(int(descriptor.group(1))) if descriptor else ""
        embedded = re.search(r"/FontFile[23]?\s+(\d+)\s+0\s+R", descriptor_source)
        fonts.append({"resource": resource, "xref": int(xref),
                      "subtype": re.search(r"/Subtype\s+/([^\s]+)", source).group(1),
                      "base_font": re.search(r"/BaseFont\s+/([^\s]+)", source).group(1),
                      "to_unicode": bool(re.search(r"/ToUnicode\s+\d+\s+0\s+R", source)),
                      "font_file_xref": int(embedded.group(1)) if embedded else None})
    return fonts


def inspect(path: Path) -> dict[str, Any]:
    import pymupdf

    document = pymupdf.open(path)
    result: dict[str, Any] = {
        "coordinate_model": {
            "canonical": "PyMuPDF unrotated page coordinates (top-left, y down)",
            "ap": "AP stream and BBox use the same canonical coordinates; BBox is the full paint envelope.",
            "cropbox": "No CropBox offset or page rotation is baked into annotation or AP geometry.",
            "rotation": "The artifact is unrotated; production would apply the established coordinate mapper before this canonical writer.",
        },
        "annotations": [],
    }
    for page in document:
        for annotation in page.annots() or ():
            name = annotation.info.get("id")
            if name not in {specimen.name for specimen in (SIMPLE, CALLOUT, EDGE_CALLOUT)}:
                continue
            root = annotation.xref
            ap = _normal_xref(document, root)
            form = document.xref_object(ap)
            stream = document.xref_stream(ap).decode("ascii")
            keys = {key: list(document.xref_get_key(root, key))
                    for key in ("Subtype", "IT", "Rect", "RD", "CL", "LE", "Contents", "RC", "DA", "DS", "C", "BS", "CA", "AP")}
            result["annotations"].append({
                "name": name,
                "annotation_xref": root,
                "dictionary": keys,
                "ap_xref": ap,
                "ap_form": {key: list(document.xref_get_key(ap, key))
                            for key in ("Type", "Subtype", "BBox", "Matrix", "Resources")},
                "ap_bbox": list(document.xref_get_key(ap, "BBox")),
                "ap_resources": list(document.xref_get_key(ap, "Resources")),
                "fonts": _font_records(document, form),
                "embedded_font_streams": [font["font_file_xref"] for font in _font_records(document, form)],
                "extgstate": list(document.xref_get_key(ap, "Resources/ExtGState")),
                "operators": re.findall(r"(?:\b(?:q|Q|rg|RG|w|re|B|W|n|BT|ET|Tf|Tm|Tj|m|l|S|gs)\b)", stream),
                "stream": stream,
                "arrow_geometry": (next((list(specimen.callout.points) for specimen in (CALLOUT, EDGE_CALLOUT)
                                         if specimen.name == name), None)),
                "semantic_text": annotation.info.get("content"),
                "extracted_text": page.get_text(),
            })
    document.close()
    result["annotations"].sort(key=lambda record: record["name"])
    return result


def verify(path: Path, inspection: dict[str, Any]) -> dict[str, Any]:
    """Reopen and render with MuPDF plus Poppler; neither substitutes Acrobat."""
    import pymupdf

    by_name = {record["name"]: record for record in inspection["annotations"]}
    expected = {specimen.name for specimen in (SIMPLE, CALLOUT, EDGE_CALLOUT)}
    structural = set(by_name) == expected
    if structural:
        for specimen in (SIMPLE, CALLOUT, EDGE_CALLOUT):
            record = by_name[specimen.name]
            dictionary = record["dictionary"]
            structural &= dictionary["Subtype"][1] == "/FreeText"
            structural &= record["semantic_text"] == specimen.contents
            structural &= bool(dictionary["RC"][1]) and bool(dictionary["AP"][1])
            structural &= record["ap_form"]["Type"][1] == "/XObject"
            structural &= record["ap_form"]["Subtype"][1] == "/Form"
            structural &= {font["subtype"] for font in record["fonts"]} == {"Type0"}
            structural &= all(font["to_unicode"] and font["font_file_xref"] for font in record["fonts"])
            structural &= all(operator in record["operators"] for operator in ("re", "B", "W", "BT", "ET"))
            if specimen.callout:
                structural &= dictionary["IT"][1] == "/FreeTextCallout"
                structural &= dictionary["LE"][1] == "/OpenArrow"
                expected_cl = _array([coordinate for point in specimen.callout.points for coordinate in point])
                structural &= dictionary["CL"][1] == expected_cl
                structural &= all(operator in record["operators"] for operator in ("m", "l", "S"))
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
            prefix = Path(temporary) / "callout"
            run = subprocess.run([poppler, "-png", "-f", "1", "-l", "1", str(path), str(prefix)],
                                 check=False, capture_output=True, text=True, timeout=30)
            poppler_ok = run.returncode == 0 and any(Path(temporary).glob("callout-*.png"))
    return {"reopen_pymupdf": True, "structural": structural,
            "mupdf_nonwhite_pixels": nonwhite, "mupdf_render_sensible": nonwhite > 2_000,
            "poppler_available": bool(poppler), "poppler_render_sensible": poppler_ok}


def main(argv: list[str] | None = None) -> int:
    arguments = argv or sys.argv[1:]
    target = Path(arguments[0]) if arguments else Path("output/m15_1/callout_spike/M15.1-custom-callout-ap-spike.pdf")
    create(target)
    report = inspect(target)
    report["verification"] = verify(target, report)
    target.with_name("inspection.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
