"""Disposable M19.2 artifact through AnnotationService + worker + production Save As.

Run: .venv/Scripts/python -m tests.m192_probe output/pdf/m192
No GUI controls, AI, locator implementation, or old M19 reply writer is used.
"""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from PySide6.QtCore import QCoreApplication

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.domain.editorial_metadata import EditorialMetadata
from pdf_annotator.domain.serialization import command_to_payload
from tests.conftest import wait_until
from tests.test_motivated_corrections_m192 import command, inspect, opened
from tests.test_save_m15 import save, submit


def main(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    output = directory / "m192_motivated_corrections.pdf"
    application = QCoreApplication.instance() or QCoreApplication([])
    manager = DocumentManager()
    manifest = []
    with TemporaryDirectory(prefix="m192-source-", dir=directory) as temporary:
        source = Path(temporary) / "source.pdf"
        subprocess.run([sys.executable, "-m", "tests.m192_helpers", "source", str(source)],
                       check=True, timeout=30)
        baseline = inspect(source)
        original_hash = sha256(source.read_bytes()).hexdigest()
        try:
            session = opened(manager, source)
            cases = (
                ("replace", "Il soggetto 'La relazione' è singolare: il verbo deve concordare al singolare."),
                ("delete", "La seconda occorrenza di 'molto' è una ripetizione involontaria e va eliminata."),
                ("insert", "Manca la copula tra il soggetto e l'aggettivo: inserire il verbo 'è'."),
            )
            for kind, reason in cases:
                cmd = command(session, kind, EditorialMetadata(reason, "Revisione IA"))
                result = submit(manager, cmd)
                manifest.append({"command": command_to_payload(cmd), "revision": result.document_revision})
            assert session.document_revision == session.history_length == 3
            assert len(session.annotation_inventory) == 6
            save(manager, session, output)
            assert not session.dirty and session.saved_revision == 3
        finally:
            assert manager.request_shutdown(discard_changes=True)
            wait_until(lambda: manager.is_shutdown)
        assert sha256(source.read_bytes()).hexdigest() == original_hash
        observed = inspect(output)
        assert observed["base"] == baseline["base"]
        assert len(observed["groups"]) == 3 and len(observed["annotations"]) == 14
    (directory / "inspection.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({
        "cases": manifest, "source_sha256": original_hash,
        "output_sha256": sha256(output.read_bytes()).hexdigest(),
        "production_save_as": True, "base_content_unchanged": True,
        "acrobat": "NOT RUN", "indesign": "NOT RUN"}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output.resolve()))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
