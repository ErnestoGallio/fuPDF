"""Pre-gate M15.1 artifact builder via the real command/service/save pipeline.

Run only on an input copy. The candidate-worker seam lives under tests and
does not enable unvalidated capabilities in the production application.
"""

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main(source: Path, output: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.document_manager import DocumentManager
    from pdf_annotator.domain.annotation_commands import CommandContext
    from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
    from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
    from pdf_annotator.domain.geometry import PdfRect
    from pdf_annotator.domain.rich_text import RichText
    from pdf_annotator.domain.serialization import rich_text_from_payload
    from pdf_annotator.worker import document_service
    from tests.conftest import wait_until
    from tests.m151_helpers import candidate_worker
    from tests.test_m6_service_persistence import _opened
    from tests.test_save_m15 import save, submit
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError("Use a distinct, new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    baseline_hash = sha256(source.read_bytes()).hexdigest()
    working = output.with_name(output.stem + "-working.pdf")
    if working.exists():
        raise ValueError("Working copy already exists")
    shutil.copyfile(source, working)
    inspected = subprocess.run([sys.executable, "-m", "tests.m151_helpers", "inspect", str(working)],
                                check=True, capture_output=True, timeout=60)
    records = json.loads(inspected.stdout)["annotations"]
    classification = [{key: row[key] for key in ("page", "xref", "subtype", "profile", "reason", "candidate")}
                      for row in records if row["subtype"] in ("FreeText", "Ink")]
    app = QApplication.instance() or QApplication([])
    document_service.document_worker_main = candidate_worker
    manager = DocumentManager()
    changes = []
    try:
        session = _opened(manager, working)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        imported_font = next(row for row in records if row["profile"] == "freetext-rich-v1"
                             and row["reason"].startswith("Imported text mapped"))
        text = next(row for row in records if row["profile"] == "freetext-rich-v1"
                    and row is not imported_font)
        callout = next(row for row in records if row["profile"] == "callout-rich-v1")
        ink = next(row for row in records if row["profile"] == "ink-translation-v1")
        rich = rich_text_from_payload(text["rich_text"])
        rich = RichText((*rich.runs[:-1], replace(rich.runs[-1], text=rich.runs[-1].text + " M15.1")))
        box = callout["box"]
        for row, edit, kwargs in (
            (text, ExternalEditKind.TEXT, {"rich_text": rich}),
            (callout, ExternalEditKind.RESIZE, {"rect": PdfRect(box[0], box[1], box[2], box[3]+25)}),
            (ink, ExternalEditKind.MOVE, {"delta": (8, 6)}),
            (imported_font, ExternalEditKind.MOVE, {"delta": (5, 4)}),
        ):
            logical_id = f'native:{row["page"]}:{row["xref"]}'
            cmd = EditExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
                    external_command_annotation_id(logical_id), logical_id, row["page"], edit, **kwargs)
            submit(manager, cmd)
            changes.append({"page": row["page"]+1, "xref": row["xref"], "operation": edit.value,
                            "profile": row["profile"]})
        save(manager, session, output)
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)
    assert sha256(source.read_bytes()).hexdigest() == baseline_hash
    manifest = {"source": str(source.resolve()), "source_sha256": baseline_hash,
                "output": str(output.resolve()), "output_sha256": sha256(output.read_bytes()).hexdigest(),
                "acrobat_gate": "NOT RUN", "changes": changes, "classification": classification}
    output.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "changes": changes}))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
