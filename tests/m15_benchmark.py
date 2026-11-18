"""Local M15 production-path benchmark and retained manual-gate PDFs."""

from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.domain.annotation_commands import (CommandContext, CreateCallout,
    CreateDeleteText, CreateFreeText, CreateHighlight, CreateInsertText, CreateReplaceText)
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.domain.styles import FreeTextStyle, HighlightStyle, RgbColor
from pdf_annotator.domain.targets import CalloutTarget, RectTarget
from tests.conftest import wait_until
from tests.m10_probe import range_target, insertion_target
from tests.test_m6_service_persistence import _opened
from tests.test_save_m15 import ink, inspect, save, submit


def main(directory: Path) -> None:
    app = QApplication.instance() or QApplication([])
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "synthetic-imported-source.pdf"
    subprocess.run([sys.executable, "-m", "tests.m151_fixtures", str(source)],
                   check=True, timeout=30)
    original = sha256(source.read_bytes()).hexdigest()
    before = inspect(source)
    manager = DocumentManager()
    beats = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(perf_counter()))
    worker = None
    report = {"platform": platform.platform(), "python": platform.python_version(),
              "source_sha256": original, "source_bytes": source.stat().st_size, "fixture": "synthetic",
              "acrobat": "NOT RUN", "indesign": "NOT RUN"}
    try:
        session = _opened(manager, source)
        worker = session.worker
        report["pages"] = session.page_count
        target = directory / "M15-SaveAs-profiled.pdf"
        started = perf_counter()
        timer.start()
        save(manager, session, target, overwrite=target.exists())
        timer.stop()
        report["save_as_seconds"] = round(perf_counter() - started, 3)
        report["qt_timer_max_gap_ms"] = round(max((b-a for a, b in zip(beats, beats[1:])), default=0) * 1000, 3)
        report["qt_timer_ticks"] = len(beats)
        report["same_worker"] = session.worker is worker
        report["generation"] = session.document_generation
        report["save_as_timing_ms"] = session.last_save_timing_ms
        total = session.last_save_timing_ms.get("gui_save_round_trip", 0)
        if total:
            report["save_as_phase_percent"] = {
                phase: round(value / total * 100, 2)
                for phase, value in session.last_save_timing_ms.items()
            }
        manager.request_close(session.document_id)
        wait_until(lambda: session.document_id not in manager.sessions)
        report["semantic_roundtrip"] = inspect(target) == before
        report["source_byte_unchanged"] = sha256(source.read_bytes()).hexdigest() == original
        report["result_bytes"] = target.stat().st_size

        reference = directory / "synthetic-editorial-reference.pdf"
        subprocess.run([sys.executable, "-m", "tests.m10_helpers", "--external-editorials", str(reference)],
                       check=True, timeout=30)
        session = _opened(manager, reference)
        context = lambda: CommandContext.create(session.document_id, session.document_generation)
        rich = RichText((RichTextRun("M15 città ", "Helvetica", 12, RgbColor(.1, .2, .8), bold=True),
                         RichTextRun("Unicode & < >", "Times", 12, RgbColor(.8, .1, .2), italic=True)))
        style = FreeTextStyle()
        commands = [ink(session),
                    CreateHighlight(context(), range_target(session, "brown"), HighlightStyle()),
                    CreateFreeText(context(), RectTarget(0, PdfRect(40, 280, 300, 370)), rich.plain, style, rich_text=rich),
                    CreateCallout(context(), CalloutTarget(0, PdfRect(310, 280, 560, 390), PdfPoint(280, 240)),
                                  rich.plain, style, rich_text=rich),
                    CreateDeleteText(context(), range_target(session, "brown")),
                    CreateInsertText(context(), insertion_target(session), " più 日本語 😀"),
                    CreateReplaceText(context(), range_target(session, "lazy"), "città 日本語 😀")]
        for command in commands:
            submit(manager, command)
        save(manager, session, directory / "M15-seven-tools.pdf", overwrite=True)
        # Also traverse normal Save before handing the artifact to manual gates.
        save(manager, session)
        report["seven_tools"] = len(session.annotations)
        report["seven_tools_generation"] = session.document_generation

        # Minimal production-Save artifact for the Acrobat FreeText gate. It
        # deliberately keeps the samples small and unambiguous: no imported
        # annotation is edited and every color/text profile is app-created.
        gate_source = directory / "acrobat-freetext-source.pdf"
        subprocess.run([sys.executable, "-m", "tests.m15_helpers", "gate-source", str(gate_source)],
                       check=True, timeout=30)
        session = _opened(manager, gate_source)
        context = lambda: CommandContext.create(session.document_id, session.document_generation)
        red = RgbColor(.86, .12, .12)
        blue = RgbColor(.08, .32, .86)
        black = RgbColor(0, 0, 0)
        pale_yellow = RgbColor(1, .96, .70)
        pale_blue = RgbColor(.79, .89, 1)
        rich_gate = RichText((
            RichTextRun("grassetto città ", "Helvetica", 12, blue, bold=True),
            RichTextRun("corsivo 日本語 😀", "Times", 12, red, italic=True),
        ))
        gate_commands = (
            CreateFreeText(context(), RectTarget(0, PdfRect(35, 80, 270, 125)),
                           "Text Box plain", FreeTextStyle(black, None, black)),
            CreateFreeText(context(), RectTarget(0, PdfRect(35, 145, 270, 205)),
                           rich_gate.plain, FreeTextStyle(black, pale_yellow, black),
                           rich_text=rich_gate),
            CreateFreeText(context(), RectTarget(0, PdfRect(35, 225, 270, 270)),
                           "bordo rosso", FreeTextStyle(red, pale_yellow, black)),
            CreateFreeText(context(), RectTarget(0, PdfRect(35, 290, 270, 335)),
                           "bordo blu", FreeTextStyle(blue, pale_blue, black)),
            CreateCallout(context(), CalloutTarget(0, PdfRect(325, 145, 560, 225),
                                                   PdfPoint(290, 105)),
                          rich_gate.plain, FreeTextStyle(blue, pale_yellow, black),
                          rich_text=rich_gate),
        )
        for command in gate_commands:
            submit(manager, command)
        gate_path = directory / "M15-acrobat-freetext-gate.pdf"
        save(manager, session, gate_path, overwrite=gate_path.exists())
        report["acrobat_freetext_gate"] = str(gate_path)
        report["acrobat_freetext_gate_annotations"] = len(gate_commands)
    finally:
        timer.stop()
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)
        report["workers_stopped"] = worker is not None and worker.stopped.is_set()
        report["raster_bytes_after_close"] = manager.budget.bytes
    (directory / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/m15"))
