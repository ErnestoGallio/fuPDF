"""Offline synthetic graph fixtures; NOT Adobe references or editorial encodings."""

from hashlib import sha256
import json
from pathlib import Path
import sys
import os
import tempfile

from pdf_annotator.worker.annotation_adapter import ProductAnnotationAdapter
from pdf_annotator.worker.document_service import run_document_worker


def persistence_worker(conn, document_id, source) -> None:
    """Disposable M10 export only; annotation writes use the product adapter."""
    output = Path(source.identity.canonical_path + ".m10-export.pdf")

    def factory(text_service):
        adapter = ProductAnnotationAdapter(text_service)

        def apply_and_export(document, command):
            applied = adapter(document, command)
            handle, path = tempfile.mkstemp(suffix=".pdf", dir=output.parent)
            os.close(handle)
            try:
                document.save(path)
                os.replace(path, output)
            finally:
                Path(path).unlink(missing_ok=True)
            return applied
        return apply_and_export

    run_document_worker(conn, document_id, source, mutation_adapter_factory=factory)


def failure_worker(conn, document_id, source, *, stage: str) -> None:
    """One-shot failures after real writes; export the rolled-back state too."""
    import pymupdf
    from pdf_annotator.worker import editorial_adapter as module

    output = source.identity.canonical_path + ".m10-export.pdf"
    fired = False
    original_popup, original_model = module._popup, module.AnnotationModel
    original_update = pymupdf.Document.update_object
    popup_count = 0

    def popup(*args):
        nonlocal popup_count, fired
        result = original_popup(*args)
        popup_count += 1
        threshold = 1 if stage == "strike" else 2
        if stage in ("strike", "caret") and popup_count == threshold and not fired:
            fired = True
            raise RuntimeError("Injected failure after native component creation")
        return result

    def model(*args, **kwargs):
        nonlocal fired
        if stage == "links" and not fired:
            fired = True
            raise RuntimeError("Injected failure after complete native grouping")
        return original_model(*args, **kwargs)

    def update(self, xref, text, *args, **kwargs):
        nonlocal fired
        result = original_update(self, xref, text, *args, **kwargs)
        if stage == "delete" and text == "null" and not fired:
            fired = True
            raise RuntimeError("Injected failure during component deletion")
        return result

    module._popup, module.AnnotationModel = popup, model
    pymupdf.Document.update_object = update

    def factory(text_service):
        adapter = ProductAnnotationAdapter(text_service)

        def apply(document, command):
            try:
                return adapter(document, command)
            finally:
                document.save(output)
        return apply
    try:
        run_document_worker(conn, document_id, source, mutation_adapter_factory=factory)
    finally:
        module._popup, module.AnnotationModel = original_popup, original_model
        pymupdf.Document.update_object = original_update


def create_source(path: Path, *, rotation: int = 0, crop: bool = False) -> None:
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page(width=600, height=500)
        page.insert_text((60, 80), "The quick brown fox jumps over the lazy dog.", fontsize=12)
        page.insert_text((60, 125), "This sentence contains editable words.", fontsize=12)
        # Embed the bundled MuPDF font so Acrobat/Poppler need no installed CJK font.
        page.insert_font(fontname="m10unicode", fontbuffer=pymupdf.Font("cjk").buffer)
        page.insert_text((60, 170), "Unicode: città, perché, Über, 日本語.", fontname="m10unicode", fontsize=12)
        page.insert_text((60, 220), "Multiline selection begins here.\nand finishes here.", fontsize=12)
        annotation = page.add_text_annot((470, 300), "Untouched external annotation")
        annotation.set_info(title="Fixture baseline")
        if crop:
            page.set_cropbox(pymupdf.Rect(20, 30, 580, 470))
        page.set_rotation(rotation)
        document.save(path, deflate=True)


def create_acrobat_source(path: Path) -> None:
    """Create the unannotated input for the manual Acrobat workflow."""
    import pymupdf

    root = Path(__file__).resolve().parents[1]
    font = root / "src/pdf_annotator/assets/fonts/SourceSans3-Regular.ttf"
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=640)
        page.insert_text((60, 90), "The brown rover sketches a map for a lazy comet.",
                         fontsize=15, fontname="SourceSans3", fontfile=str(font))
        document.set_metadata({"title": "Synthetic editorial test",
                               "author": "Example Fixture", "creator": "fuPDF test helper",
                               "producer": f"PyMuPDF {pymupdf.VersionBind}"})
        data = document.tobytes(deflate=True, no_new_id=True)
    with path.open("xb") as stream:
        stream.write(data)


