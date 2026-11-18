"""Disposable M15.1 probe for embedded rich FreeText appearances.

This is intentionally not imported by the product.  It tests whether public
PyMuPDF font insertion plus a small PDF appearance dictionary is sufficient
for Acrobat-facing, embedded multi-font FreeText.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


ASSETS = Path(__file__).parents[1] / "assets" / "fonts"
FONT_FILES = {
    "Source Sans 3": ASSETS / "SourceSans3-Regular.ttf",
    "Courier Prime": ASSETS / "CourierPrime-Regular.ttf",
}
FONT_RESOURCE = {"Source Sans 3": "FSource", "Courier Prime": "FCourier"}
FILL = (1.0, .95, .30)
BORDER = (.90, .10, .10)
TEXT = (0.0, 0.0, 0.0)
BORDER_WIDTH = 2.0
RUNS = {
    "source-only": (("Source Sans 3", "Source Sans 3 embedded."),),
    "multi-run": (("Source Sans 3", "Per stampare utilizziamo "), ("Courier Prime", "cout"),
                  ("Source Sans 3", " nel programma.")),
}


def _pdf_string(value: str) -> str:
    import pymupdf
    return pymupdf.get_pdf_str(value)


def _encode_text_for_embedded_font(document, font_xref: int, text: str) -> str:
    """Encode through MuPDF's public Unicode→CID table for Identity-H."""
    widths = document.get_char_widths(font_xref, max((ord(char) for char in text), default=0) + 1)
    values: list[str] = []
    for char in text:
        glyph, _width = widths[ord(char)]
        if glyph <= 0:
            raise ValueError(f"Font lacks U+{ord(char):04X}")
        values.append(f"{glyph:04X}")
    return "<" + "".join(values) + ">"


def _appearance(document, rect, fonts: dict[str, int], runs: tuple[tuple[str, str], ...]) -> int:
    xref = document.get_new_xref()
    resources = " ".join(f"/{FONT_RESOURCE[family]} {fonts[family]} 0 R" for family in fonts)
    x, y = rect.x0 + 8, rect.y1 - 20
    pieces = ["q", f"{FILL[0]:g} {FILL[1]:g} {FILL[2]:g} rg", f"{BORDER[0]:g} {BORDER[1]:g} {BORDER[2]:g} RG {BORDER_WIDTH:g} w",
              f"{rect.x0:g} {rect.y0:g} {rect.width:g} {rect.height:g} re B",
              f"{TEXT[0]:g} {TEXT[1]:g} {TEXT[2]:g} rg", "BT",
              f"1 0 0 1 {x:g} {y:g} Tm"]
    for family, text in runs:
        pieces.extend((f"/{FONT_RESOURCE[family]} 14 Tf",
                       _encode_text_for_embedded_font(document, fonts[family], text), "Tj"))
    pieces.extend(("ET", "Q"))
    stream = ("\n".join(pieces) + "\n").encode("ascii")
    document.update_object(xref, "<< /Type /XObject /Subtype /Form /FormType 1 "
                           f"/BBox [{rect.x0:g} {rect.y0:g} {rect.x1:g} {rect.y1:g}] "
                           f"/Resources << /Font << {resources} >> >> /Length {len(stream)} >>")
    document.update_stream(xref, stream)
    return xref


def _add(document, page, rect, name: str, runs: tuple[tuple[str, str], ...], fonts: dict[str, int]) -> int:
    import pymupdf
    annot = page.add_freetext_annot(rect, "", richtext=True, fill_color=FILL, border_color=BORDER,
                                    border_width=BORDER_WIDTH, opacity=1)
    plain = "".join(text for _family, text in runs)
    spans = "".join(f'<span style="font-family:{family};font-size:14pt;color:#000000;">{text}</span>'
                    for family, text in runs)
    rc = ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
          'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" '
          'xfa:APIVersion="Acrobat:26.1.0" xfa:spec="2.0.2" '
          'style="font-family:Source Sans 3;font-size:14pt;color:#000000;text-align:left;">'
          f'<p dir="ltr">{spans}</p></body>')
    ap = _appearance(document, rect, fonts, runs)
    document.xref_set_key(annot.xref, "NM", _pdf_string(name))
    document.xref_set_key(annot.xref, "Contents", _pdf_string(plain))
    document.xref_set_key(annot.xref, "RC", _pdf_string(rc))
    # Acrobat's observed rich FreeText state: /C is fill, /DA and /DS carry
    # border/default color; actual rich text color remains per-run in /RC.
    document.xref_set_key(annot.xref, "C", "[1 .95 .3]")
    document.xref_set_key(annot.xref, "DA", _pdf_string(".9 .1 .1 rg /FSource 14 Tf"))
    document.xref_set_key(annot.xref, "DS", _pdf_string("font: Source Sans 3 14pt; text-align:left; color:#E61A1A"))
    document.xref_set_key(annot.xref, "BS", "<</W 2 /S /S>>")
    document.xref_set_key(annot.xref, "CA", "1")
    document.xref_set_key(annot.xref, "AP", f"<</N {ap} 0 R>>")
    return annot.xref


