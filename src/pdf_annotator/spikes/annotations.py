"""FreeText/callout feasibility on worker-owned disposable documents only."""

from html import escape
import re
from typing import Any

PLAIN_TEXT = 'Verificare <dato> & "perché"\nSeconda riga è leggibile.'
BOX_RECT = (200, 200, 480, 340)
CALLOUT_POINTS = ((100, 100), (160, 170), (200, 230))
FILL_COLOR = (1.0, 1.0, 0.75)
BORDER_COLOR = (0.0, 0.0, 1.0)
FIXED_STYLE = "font-family:Helvetica;font-size:12pt;color:#000000;margin:0;"


def plain_markup(text: str) -> str:
    return "<p>" + escape(text, quote=True).replace("\n", "<br/>") + "</p>"


def add_probe(doc: Any, callout: bool) -> dict:
    import pymupdf

    page = doc[0]
    callout_options = ({"callout": CALLOUT_POINTS,
                        "line_end": pymupdf.PDF_ANNOT_LE_OPEN_ARROW} if callout else {})
    annot = page.add_freetext_annot(
        BOX_RECT, plain_markup(PLAIN_TEXT), richtext=True, style=FIXED_STYLE,
        fill_color=FILL_COLOR, border_color=BORDER_COLOR, border_width=1,
        **callout_options,
    )
    # 1.28.2 accepts border_color but its rich-text creation path omits /DA's
    # requested color. Set the native default appearance explicitly and regenerate
    # this NEW annotation. /RC + fixed /DS keep the actual user text black.
    border = " ".join(str(value) for value in BORDER_COLOR)
    doc.xref_set_key(annot.xref, "DA", pymupdf.get_pdf_str(f"/Helv 12 Tf {border} rg"))
    if not callout:
        # MuPDF initializes a default /CL even without callout intent. Do not
        # leave unused callout coordinates on an ordinary app-created text box.
        doc.xref_set_key(annot.xref, "CL", "null")
    annot.update()
    # In 1.28.2 set_info(content=...) removes /RC. Write only the plain /Contents
    # key using the public low-level API, retaining fixed rich-text appearance.
    doc.xref_set_key(annot.xref, "Contents", pymupdf.get_pdf_str(PLAIN_TEXT))
    xref = annot.xref
    page = doc.reload_page(page)
    return inspect_annotation(doc, page.load_annot(xref))


def inspect_annotation(doc: Any, annot: Any) -> dict:
    import pymupdf

    keys = ("Subtype", "IT", "CL", "Rect", "RD", "DA", "RC", "DS", "BS", "LE", "C")
    # Inspect the annotation as displayed on its page. In 1.28.2 the annotation-
    # only renderer can return converted RGBA despite an explicit RGB/alpha=0
    # request. The page renderer supplies the expected RGB appearance.
    appearance = annot.parent.get_pixmap(
        matrix=pymupdf.Matrix(2, 2), clip=annot.rect,
        colorspace=pymupdf.csRGB, alpha=False, annots=True,
    )
    pixels = appearance.samples
    components = appearance.n
    assert components == 3 and appearance.alpha == 0
    ap_xref = int(doc.xref_get_key(annot.xref, "AP/N")[1].split()[0])
    ap_stream = doc.xref_stream(ap_xref).decode("latin-1")
    # Evidence that independent fill/border actually reach the appearance stream,
    # rather than checking only the inputs we supplied to add_freetext_annot.
    blue = sum(1 for r, g, b in zip(pixels[0::components], pixels[1::components], pixels[2::components])
               if b > 200 and r < 50 and g < 50)
    yellow = sum(1 for r, g, b in zip(pixels[0::components], pixels[1::components], pixels[2::components])
                 if r > 240 and g > 240 and 170 < b < 220)
    return {"type": list(annot.type), "text": annot.info.get("content"),
            "appearance_text": annot.get_text(), "rect": list(annot.rect),
            "vertices": annot.vertices, "colors": annot.colors,
            "keys": {key: list(doc.xref_get_key(annot.xref, key)) for key in keys},
            "blue_pixels": blue, "fill_pixels": yellow,
            "appearance_strokes": re.findall(r"[\d.]+ [\d.]+ [\d.]+ RG", ap_stream),
            "appearance_fills": re.findall(r"[\d.]+ [\d.]+ [\d.]+ rg", ap_stream),
            "pymupdf_version": __import__("pymupdf").VersionBind}


def inspect_all(doc: Any) -> list[dict]:
    page = doc[0]
    return [inspect_annotation(doc, annot) for annot in page.annots() or ()]
