"""fuPDF desktop annotator. --smoke is a source-only historical M0 probe."""

import argparse
import multiprocessing as mp
from pathlib import Path
import sys
import tempfile
from time import monotonic

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from .util.logging import configure_logging
from .worker.client import RasterBudget, WorkerClient
from .worker.protocol import Operation


class FoundationWindow(QMainWindow):
    def __init__(self, clients: list[WorkerClient] | None = None) -> None:
        super().__init__()
        self.clients = clients or []
        self._closing = False
        self.setWindowTitle("Personal PDF Annotator — M0")
        self.setCentralWidget(QLabel("M0: foundations only. No document viewer or annotation tools."))
        self.resize(620, 160)
        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.setInterval(20)
        self.shutdown_timer.timeout.connect(self._finish_close)

    def closeEvent(self, event) -> None:
        if all(client.stopped.is_set() for client in self.clients):
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            for client in self.clients:
                client.close_async()
            self.shutdown_timer.start()

    def _finish_close(self) -> None:
        if all(client.stopped.is_set() for client in self.clients):
            self.shutdown_timer.stop()
            self.close()


def main(argv: list[str] | None = None) -> int:
    mp.freeze_support()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Probe bootstrap/shutdown, then exit")
    parser.add_argument("pdf_paths", nargs="*", metavar="PDF", help="Local PDF files to open")
    args = parser.parse_args(argv)
    if args.smoke and args.pdf_paths:
        parser.error("--smoke does not accept PDF paths")
    logger = configure_logging()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if not args.smoke:
        from .app.branding import apply_branding
        from .app.canonical_fonts import ensure_canonical_editor_fonts
        from .app.main_window import MainWindow

        try:
            apply_branding(app)
            ensure_canonical_editor_fonts()
        except RuntimeError as error:
            logger.error("startup resources unavailable: %s", error)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "fuPDF", str(error))
            return 2
        logger.info("fuPDF bootstrap")
        window = MainWindow()
        window.show()
        if args.pdf_paths:
            # Use the ordinary resolver/worker path after the GUI event loop starts.
            QTimer.singleShot(0, lambda: window.open_paths(args.pdf_paths))
        result = app.exec()
        logger.info("fuPDF shutdown")
        return result
    if getattr(sys, "frozen", False):
        parser.error("--smoke is a source-only historical probe")
    logger.info("M0 bootstrap")
    from .spikes.fixtures import FixtureSpec
    with tempfile.TemporaryDirectory(prefix="pdf-annotator-m0-") as temporary:
        clients = []
        if args.smoke:
            budget = RasterBudget()
            clients = [WorkerClient(name, FixtureSpec(str(Path(temporary) / f"{name}.pdf"), name), budget)
                       for name in ("A", "B")]
            for client in clients:
                client.submit(Operation.INFO)
        window = FoundationWindow(clients)
        window.show()
        observed: set[str] = set()
        outcome = {"failed": False}
        timer = QTimer(window)
        started = monotonic()

        def check_smoke() -> None:
            for client in clients:
                delivery = client.poll()
                if delivery:
                    with delivery:
                        response = delivery.response
                        if response.success and response.payload["marker"] == client.document_id:
                            observed.add(client.document_id)
                        else:
                            outcome["failed"] = True
                if client.failure:
                    outcome["failed"] = True
            if len(observed) == 2 or outcome["failed"] or monotonic() - started > 20:
                outcome["failed"] |= len(observed) != 2
                timer.stop()
                window.close()

        if args.smoke:
            timer.timeout.connect(check_smoke)
            timer.start(20)
        result = app.exec()
        if args.smoke:
            outcome["failed"] |= any(not client.stopped.is_set() or client.failure
                                      or client.forced_termination for client in clients)
        logger.info("M0 shutdown workers=%s", len(clients))
        return 1 if outcome["failed"] else result


if __name__ == "__main__":
    raise SystemExit(main())
