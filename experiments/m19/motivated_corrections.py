"""Disposable M19 probe. No GUI, product edits, production Save or AI calls.

Run from the repository: .venv/Scripts/python -m experiments.m19.motivated_corrections
The CLI process is the sole PDF owner; all documents are opened sequentially.
Correction commands use the existing authoritative worker service and adapter.
Only experimental reply encoding lives here, outside the application package.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from typing import Any
from xml.etree import ElementTree

import pymupdf

from pdf_annotator.domain.annotation_commands import (
    CommandContext, CommandSource, CreateDeleteText, CreateInsertText, CreateReplaceText,
)
from pdf_annotator.domain.geometry import PageBounds
from pdf_annotator.domain.serialization import command_to_payload
from pdf_annotator.domain.targets import TextRangeTarget
from pdf_annotator.worker.annotation_adapter import ProductAnnotationAdapter
from pdf_annotator.worker.annotation_dispatch import AnnotationCommandDispatcher
from pdf_annotator.worker.text_service import WorkerTextService

AUTHOR = "Revisione IA"
DOCUMENT_ID = "m19-disposable-probe"


@dataclass(frozen=True)
class Case:
    label: str
    kind: str
    sentence: str
    selected: str
    occurrence: int
    payload: str | None
    reason: str
    expected_sentence: str


CASES = (
    Case("A", "replace", "La relazione sono chiara.", "sono", 0, "è",
         "Il soggetto 'La relazione' è singolare: il verbo deve concordare al singolare.",
         "La relazione è chiara."),
    Case("B", "delete", "Questo esempio è molto molto chiaro.", "molto ", 1, None,
         "La seconda occorrenza di 'molto' è una ripetizione involontaria e va eliminata.",
         "Questo esempio è molto chiaro."),
    Case("C", "insert", "La relazione chiara.", "chiara", 0, "è ",
         "Manca la copula tra il soggetto e l'aggettivo: inserire il verbo 'è'.",
         "La relazione è chiara."),
)


def require(condition: bool, message: str) -> None:
    """Keep checks active even when Python is started with -O."""
    if not condition:
        raise ValueError(message)


def key(document: Any, xref: int, name: str) -> str:
    return document.xref_get_key(xref, name)[1]


def annotation_records(document: Any) -> list[dict[str, Any]]:
    """Include Popups, which Page.annots() intentionally skips."""
    records = []
    for page in document:
        for xref, _type, _name in page.annot_xrefs():
            records.append({
                "page_index": page.number, "xref": xref,
                "keys": {name: key(document, xref, name)
                         for name in document.xref_get_keys(xref)},
                "object": document.xref_object(xref),
            })
    return records


def appearance_state(document: Any, xref: int) -> dict[str, Any]:
    appearance = int(key(document, xref, "AP/N").split()[0])
    return {"keys": {name: key(document, appearance, name)
                     for name in document.xref_get_keys(appearance)},
            "stream_sha256": sha256(document.xref_stream(appearance)).hexdigest()}


def base_state(document: Any) -> dict[str, Any]:
    """Three tiny fixture pages only; no full-document product indexing."""
    pages = []
    for page in document:
        raster = page.get_pixmap(annots=False, alpha=False)
        pages.append({
            "text": page.get_text(),
            "streams": [sha256(document.xref_stream(xref)).hexdigest()
                        for xref in page.get_contents()],
            "raster": sha256(raster.samples).hexdigest(),
            "page_keys": {name: key(document, page.xref, name)
                          for name in document.xref_get_keys(page.xref) if name != "Annots"},
        })
    return {"pages": pages, "metadata": document.metadata}


def create_source(path: Path) -> None:
    """Generate an explicit synthetic baseline, never accept a user source path."""
    with pymupdf.open() as document:
        document.set_metadata({"title": "fuPDF M19 - Correzioni con motivazione",
                               "author": "fuPDF - fixture sintetica", "creator": "M19 probe"})
        for number, case in enumerate(CASES, 1):
            page = document.new_page(width=595, height=600)
            page.insert_text((48, 52), "fuPDF / M19", fontsize=12, color=(.2, .3, .45))
            page.insert_text((48, 96), f"Caso {case.label} - {case.kind.title()} Text", fontsize=23)
            page.insert_text((48, 133), "Campione sperimentale per Adobe Acrobat", fontsize=11)
            page.draw_line((48, 156), (547, 156), color=(.75, .8, .85), width=.6)
            page.insert_text((48, 205), "TESTO ORIGINALE DA CORREGGERE", fontsize=9,
                             color=(.35, .4, .45))
            page.insert_text((48, 244), case.sentence, fontsize=17)
            page.insert_text((48, 325), "Aprire il pannello Commenti ed espandere la correzione.", fontsize=11)
            page.insert_text((48, 346), "La motivazione deve comparire come risposta di Revisione IA.", fontsize=11)
            page.insert_text((48, 399), "Il testo originale della pagina resta invariato.", fontsize=10)
            page.insert_text((48, 419), "Compatibilità Acrobat: da verificare manualmente.", fontsize=10)
            page.insert_text((48, 558), f"Fixture sintetica - nessuna funzione IA eseguita       {number} / 3", fontsize=9)
        document.save(path)


def confirmed_range(service: WorkerTextService, page_index: int, case: Case) -> TextRangeTarget:
    """Fixture substring locates endpoints; M6 confirms the actual geometry/range."""
    index = service.page_index(page_index)
    characters = [character for descriptor in index.chunks
                  for character in service.chunk(page_index, index.page_text_id,
                                                  descriptor.chunk_index).characters]
    text = "".join(character.text for character in characters)
    sentence_start = text.index(case.sentence)
    sentence_end = sentence_start + len(case.sentence)
    start = sentence_start - 1
    for _ in range(case.occurrence + 1):
        start = text.index(case.selected, start + 1, sentence_end)
    offsets = [character for character in characters for _ in character.text]

    def center(character: Any) -> list[float]:
        return [sum(p.x for p in character.quad.points) / 4,
                sum(p.y for p in character.quad.points) / 4]

    status, target = service.resolve({
        "page_index": page_index, "page_text_id": index.page_text_id,
        "anchor": center(offsets[start]),
        "focus": center(offsets[start + len(case.selected) - 1]),
    })
    require(status == "confirmed" and target is not None, "Unconfirmed fixture range")
    require(target.original_text == case.selected, "Wrong fixture range")
    return target


def add_reply(document: Any, page_index: int, root_name: str, reason: str) -> int:
    """Experimental /Text reply to the logical root, never its Group/Popup child."""
    page = document[page_index]
    matches = [annotation for annotation in page.annots()
               if annotation.info["id"] == root_name]
    require(len(matches) == 1, "Missing or ambiguous correction root")
    root = matches[0]
    require(key(document, root.xref, "Subtype") in ("/Caret", "/StrikeOut")
            and key(document, root.xref, "IRT") == "null", "Reply target is not a primary correction")
    # A renderer that ignores reply threading may paint the Note icon anyway
    # (observed with bundled Poppler). Keep that icon off the source text.
    reply = page.add_text_annot((page.cropbox.width - 48, root.rect.y0), reason, icon="Note")
    stamp = datetime.now().astimezone().strftime("D:%Y%m%d%H%M%S%z")
    stamp = stamp[:-2] + "'" + stamp[-2:] + "'"
    reply.set_info(title=AUTHOR, subject="Motivazione", content=reason,
                   creationDate=stamp, modDate=stamp)
    reply.set_popup(pymupdf.Rect(290, 280, 555, 430))
    reply.set_open(False)
    reply.set_irt_xref(root.xref)  # Does not set /RT in PyMuPDF 1.28.2.
    document.xref_set_key(reply.xref, "RT", "/R")
    document.xref_set_key(reply.xref, "NM", pymupdf.get_pdf_str(root_name + ":reason"))
    # Do not update the correction root: Caret.update() would reset its geometry.
    return reply.xref


def validate_document(document: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    """Read-only checks, also usable after a manual Acrobat Save As."""
    require(not document.is_encrypted and not document.is_repaired, "Protected/repaired fixture")
    require(base_state(document) == manifest["baseline"], "Original page content changed")
    records = annotation_records(document)
    named: dict[str, dict[str, Any]] = {}
    for record in records:
        name = record["keys"].get("NM")
        if name:
            require(name not in named, "Duplicate annotation name")
            named[name] = record
    checks = []
    for case in manifest["cases"]:
        label, kind, name = case["label"], case["kind"], case["root_name"]
        root = named[name]
        reply = named[name + ":reason"]
        root_keys, reply_keys = root["keys"], reply["keys"]
        ref = f'{root["xref"]} 0 R'
        require(root["page_index"] == reply["page_index"] == case["page_index"], "Cross-page reply")
        require(root_keys.get("IRT", "null") == "null", "Root became a reply")
        require(root_keys["Subtype"] == ("/StrikeOut" if kind == "delete" else "/Caret"), "Wrong native type")
        require(root_keys.get("Contents") == case["payload"], "Correction payload changed")
        require(root_keys["T"] == AUTHOR, "Wrong correction author")
        if case["payload"] is not None:
            require("".join(ElementTree.fromstring(root_keys["RC"]).itertext()) == case["payload"],
                    "Correction rich companion changed")
        require(reply_keys["Subtype"] == "/Text" and reply_keys["IRT"] == ref
                and reply_keys["RT"] == "/R", "Wrong reply type or parent")
        require(reply_keys["Contents"] == case["reason"] and reply_keys["T"] == AUTHOR,
                "Reason/author changed")
        require(reply_keys.get("RC", "null") == "null", "Unexpected rich reply")
        require(reply_keys["CreationDate"].startswith("D:") and reply_keys["M"].startswith("D:"),
                "Missing reply timestamps")
        members = [r for r in records if r["keys"].get("IRT") == ref and r["keys"].get("RT") == "/Group"]
        require(len(members) == (1 if kind == "replace" else 0), "Broken correction grouping")
        correction = [root]
        if kind == "replace":
            strike = named[name + ":strike"]
            require(members == [strike] and strike["page_index"] == root["page_index"], "Wrong replacement member")
            require(root_keys.get("IT") == "/Replace" and strike["keys"]["IT"] == "/StrikeOutTextEdit"
                    and strike["keys"]["Subtype"] == "/StrikeOut", "Replacement intents changed")
            require(strike["keys"].get("Contents") is None, "Reason leaked into StrikeOut")
            correction.append(strike)
        else:
            require(root_keys.get("IT", "null") == "null", "Unexpected intent")
        # Exact geometry and metadata compare to the native pre-reply profile.
        for component in correction:
            expected = case["native_profile"][component["keys"]["NM"]]
            differences = {k: (expected.get(k), component["keys"].get(k))
                           for k in expected.keys() | component["keys"].keys()
                           if expected.get(k) != component["keys"].get(k)}
            require(not differences, f"Native correction profile changed: {differences}")
            require(appearance_state(document, component["xref"]) ==
                    case["native_appearances"][component["keys"]["NM"]],
                    "Native correction appearance changed")
        for parent in (*correction, reply):
            popup_ref = parent["keys"]["Popup"]
            popups = [r for r in records if f'{r["xref"]} 0 R' == popup_ref]
            require(len(popups) == 1, "Missing popup")
            popup = popups[0]
            require(popup["page_index"] == parent["page_index"]
                    and popup["keys"]["Subtype"] == "/Popup"
                    and popup["keys"]["Parent"] == f'{parent["xref"]} 0 R', "Wrong popup parent")
        incoming = [r for r in records if r["keys"].get("IRT") == ref]
        require(len(incoming) == (2 if kind == "replace" else 1), "Unexpected reply/group member")
        checks.append({"case": label, "root_xref": root["xref"], "reply_xref": reply["xref"],
                       "group_xrefs": [r["xref"] for r in members],
                       "IRT": reply_keys["IRT"], "RT": reply_keys["RT"], "status": "PASS"})
    require(len(records) == 14, "Unexpected or orphaned annotation objects")
    return {"checks": checks, "annotation_objects": records, "base_content_unchanged": True}


def run_probe(output_dir: Path) -> dict[str, Any]:
    require(pymupdf.VersionBind == "1.28.2", "Use the pinned PyMuPDF 1.28.2; do not upgrade")
    # Refuse all existing directories to avoid overwriting prior evidence/user files.
    output_dir.mkdir(parents=True, exist_ok=False)
    source = output_dir / "m19_source.pdf"
    output = output_dir / "m19_motivated_corrections.pdf"
    resaved = output_dir / "m19_pymupdf_resaved.pdf"
    create_source(source)
    source_hash = sha256(source.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {"pymupdf": pymupdf.VersionBind, "source_sha256": source_hash,
                                "acrobat": "NOT RUN", "indesign": "NOT RUN", "cases": []}
    with TemporaryDirectory(prefix="m19-", dir=output_dir) as directory:
        working = Path(directory) / "copy.pdf"
        copyfile(source, working)
        with pymupdf.open(working) as document:
            manifest["baseline"] = base_state(document)
            text_service = WorkerTextService(DOCUMENT_ID, 1, document)
            adapter = ProductAnnotationAdapter(text_service)
            dispatcher = AnnotationCommandDispatcher(DOCUMENT_ID, 1,
                lambda number: PageBounds(document[number].cropbox.width, document[number].cropbox.height),
                adapter)
            for page_index, case in enumerate(CASES):
                target = confirmed_range(text_service, page_index, case)
                context = CommandContext.create(DOCUMENT_ID, 1, CommandSource.INTERNAL)
                if case.kind == "replace":
                    command = CreateReplaceText(context, target, case.payload)
                elif case.kind == "delete":
                    command = CreateDeleteText(context, target)
                else:
                    status, insertion = text_service.resolve_insertion({
                        "page_index": page_index, "page_text_id": target.snapshot_id,
                        "char_index": target.start_char_index, "affinity": "before"})
                    require(status == "confirmed" and insertion is not None, "Unconfirmed insertion")
                    command = CreateInsertText(context, insertion, case.payload)
                result = dispatcher.dispatch(document, command_to_payload(command))
                require(result.success and result.annotation is not None, f"Correction failed: {result.error}")
                name = command.annotation_id.value
                page = document[page_index]
                for annotation in page.annots():
                    if annotation.info["id"] in (name, name + ":strike"):
                        # set_info(title=...) marks the Caret for resynthesis in
                        # 1.28.2; a later render can reset its carefully placed Rect.
                        # /T is metadata only: preserve the product AP/geometry.
                        document.xref_set_key(annotation.xref, "T", pymupdf.get_pdf_str(AUTHOR))
                native = {r["keys"]["NM"]: r["keys"] for r in annotation_records(document)
                          if r["keys"].get("NM") in (name, name + ":strike")}
                appearances = {annotation.info["id"]: appearance_state(document, annotation.xref)
                               for annotation in page.annots()
                               if annotation.info["id"] in native}
                record = {**asdict(case), "page_index": page_index, "root_name": name,
                          "command": command_to_payload(command), "revision": result.document_revision,
                          "native_profile": native, "native_appearances": appearances}
                manifest["cases"].append(record)
                add_reply(document, page_index, name, case.reason)
            live = validate_document(document, manifest)
            document.save(output)  # Disposable export; never production Save/Save As.
        with pymupdf.open(output) as document:
            reopened = validate_document(document, manifest)
            require(live == reopened, "Graph changed on first save/reopen")
            document.save(resaved)
        with pymupdf.open(resaved) as document:
            second = validate_document(document, manifest)
            require(reopened == second, "Graph changed on second save/reopen")
    require(sha256(source.read_bytes()).hexdigest() == source_hash, "Source file changed")
    manifest["output_sha256"] = sha256(output.read_bytes()).hexdigest()
    manifest["resaved_sha256"] = sha256(resaved.read_bytes()).hexdigest()
    manifest["automatic_checks"] = "PASS"
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "inspection.json").write_text(json.dumps(reopened, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output/pdf/m19"))
    args = parser.parse_args()
    manifest = run_probe(args.output_dir)
    print(json.dumps({"output": str((args.output_dir / "m19_motivated_corrections.pdf").resolve()),
                      "automatic_checks": manifest["automatic_checks"], "acrobat": "NOT RUN"}))


if __name__ == "__main__":
    main()