def create(path: Path) -> None:
    import pymupdf
    for file in FONT_FILES.values():
        if not file.is_file():
            raise RuntimeError(f"Missing static font asset: {file}")
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    fonts = {family: page.insert_font(fontname=FONT_RESOURCE[family], fontfile=str(file))
             for family, file in FONT_FILES.items()}
    _add(document, page, pymupdf.Rect(45, 60, 565, 115), "source-only", RUNS["source-only"], fonts)
    _add(document, page, pymupdf.Rect(45, 145, 565, 205), "multi-run", RUNS["multi-run"], fonts)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()


def inspect(path: Path) -> dict:
    import pymupdf
    document = pymupdf.open(path)
    result = {"annotations": []}
    for page in document:
        for annot in page.annots() or ():
            if annot.type[1] != "FreeText":
                continue
            root = annot.xref
            ap = document.xref_get_key(root, "AP")[1]
            normal = int(re.search(r"/N\s+(\d+)\s+0 R", ap).group(1))
            form = document.xref_object(normal)
            stream = document.xref_stream(normal).decode("ascii")
            fonts = []
            for resource, xref in re.findall(r"/(F\w+)\s+(\d+)\s+0 R", form):
                source = document.xref_object(int(xref))
                descendant = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0 R", source)
                child = document.xref_object(int(descendant.group(1))) if descendant else ""
                descriptor = re.search(r"/FontDescriptor\s+(\d+)\s+0 R", child)
                descriptor_source = document.xref_object(int(descriptor.group(1))) if descriptor else ""
                embedded = re.search(r"/FontFile[23]?\s+(\d+)\s+0 R", descriptor_source)
                fonts.append({"resource": resource, "xref": int(xref),
                              "base_font": re.search(r"/BaseFont\s+/([^\s]+)", source).group(1),
                              "subtype": re.search(r"/Subtype\s+/([^\s]+)", source).group(1),
                              "to_unicode": bool(re.search(r"/ToUnicode\s+\d+\s+0 R", source)),
                              "font_file_xref": int(embedded.group(1)) if embedded else None})
            name = annot.info.get("id")
            encoded_runs = re.findall(r"/(F\w+)\s+14\s+Tf\s*<([0-9A-F]+)>\s*Tj", stream)
            resource_xrefs = {font["resource"]: font["xref"] for font in fonts}
            expected_runs = [
                (FONT_RESOURCE[family], _encode_text_for_embedded_font(document, resource_xrefs[FONT_RESOURCE[family]], text)[1:-1])
                for family, text in RUNS[name]
            ]
            result["annotations"].append({"xref": root, "name": name,
                "subtype": document.xref_get_key(root, "Subtype")[1], "contents": annot.info.get("content"),
                "rc": document.xref_get_key(root, "RC")[1], "box_state": {
                    key: document.xref_get_key(root, key) for key in ("C", "DA", "DS", "BS", "CA", "IC")}, "ap": normal, "fonts": fonts,
                "ap_font_uses": re.findall(r"/(F\w+)\s+14\s+Tf", stream),
                "encoded_runs": [{"resource": resource, "hex": codes} for resource, codes in encoded_runs],
                "run_encoding_matches": encoded_runs == expected_runs,
                "text_paint_black": bool(re.search(r"re B\s*0 0 0 rg\s*BT", stream)),
                "text": page.get_text()})
    document.close()
    return result


def main(argv: list[str] | None = None) -> int:
    target = Path((argv or sys.argv[1:])[0])
    create(target)
    inspection = inspect(target)
    target.with_name("inspection.json").write_text(json.dumps(inspection, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
