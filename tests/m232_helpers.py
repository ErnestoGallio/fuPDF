"""M23.2 fixture creation and native inspection outside the GUI process."""
import json
from pathlib import Path
import sys

from tests.m231_helpers import inspect


def fixture(path):
    import pymupdf
    with pymupdf.open() as document:
        for rotation in (0, 90, 180, 270):
            page = document.new_page(width=640, height=800)
            page.set_cropbox(pymupdf.Rect(30, 40, 610, 760))
            page.insert_text((45, 45), "Selectable text for colored Highlights")
            for name, y in (("custom", 110), ("no-ap", 210), ("locked", 310), ("shared-ap", 410)):
                annot = page.add_highlight_annot(pymupdf.Rect(70, y, 250, y+16))
                annot.set_colors(stroke=(.17, .63, .81))
                annot.set_info(title="External author", content="Unicode note \u00e8 intatta",
                               subject="Highlight metadata", creationDate="D:20200101000000Z",
                               modDate="D:20200202000000Z")
                annot.update(opacity=.61)
                document.xref_set_key(annot.xref, "NM", pymupdf.get_pdf_str(name))
                document.xref_set_key(annot.xref, "PrivateData", "<</Keep (opaque metadata)>>")
                if name == "custom":
                    annot.set_popup(pymupdf.Rect(400, 450, 540, 550))
                    shared_ap = document.xref_get_key(annot.xref, "AP")[1]
                    root = annot.xref
                    reply = page.add_text_annot((300, 130), "External reply remains intact")
                    reply.set_irt_xref(root)
                    document.xref_set_key(reply.xref, "NM", pymupdf.get_pdf_str("reply"))
                if name == "shared-ap":
                    document.xref_set_key(annot.xref, "AP", shared_ap)
                if name == "no-ap":
                    document.xref_set_key(annot.xref, "AP", "null")
                    document.xref_set_key(annot.xref, "CA", "null")
                if name == "locked":
                    annot.set_flags(4 | 128)
            line = page.add_line_annot((60, 550), (200, 570))
            line.update()
            ink = page.add_ink_annot([[(60, 620), (120, 650), (230, 615)]])
            ink.update()
            page.set_rotation(rotation)
        document.save(path)


if __name__ == "__main__":
    operation, path = sys.argv[1:]
    result = {"fixture": fixture, "inspect": inspect}[operation](Path(path))
    if result is not None:
        print(json.dumps(result))
