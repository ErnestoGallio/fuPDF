"""Launch the existing UI on a private copy for the pending M15.1 manual gate."""

from pathlib import Path
import json
import shutil
import subprocess
import sys


def main(source: Path, working: Path):
    if source.resolve() == working.resolve() or working.exists():
        raise ValueError("Supply a distinct, new working-copy path")
    working.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, working)
    inspected = subprocess.run([sys.executable, "-m", "tests.m151_helpers", "inspect", str(working)],
                                check=True, capture_output=True, text=True, timeout=60)
    profiles = {f'native:{item["page"]}:{item["xref"]}': item
                for item in json.loads(inspected.stdout)["annotations"]}
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont, QFontInfo
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.canonical_fonts import ensure_canonical_editor_fonts
    from pdf_annotator.viewer.pdf_view import _editor_font_family
    from pdf_annotator.worker import document_service
    from pdf_annotator.app.main_window import MainWindow
    from tests.m151_helpers import candidate_worker
    document_service.document_worker_main = candidate_worker
    app = QApplication.instance() or QApplication([])
    ensure_canonical_editor_fonts()
    window = MainWindow()
    window.setWindowTitle("M15.1 — verifica manuale dei profili candidati")
    window.show()
    window.open_paths([str(working.resolve())])

    attached: set[str] = set()

    def report(document_id: str, logical_id: str) -> None:
        item = profiles.get(logical_id)
        if item is None:
            return
        candidate = item["candidate"]
        operations = ", ".join(name for name, enabled in candidate.items()
                               if name not in ("selectable", "deletable", "logical_group") and enabled) or "nessuna"
        # This disposable launcher enables every structurally supported
        # candidate operation. Production remains gated and unchanged.
        message = (f'p.{item["page"] + 1} xref {item["xref"]} id={logical_id} · '
                   f'profile={item["profile"]} · candidate={operations} · '
                   f'gate={operations} · {item["reason"]}')
        print(message, flush=True)
        window.statusBar().showMessage(message)
        # Report the actual production editor families. The worker's compact
        # semantic roles are resolved to the same bundled files that the
        # canonical child Form embeds.
        view = window._widgets.get(document_id)
        detail = view.session.annotation_details.get(logical_id) if view else None
        if detail and detail.edit_rich_text:
            for run in detail.edit_rich_text.runs:
                source_family = run.font_family
                canonical_pdf_family = _editor_font_family(source_family)
                # Request Qt by its bundled canonical presentation name, not
                # by the imported PDF semantic role. A source→canonical
                # difference is therefore expected and ``exactMatch`` is only
                # a Qt fallback diagnostic, never an AP compatibility result.
                qt_requested_family = canonical_pdf_family
                ui_family = qt_requested_family
                font = QFont(ui_family)
                font.setPointSizeF(run.font_size)
                font.setBold(run.bold)
                font.setItalic(run.italic)
                info = QFontInfo(font)
                print(f"  font source_imported_family={source_family} "
                      f"canonical_pdf_family={canonical_pdf_family} "
                      f"qt_requested_family={qt_requested_family} "
                      f"qt_resolved_family={info.family()} style={info.styleName()} "
                      f"pointSize={info.pointSizeF():g} bold={info.bold()} italic={info.italic()} "
                      f"qt_exactMatch={info.exactMatch()} "
                      f"source_to_canonical_mapping={source_family != canonical_pdf_family}", flush=True)

    def attach(_document_id: str | None = None) -> None:
        for document_id, view in window._widgets.items():
            if document_id in attached:
                continue
            attached.add(document_id)
            view.inventory_annotation_selected.connect(
                lambda logical_id, key=document_id: QTimer.singleShot(0, lambda: report(key, logical_id)))

    window.manager.active_changed.connect(attach)
    window.manager.session_changed.connect(attach)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
