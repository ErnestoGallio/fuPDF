"""Disposable fixture creation / audited worker targets, never imported PyMuPDF in GUI."""

import base64
from pathlib import Path
import re
import sys
import time


def create_files(directory: Path) -> None:
    import pymupdf

    for name, count in (("normal", 5), ("alternative", 5), ("third", 3), ("long", 300),
                        ("geometry", 8), ("encrypted", 1), ("owner_only", 1), ("large", 1)):
        with pymupdf.open() as doc:
            for index in range(count):
                page = doc.new_page(width=10000 if name == "large" else 600,
                                    height=12000 if name == "large" else 800)
                page.insert_text((100, 120), f"{name} page {index + 1}")
                # Distinct content landmarks verify actual page/document raster routing.
                color = ((index % 5 + 1) / 6, 0.7 if name == "alternative" else 0.2, 0.3)
                page.draw_rect(pymupdf.Rect(100, 150, 120, 170), color=color, fill=color)
                if name == "geometry":
                    if index >= 4:
                        page.set_cropbox(pymupdf.Rect(50, 70, 550, 730))
                    page.set_rotation((index % 4) * 90)
            options = {}
            if name in ("encrypted", "owner_only"):
                options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256,
                           "owner_pw": "fixture-owner", "permissions": pymupdf.PDF_PERM_PRINT,
                           "user_pw": "fixture-user" if name == "encrypted" else ""}
            doc.save(directory / f"{name}.pdf", **options)
    (directory / "invalid.pdf").write_bytes(b"%PDF-1.7\ninvalid fixture")
    (directory / "not_pdf.txt").write_text("This is not a PDF", encoding="utf-8")
    (directory / "image.pdf").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="))
    original = (directory / "normal.pdf").read_bytes()
    (directory / "repaired.pdf").write_bytes(re.sub(rb"startxref\s+\d+", b"startxref\n99999999", original))


def audited_worker(conn, document_id, source) -> None:
    import pymupdf
    from pdf_annotator.worker.document_service import document_worker_main

    def forbidden(*args, **kwargs):
        raise AssertionError("M1 must not parse page content or create raster/text caches")

    for name in ("get_pixmap", "get_text", "get_textpage", "get_displaylist", "get_drawings", "annots"):
        setattr(pymupdf.Page, name, forbidden)
    document_worker_main(conn, document_id, source)


def delayed_worker(conn, document_id, source) -> None:
    from pdf_annotator.worker.document_service import document_worker_main

    time.sleep(0.4)
    document_worker_main(conn, document_id, source)


def entrypoint_smoke(directory: Path) -> None:
    import multiprocessing as mp
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.main import main
    from pdf_annotator.worker.protocol import Operation

    app = QApplication([])
    observed = {"window": None, "workers": [], "failed": False, "rendered": set()}
    started = time.monotonic()
    timer = QTimer()

    def tick():
        if observed["window"] is None:
            observed["window"] = next(w for w in app.topLevelWidgets() if isinstance(w, MainWindow))
            observed["window"].open_paths([str(directory / "normal.pdf"), str(directory / "alternative.pdf")])
        window = observed["window"]
        sessions = tuple(window.manager.sessions.values())
        if len(sessions) == 2 and all(s.lifecycle == Lifecycle.READY for s in sessions):
            observed["workers"] = [s.worker for s in sessions]
            view = window.tabs.currentWidget()
            if 0 in view.scheduler.rasters:
                observed["rendered"].add(view.session.document_id)
                if len(observed["rendered"]) == 1:
                    window.tabs.setCurrentIndex(1 - window.tabs.currentIndex())
                else:
                    # M2 replaces the former metadata-only tab presentation.
                    window.grab().save(str(directory / "m1-window.png"))
                    timer.stop()
                    window.close()
        elif time.monotonic() - started > 15:
            observed["failed"] = True
            timer.stop()
            window.close()

    timer.timeout.connect(tick)
    timer.start(10)
    result = main([])  # execute the actual normal entry point and QApplication loop
    assert result == 0 and not observed["failed"]
    assert observed["window"].manager.is_shutdown
    assert len(observed["workers"]) == 2
    assert len({w.pid for w in observed["workers"]}) == 2
    assert len(observed["rendered"]) == 2
    assert all(0 < w.submitted_counts[Operation.RENDER] < 5 for w in observed["workers"])
    assert all(w.stopped.is_set() and not w.forced_termination for w in observed["workers"])
    assert not mp.active_children()
    print("M1_ENTRYPOINT_PASS workers=2 rendered_tabs=2 children_after_close=0")


if __name__ == "__main__":
    if sys.argv[1] == "--smoke":
        entrypoint_smoke(Path(sys.argv[2]))
    else:
        create_files(Path(sys.argv[1]))
