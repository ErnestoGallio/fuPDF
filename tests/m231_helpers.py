"""Synthetic native Line fixtures and PDF inspection in a separate process."""

import json
from pathlib import Path
import sys


def fixture(path):
    import pymupdf

    with pymupdf.open() as document:
        for rotation in (0, 90, 180, 270):
            page = document.new_page(width=640, height=800)
            page.set_cropbox(pymupdf.Rect(30, 40, 610, 760))
            page.insert_text((45, 45), "Native Line / Ink preservation")
            page.draw_line((60, 580), (220, 580), color=(0, 0, 0))
            for name, a, b in (
                    ("diagonal", (70, 110), (250, 200)),
                    ("horizontal", (70, 280), (250, 280)),
                    ("vertical", (330, 100), (330, 250)),
                    ("reverse", (250, 370), (70, 310)),
                    ("no-ap", (70, 430), (250, 430)),
                    ("locked", (70, 490), (250, 490))):
                annot = page.add_line_annot(a, b)
                annot.set_colors(stroke=(.1, .3, .8), fill=(.8, .9, .2))
                annot.set_border(width=.4 if name == "horizontal" else 2, dashes=[3, 2])
                annot.set_line_ends(1, 5)
                annot.set_info(title="Autore esterno", content="Linea già presente è intatta",
                               subject="Line metadata", creationDate="D:20200101000000Z",
                               modDate="D:20200202000000Z")
                annot.update(opacity=.65)
                document.xref_set_key(annot.xref, "NM", pymupdf.get_pdf_str(name))
                document.xref_set_key(annot.xref, "PrivateData", "<</Keep (native metadata)>>")
                # Caption/leader inputs and arbitrary authored AP resources are
                # deliberately retained, rather than mapped into a new writer.
                document.xref_set_key(annot.xref, "IT", "/LineDimension")
                document.xref_set_key(annot.xref, "Cap", "true")
                document.xref_set_key(annot.xref, "CO", "[2 3]")
                annot.set_flags(4 | (128 if name == "locked" else 0))
                if name == "diagonal":
                    annot.set_popup(pymupdf.Rect(400, 450, 540, 550))
                if name == "no-ap":
                    document.xref_set_key(annot.xref, "AP", "null")
            ink = page.add_ink_annot([[(60, 620), (120, 650), (230, 615)]])
            ink.set_colors(stroke=(.8, .2, .1))
            ink.update()
            document.xref_set_key(ink.xref, "NM", pymupdf.get_pdf_str("ink"))
            page.set_rotation(rotation)
        document.save(path)


def inspect(path):
    import pymupdf
    from hashlib import sha256

    with pymupdf.open(path) as document:
        rows = []
        for page in document:
            for annot in page.annots() or ():
                ap = document.xref_get_key(annot.xref, "AP")[1]
                rows.append({"page": page.number, "xref": annot.xref, "name": annot.info["id"],
                             "subtype": annot.type[1], "rect": list(annot.rect),
                             "vertices": annot.vertices,
                             "keys": {key: document.xref_get_key(annot.xref, key)
                                      for key in document.xref_get_keys(annot.xref)}, "ap": ap})
        return {"rows": rows, "text": [page.get_text() for page in document],
                "objects": {str(xref): {"source": document.xref_object(xref),
                    "stream": sha256(document.xref_stream(xref)).hexdigest()
                    if document.xref_is_stream(xref) else None}
                    for xref in range(1, document.xref_length())}}


def appearance_probe(path):
    import pymupdf
    from pdf_annotator.worker.annotation_appearance import render_annotation_appearance
    from pdf_annotator.worker.external_edit_adapter import apply_external_edit
    from pdf_annotator.domain.annotation_commands import CommandContext
    from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
    from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind

    results = []
    with pymupdf.open(path) as document:
        for index in range(4):
            for name in ("diagonal", "no-ap"):
                page = document.load_page(index)
                root = next(annot.xref for annot in page.annots() if annot.info["id"] == name)
                logical_id = f"native:{index}:{root}"
                payload = {"logical_id": logical_id, "page_index": index, "render_scale": 2}
                before_root = document.xref_object(root)
                before, pixels = render_annotation_appearance(document, payload)
                after_root = document.xref_object(root)
                bounded, _bounded_pixels = render_annotation_appearance(
                    document, {**payload, "render_scale": 800, "max_bytes": 64 * 1024})
                command = EditExternalAnnotation(CommandContext.create("probe", 1),
                    external_command_annotation_id(logical_id), logical_id, index,
                    ExternalEditKind.MOVE, delta=(12, 8))
                apply_external_edit(document, command)
                after, moved_pixels = render_annotation_appearance(document, payload)
                results.append({"page": index, "name": name, "before": before, "after": after,
                                "same_pixels": pixels == moved_pixels,
                                "read_only_render": before_root == after_root,
                                "bounded_bytes": bounded["byte_count"],
                                "visible": any(pixels[3::4])})
    return results


def failing_line_worker(conn, document_id, source):
    """One post-write failure proves the existing transaction restores /L too."""
    from pdf_annotator.worker.annotation_inventory import NativeAnnotationInventory
    from pdf_annotator.worker.document_service import run_document_worker
    original = NativeAnnotationInventory.summary_for_edit
    calls = 0

    def fail_second(self, logical_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Injected M23.1 inventory failure")
        return original(self, logical_id)

    NativeAnnotationInventory.summary_for_edit = fail_second
    run_document_worker(conn, document_id, source)


if __name__ == "__main__":
    operation, path = sys.argv[1:]
    result = {"fixture": fixture, "inspect": inspect, "appearance": appearance_probe}[operation](Path(path))
    if result is not None:
        print(json.dumps(result))
