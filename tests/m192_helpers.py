"""Fault injection and read-only inspection confined to PDF-owning processes."""

from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
import json
import sys


def fault_worker(conn, document_id, source, *, stage):
    import pymupdf
    from pdf_annotator.worker import document_service, managed_corrections as managed
    from pdf_annotator.worker import editorial_adapter as editorial
    from pdf_annotator.worker.annotation_adapter import ProductAnnotationAdapter
    from pdf_annotator.worker.annotation_dispatch import AnnotationCommandDispatcher
    from pdf_annotator.worker.annotation_inventory import NativeAnnotationInventory
    from pdf_annotator.domain.errors import DomainValidationError, MutationErrorCode

    fired = False
    rollback_broken = False
    armed = False

    def fail():
        nonlocal fired
        if fired:
            return
        fired = True
        if stage == "history_domain":
            raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION)
        raise RuntimeError("Injected M19.2 failure")

    original_reason = managed.add_reason
    def reason(*args, **kwargs):
        nonlocal rollback_broken
        result = original_reason(*args, **kwargs)
        if stage in ("reason", "rollback"):
            rollback_broken = stage == "rollback"
            fail()
        return result
    managed.add_reason = reason

    original_key = pymupdf.Document.xref_set_key
    def set_key(document, xref, key, value):
        result = original_key(document, xref, key, value)
        if ((stage == "manifest" and key == managed.MARKER_KEY)
                or (stage in ("irt", "rt") and key == stage.upper()
                    and document.xref_get_key(xref, "Subtype")[1] == "/Text")):
            fail()
        return result
    pymupdf.Document.xref_set_key = set_key
    original_irt = pymupdf.Annot.set_irt_xref
    def set_irt(annotation, xref):
        result = original_irt(annotation, xref)
        if stage == "irt" and annotation.type[1] == "Text":
            fail()
        return result
    pymupdf.Annot.set_irt_xref = set_irt
    original_info = editorial._info
    def info(*args, **kwargs):
        nonlocal armed
        armed = True
        result = original_info(*args, **kwargs)
        if stage == "primary":
            fail()
        return result
    editorial._info = info

    original_object = pymupdf.Document.update_object
    def update_object(document, xref, source, *args, **kwargs):
        if rollback_broken:
            raise RuntimeError("Injected rollback failure")
        result = original_object(document, xref, source, *args, **kwargs)
        if stage == "delete" and source == "null":
            fail()
        return result
    pymupdf.Document.update_object = update_object

    original_push = AnnotationCommandDispatcher._push_history
    def push(dispatcher, entry):
        original_push(dispatcher, entry)
        if stage == "history_register" and armed:
            fail()
    AnnotationCommandDispatcher._push_history = push
    original_validate = AnnotationCommandDispatcher._validate_result
    def validate(result):
        original_validate(result)
        if stage == "result" and armed:
            fail()
    AnnotationCommandDispatcher._validate_result = staticmethod(validate)
    original_prepare = NativeAnnotationInventory.prepare_delta
    def prepare(inventory, delta):
        result = original_prepare(inventory, delta)
        if stage == "inventory" and armed:
            fail()
        return result
    NativeAnnotationInventory.prepare_delta = prepare
    original_payload = document_service.result_to_payload
    def payload(result):
        value = original_payload(result)
        if stage == "serialization" and result.success and armed:
            fail()
        return value
    document_service.result_to_payload = payload

    class Adapter(ProductAnnotationAdapter):
        @contextmanager
        def atomic_mutation(self, document, command, entry=None):
            try:
                with super().atomic_mutation(document, command, entry):
                    yield
            finally:
                # Disposable test-only observation of the post-transaction
                # state, never a product save or mutation interface.
                if fired and not document.is_closed:
                    document.save(source.identity.canonical_path + ".m192-state.pdf", no_new_id=True)

        def history_after(self, document, before, applied):
            result = super().history_after(document, before, applied)
            if stage in ("history_prepare", "history_domain") and armed:
                fail()
            return result

        def undo_history(self, document, entry):
            result = super().undo_history(document, entry)
            if stage == "undo":
                fail()
            return result

        def redo_history(self, document, entry):
            result = super().redo_history(document, entry)
            if stage == "redo":
                fail()
            return result

    document_service.run_document_worker(conn, document_id, source, mutation_adapter_factory=Adapter)


def inspect(path):
    import pymupdf
    from experiments.m19.motivated_corrections import annotation_records, base_state
    from pdf_annotator.worker.annotation_inventory import NativeAnnotationInventory
    from pdf_annotator.worker.managed_corrections import collect_managed, has_marker
    with pymupdf.open(path) as document:
        records = annotation_records(document)
        inventory = NativeAnnotationInventory(document)
        summaries = []
        cursor = 0
        while True:
            batch = inventory.summaries(cursor)
            summaries.extend(batch["summaries"])
            if batch["complete"]:
                break
            cursor = batch["next_cursor"]
        groups = []
        for record in records:
            if has_marker(document, record["xref"]):
                from pdf_annotator.domain.errors import DomainValidationError
                try:
                    group = collect_managed(document, record["page_index"], record["xref"])
                    groups.append({"kind": group.kind, "root": group.root,
                                   "replies": group.replies, "components": group.components})
                except DomainValidationError:
                    groups.append({"root": record["xref"], "invalid": True})
        return {"annotations": [{k: v for k, v in record.items() if k != "object"} for record in records],
                "base": base_state(document), "summaries": summaries, "groups": groups}