def create_external_editorials(path: Path) -> None:
    """Independent synthetic baseline for importing an external editorial graph."""
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page(width=600, height=640)
        page.insert_text((60, 80), "The quick brown fox jumps over the lazy dog.", fontsize=12)
        brown, = page.search_for("brown", quads=True)
        lazy, = page.search_for("lazy", quads=True)
        the = page.search_for("The", quads=True)[0]
        delete = page.add_strikeout_annot(brown)
        member = page.add_strikeout_annot(lazy)
        replace = page.add_caret_annot(lazy.lr)
        insert = page.add_caret_annot(the.lr)
        for annot, name, text, color in (
            (delete, "example-delete", None, (1, 0, 0)),
            (member, "example-replace-strike", None, (1, 0, 0)),
            (replace, "example-replace", "sleepy", (1, 0, 0)),
            (insert, "example-insert", " very", (.75, .2, .75)),
        ):
            annot.set_info(title="Example Reviewer", creationDate="D:20200101000000Z")
            annot.set_colors(stroke=color)
            annot.set_flags(4)
            annot.update()
            document.xref_set_key(annot.xref, "NM", pymupdf.get_pdf_str(name))
            if text is not None:
                document.xref_set_key(annot.xref, "Contents", pymupdf.get_pdf_str(text))
                document.xref_set_key(annot.xref, "RC", pymupdf.get_pdf_str(
                    f'<body xmlns="http://www.w3.org/1999/xhtml"><p>{text}</p></body>'))
            annot.set_popup(pymupdf.Rect(600, 40, 760, 130))
            annot.set_open(False)
            document.xref_set_key(annot.popup_xref, "F", "28")
        member.set_irt_xref(replace.xref)
        document.xref_set_key(member.xref, "IT", "/StrikeOutTextEdit")
        document.xref_set_key(member.xref, "RT", "/Group")
        document.xref_set_key(replace.xref, "IT", "/Replace")
        # Keep the padded native caret centered at each selected text boundary.
        for caret, boundary in ((replace, lazy.lr), (insert, the.lr)):
            bottom = page.rect.height - boundary.y
            document.xref_set_key(caret.xref, "Rect",
                                  f"[{boundary.x - 5} {bottom - .75} {boundary.x + 5} {bottom + 7.25}]")
            document.xref_set_key(caret.xref, "RD", "[.75 .75 .75 .75]")
        # Cover an indirect /Annots array and its popup/group backlinks.
        array = document.get_new_xref()
        document.update_object(array, document.xref_get_key(page.xref, "Annots")[1])
        document.xref_set_key(page.xref, "Annots", f"{array} 0 R")
        document.save(path, deflate=True, no_new_id=True)


def inspect_state(path: Path) -> dict:
    import pymupdf
    from pdf_annotator.spikes.editorial_inspector import inspect_reference

    report = inspect_reference(path, include_streams=True)
    with pymupdf.open(path) as document:
        report["page_text"] = [page.get_text() for page in document]
        report["content_streams"] = [[sha256(document.xref_stream(xref)).hexdigest()
                                      for xref in page.get_contents()] for page in document]
        report["base_rasters"] = [sha256(page.get_pixmap(annots=False).samples).hexdigest()
                                   for page in document]
    return report


def rejection_worker(conn, document_id, source) -> None:
    """Exercise the real adapter and check the entire live PDF on rejection."""
    def factory(text_service):
        adapter = ProductAnnotationAdapter(text_service)

        def snapshot(document):
            return tuple((document.xref_object(xref),
                          sha256(document.xref_stream(xref)).hexdigest()
                          if document.xref_is_stream(xref) else None)
                         for xref in range(1, document.xref_length()))

        def checked(document, command):
            before = snapshot(document)
            try:
                return adapter(document, command)
            finally:
                if snapshot(document) != before:
                    raise AssertionError("Rejected editorial command changed the live PDF")
        return checked

    run_document_worker(conn, document_id, source, mutation_adapter_factory=factory)


def make_inspector_fixture(path: Path) -> dict:
    """Arbitrary Text/Popup/reply graph to test inspection, never a correction."""
    import pymupdf

    payload = "città, perché, Über, 日本語 & <ok> e\u0301 ①"
    with pymupdf.open() as document:
        page = document.new_page(width=500, height=400)
        page.insert_text((60, 70), "The quick brown fox jumps over the lazy dog.\n"
                         "This sentence contains editable words.")
        first = page.add_text_annot((60, 100), payload)
        first.set_info(title="Synthetic inspector fixture", subject="No Adobe evidence")
        first.set_popup(pymupdf.Rect(250, 100, 450, 250))
        root, popup = first.xref, first.popup_xref
        second = page.add_text_annot((60, 160), "synthetic relationship only")
        reply = second.xref
        document.xref_set_key(reply, "IRT", f"{root} 0 R")
        document.xref_set_key(reply, "RT", "/R")
        # Deliberately duplicate names: the inspector must keep both objects.
        for xref in (root, reply):
            document.xref_set_key(xref, "NM", pymupdf.get_pdf_str("duplicate-synthetic-name"))
        text_object = document.get_new_xref()
        document.update_object(text_object, pymupdf.get_pdf_str(payload))
        array_object = document.get_new_xref()
        document.update_object(array_object, f"[{text_object} 0 R {reply} 0 R]")
        private = document.get_new_xref()
        document.update_object(private, f"<< /Back {root} 0 R /Nested {array_object} 0 R "
                               "/Decoy (999999 0 R) >>")
        document.xref_set_key(root, "SyntheticPrivate", f"{private} 0 R")
        document.xref_set_key(root, "RC", f"{text_object} 0 R")
        document.xref_set_key(root, "State", pymupdf.get_pdf_str("Unmarked"))
        document.xref_set_key(root, "StateModel", pymupdf.get_pdf_str("Marked"))
        page.set_cropbox(pymupdf.Rect(20, 30, 480, 380))
        page.set_rotation(90)
        document.save(path, deflate=True)
    return {"root": root, "reply": reply, "popup": popup, "private": private,
            "array": array_object, "text_object": text_object, "payload": payload}


if __name__ == "__main__":
    if sys.argv[1] == "--source":
        create_source(Path(sys.argv[2]), rotation=int(sys.argv[3]) if len(sys.argv) > 3 else 0,
                      crop=len(sys.argv) > 4 and sys.argv[4] == "crop")
    elif sys.argv[1] == "--inspect":
        print(json.dumps(inspect_state(Path(sys.argv[2])), ensure_ascii=True))
    elif sys.argv[1] == "--external-editorials":
        create_external_editorials(Path(sys.argv[2]))
    elif sys.argv[1] == "--acrobat-source":
        create_acrobat_source(Path(sys.argv[2]))
    else:
        print(json.dumps(make_inspector_fixture(Path(sys.argv[1])), ensure_ascii=True))
