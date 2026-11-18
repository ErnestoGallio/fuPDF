"""M19.1 PDF setup/inspection in a process separate from the Qt test owner."""

import json
from pathlib import Path
import sys

LONG_REASON = "Perché: città 日本語 😀 & <ok> e\u0301\nSeconda riga della motivazione.\n" * 2200
PLAIN_NOTE = "Nota ordinaria indipendente: città & <test>"


def create(path: Path, source: Path, *, anomalies: bool = True) -> None:
    import pymupdf

    with pymupdf.open(source) as document:
        page = document[0]
        root = next(a.xref for a in page.annots() if a.type[1] == "Caret")
        strike = next(a.xref for a in page.annots() if a.type[1] == "StrikeOut")
        existing = next(a.xref for a in page.annots() if a.type[1] == "Text")

        def note(name, text="Motivazione esterna", parent=None, rt="/R", author="Autrice esterna"):
            annotation = page.add_text_annot((470, 330), text)
            annotation.set_info(title=author, subject="Oggetto esterno", creationDate="D:20260102030405Z")
            document.xref_set_key(annotation.xref, "NM", pymupdf.get_pdf_str(name))
            if parent is not None:
                document.xref_set_key(annotation.xref, "IRT", f"{parent} 0 R")
                if rt is not None:
                    document.xref_set_key(annotation.xref, "RT", rt)
            return annotation.xref

        note("long-reply", LONG_REASON, root)
        note("default-r", parent=root, rt=None)
        note("member-reply", parent=strike)
        note("nested-reply", parent=existing)
        note("ordinary", PLAIN_NOTE)
        editable = page.add_freetext_annot(pymupdf.Rect(48, 470, 250, 510), "Padre modificabile", fontsize=11)
        document.xref_set_key(editable.xref, "NM", pymupdf.get_pdf_str("editable-parent"))
        note("editable-reply", "Risposta al riquadro", editable.xref)
        if not anomalies:
            document.save(path)
            return
        note("missing", parent=999999)
        note("self", parent=1)  # Rewire below, preserving the native malformed edge.
        self_xref = next(a.xref for a in page.annots() if a.info["id"] == "self")
        document.xref_set_key(self_xref, "IRT", f"{self_xref} 0 R")
        first = note("cycle-a", parent=root)
        second = note("cycle-b", parent=first)
        document.xref_set_key(first, "IRT", f"{second} 0 R")
        note("group-note", parent=root, rt="/Group")
        note("unknown-rt", parent=root, rt="/Other")
        invalid = note("invalid-irt", parent=root)
        document.xref_set_key(invalid, "IRT", "123")
        popup = int(document.xref_get_key(root, "Popup")[1].split()[0])
        note("popup-parent", parent=popup)
        cross = note("cross-page", parent=root)
        other = document[1]
        other_root = next(a.xref for a in other.annots() if a.type[1] == "StrikeOut")
        document.xref_set_key(cross, "IRT", f"{other_root} 0 R")
        document.save(path)


def inspect(path: Path) -> dict:
    import pymupdf
    from experiments.m19.motivated_corrections import annotation_records, base_state

    with pymupdf.open(path) as document:
        records = annotation_records(document)
        return {"annotations": [{k: v for k, v in record.items() if k != "object"} for record in records],
                "base": base_state(document)}


def orphan(path: Path, source: Path) -> None:
    """Simulate an externally removed parent, outside all product mutation APIs."""
    import pymupdf

    with pymupdf.open(source) as document:
        page = document[1]
        root = next(a.xref for a in page.annots() if a.type[1] == "StrikeOut")
        popup = int(document.xref_get_key(root, "Popup")[1].split()[0])
        kept = [xref for xref, _, _ in page.annot_xrefs() if xref not in (root, popup)]
        document.xref_set_key(page.xref, "Annots", "[" + " ".join(f"{xref} 0 R" for xref in kept) + "]")
        document.update_object(root, "null")
        document.update_object(popup, "null")
        document.save(path)


if __name__ == "__main__":
    operation, path = sys.argv[1], Path(sys.argv[2])
    if operation in ("create", "clean"):
        create(path, Path(sys.argv[3]), anomalies=operation == "create")
    elif operation == "orphan":
        orphan(path, Path(sys.argv[3]))
    else:
        print(json.dumps(inspect(path), ensure_ascii=True))