def create_source(path):
    """Unannotated, synthetic source for the production-command Acrobat gate."""
    import pymupdf
    cases = (("Replace", "La relazione sono chiara."),
             ("Delete", "Questo esempio è molto molto chiaro."),
             ("Insert", "La relazione chiara."))
    with pymupdf.open() as document:
        document.set_metadata({"title": "fuPDF M19.2 - Comandi applicativi", "author": "fuPDF test fixture"})
        for number, (kind, sentence) in enumerate(cases, 1):
            page = document.new_page(width=595, height=600)
            page.insert_text((48, 52), "fuPDF / M19.2", fontsize=12, color=(.2, .3, .45))
            page.insert_text((48, 96), f"Caso {number} - {kind} Text", fontsize=23)
            page.insert_text((48, 133), "Creazione atomica tramite comandi applicativi", fontsize=11)
            page.draw_line((48, 156), (547, 156), color=(.75, .8, .85), width=.6)
            page.insert_text((48, 205), "TESTO ORIGINALE DA CORREGGERE", fontsize=9)
            page.insert_text((48, 244), sentence, fontsize=17)
            page.insert_text((48, 325), "Aprire Commenti in Acrobat ed espandere la correzione.", fontsize=11)
            page.insert_text((48, 346), "Il payload editoriale e la motivazione devono restare distinti.", fontsize=11)
            page.insert_text((48, 399), "Acrobat: collaudo manuale ancora da eseguire.", fontsize=10)
            page.insert_text((48, 419), "Nessuna funzione IA eseguita. Nessun testo originale modificato.", fontsize=10)
            page.insert_text((48, 558), f"Fixture sintetica / produzione Save As                     {number} / 3", fontsize=9)
        document.save(path)


def mutate(source, destination, mode):
    """Produce a private external-tool variant only after its app worker closes."""
    import pymupdf
    from pdf_annotator.worker.managed_corrections import MARKER_KEY
    with pymupdf.open(source) as document:
        root = next(xref for page in document for xref, _, _ in page.annot_xrefs()
                    if document.xref_get_key(xref, MARKER_KEY)[0] != "null")
        page = next(page for page in document if root in [x for x, _, _ in page.annot_xrefs()])
        if mode == "renumber":
            document.save(destination, garbage=4)
            return
        if mode in ("foreign", "nested", "cross_page"):
            parent = root
            if mode == "nested":
                parent = next(x for x, _, _ in page.annot_xrefs()
                              if document.xref_get_key(x, "IRT")[1] == f"{root} 0 R"
                              and document.xref_get_key(x, "RT")[1] == "/R")
            if mode == "cross_page":
                page = document[(page.number + 1) % document.page_count]
            reply = page.add_text_annot((450, 300), "External Acrobat-style reply - preserve")
            reply.set_info(title="External reviewer")
            reply.set_irt_xref(parent) if mode != "cross_page" else document.xref_set_key(reply.xref, "IRT", f"{parent} 0 R")
            document.xref_set_key(reply.xref, "RT", "/R")
        elif mode == "missing":
            document.xref_set_key(root, MARKER_KEY, "null")
        elif mode == "version":
            value = json.loads(document.xref_get_key(root, MARKER_KEY)[1])
            value["v"] = 99
            document.xref_set_key(root, MARKER_KEY, pymupdf.get_pdf_str(json.dumps(value)))
        elif mode == "multiple":
            from pdf_annotator.worker.managed_corrections import add_reason
            value = json.loads(document.xref_get_key(root, MARKER_KEY)[1])
            name = value["group"] + ":reason:123456781234423482341234567890ab"
            add_reason(document, page, root, name, "Second managed reason", "Fixture", "D:20260101000000Z")
            value["replies"].append(name)
            document.xref_set_key(root, MARKER_KEY, pymupdf.get_pdf_str(json.dumps(value)))
        elif mode == "broken_irt":
            reply = next(x for x, _, _ in page.annot_xrefs()
                         if document.xref_get_key(x, "IRT")[1] == f"{root} 0 R"
                         and document.xref_get_key(x, "RT")[1] == "/R")
            document.xref_set_key(reply, "IRT", f"{reply} 0 R")
        elif mode == "string_irt":
            reply = next(x for x, _, _ in page.annot_xrefs()
                         if document.xref_get_key(x, "IRT")[1] == f"{root} 0 R"
                         and document.xref_get_key(x, "RT")[1] == "/R")
            document.xref_set_key(reply, "IRT", pymupdf.get_pdf_str(f"{root} 0 R"))
        elif mode == "missing_popup":
            document.xref_set_key(root, "Popup", "null")
        elif mode == "duplicate":
            clone = page.add_text_annot((450, 300), "Duplicate name must never authorize deletion")
            document.xref_set_key(clone.xref, "NM", document.xref_get_key(root, "NM")[0] == "string" and
                                  pymupdf.get_pdf_str(document.xref_get_key(root, "NM")[1]))
        elif mode == "unknown_component":
            extra = page.add_rect_annot(pymupdf.Rect(400, 300, 450, 330))
            extra.set_irt_xref(root)
            document.xref_set_key(extra.xref, "RT", "/Group")
        elif mode == "locked":
            document.xref_set_key(root, "F", "128")
        elif mode == "bad_parent":
            document.xref_set_key(root, "Parent", f"{root} 0 R")
        else:
            raise ValueError("Unknown fixture variant")
        document.save(destination)


if __name__ == "__main__":
    if sys.argv[1] == "source":
        create_source(Path(sys.argv[2]))
    elif sys.argv[1] == "inspect":
        print(json.dumps(inspect(Path(sys.argv[2])), ensure_ascii=True))
    else:
        mutate(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[1])
