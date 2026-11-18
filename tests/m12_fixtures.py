"""M12 native-PDF fixture work, always isolated from the Qt test process."""

from pathlib import Path
import sys


def native(path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=500, height=400)
    page.insert_text((40, 70), "alpha beta gamma delta", fontsize=14)
    highlight = page.add_highlight_annot(page.search_for("alpha"))
    highlight.set_info(content="evidenza esterna")
    ink = page.add_ink_annot([[(70, 190), (120, 210), (180, 180)],
                              [(95, 205), (145, 230), (190, 215)]])
    ink.set_info(content="tratto")
    long_text = "Testo esterno molto lungo: città 日本語 " * 1800
    text = page.add_freetext_annot((220, 100, 440, 160), long_text)
    text.set_info(title="Autore", content=long_text)
    page.add_freetext_annot((230, 220, 430, 280), "Callout esterno",
                            callout=[(80, 210), (180, 220), (230, 220)])
    page.add_text_annot((80, 280), "Nota completa")
    page.add_underline_annot(page.search_for("beta"))
    document.save(path)
    document.close()


def appearance(path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=500, height=400)
    page.add_freetext_annot((250, 40, 450, 110), "Casella Acrobat",
                            fill_color=(1, .85, .15), border_color=(.85, .1, .15),
                            border_width=2, opacity=.65, richtext=True)
    callout = page.add_freetext_annot(
        (220, 180, 460, 330), "Callout Acrobat", fill_color=(.2, .8, .9),
        border_color=(.7, .1, .6), border_width=2, opacity=.7,
        callout=[(55, 275), (160, 255), (220, 255)], richtext=True)
    # The /Rect includes the leader; /RD gives only the text box.
    document.xref_set_key(callout.xref, "RD", "[2 80 3 4]")
    document.save(path)
    document.close()


def many(path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    for index in range(110):
        page.add_text_annot((40 + index % 8 * 45, 40 + index // 8 * 35), f"nota {index}")
    document.save(path)
    document.close()


def cross_page(path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    for index in range(3):
        page = document.new_page()
        page.insert_text((60, 80), f"pagina {index}")
        page.add_text_annot((60, 120), f"nota {index}")
        page.add_ink_annot([[(70, 170), (120, 190)]])
    document.save(path)
    document.close()


def sticky_notes(path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    for rotation in (0, 90, 180, 270):
        page = document.new_page(width=640, height=960)
        page.set_cropbox(pymupdf.Rect(30, 40, 610, 920))
        page.insert_text((60, 65), "Original page text stays unchanged.")
        note = page.add_text_annot((85, 100), "<b>città 日本語 😀</b>\nseconda riga & tè")
        note.set_info(title="<Autore>")
        # External identities must not rely on /NM uniqueness.
        document.xref_set_key(note.xref, "NM", pymupdf.get_pdf_str("duplicate"))
        long_note = page.add_text_annot((365, 100), "Testo lungo: città 日本語 😀 & <ok>\n" * 2400)
        document.xref_set_key(long_note.xref, "NM", pymupdf.get_pdf_str("duplicate"))
        empty = page.add_text_annot((85, 260), "")
        document.xref_set_key(empty.xref, "NM", "null")
        parent = page.add_text_annot((365, 260), "Nota con risposta")
        reply = page.add_text_annot((365, 350), "Risposta già inventariata")
        reply.set_irt_xref(parent.xref)
        page.add_freetext_annot((70, 430, 300, 480), "Altra annotazione")
        page.set_rotation(rotation)
    document.save(path)
    document.close()


def rollback(path: Path) -> None:
    import pymupdf
    from pdf_annotator.worker.external_delete import delete_external_annotation

    native(path)
    document = pymupdf.open(path)
    try:
        page = document[0]
        xref = next(value[0] for value in page.annot_xrefs()
                    if document.xref_get_key(value[0], "Subtype")[1] == "/FreeText")
        before = tuple(value[0] for value in page.annot_xrefs())
        del page
        original_update = pymupdf.Document.update_object
        failed = False

        def fail_once(instance, object_xref, source):
            nonlocal failed
            if instance is document and source == "null" and not failed:
                failed = True
                raise RuntimeError("simulated worker write failure")
            return original_update(instance, object_xref, source)

        pymupdf.Document.update_object = fail_once
        try:
            try:
                delete_external_annotation(document, f"native:0:{xref}")
            except RuntimeError as exc:
                if str(exc) != "simulated worker write failure":
                    raise
            else:
                raise AssertionError("expected simulated worker write failure")
            if tuple(value[0] for value in document[0].annot_xrefs()) != before:
                raise AssertionError("failed deletion changed the annotation graph")
        finally:
            pymupdf.Document.update_object = original_update
    finally:
        document.close()


if __name__ == "__main__":
    operation, output = sys.argv[1], Path(sys.argv[2])
    {"native": native, "appearance": appearance, "many": many,
     "cross-page": cross_page, "sticky-notes": sticky_notes, "rollback": rollback}[operation](output)
