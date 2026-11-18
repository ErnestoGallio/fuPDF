"""Reproducible post-M4 rendering-quality benchmark for native Windows.

Fixture generation and direct PyMuPDF comparisons run in short-lived helper
processes. The native smoke imports no PyMuPDF object into the GUI process.
"""

import argparse
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter

from .m3_benchmark import working_set

ZOOMS = (.75, 1.0, 1.1, 1.25, 1.35, 1.5, 2.0, 4.0, 8.0)
FACTORS = (1.0, 1.25, 1.5, 2.0)
DPR = 1.25
SINGLE_RASTER = 16 * 1024 * 1024


def create_fixtures(directory: Path) -> None:
    import pymupdf

    image_pixels = bytes(channel for y in range(180) for x in range(260)
                         for channel in (round(255 * x / 259), round(255 * y / 179),
                                         round(255 * (x + y) / 438)))
    image = pymupdf.Pixmap(pymupdf.csRGB, 260, 180, image_pixels, False)
    for name, tint in (("quality", (0.08, 0.16, 0.28)),
                       ("quality-alt", (0.28, 0.10, 0.08))):
        with pymupdf.open() as doc:
            for page_index in range(12):
                page = doc.new_page(width=595, height=842)
                page.insert_text((24, 32), f"Rendering quality fixture · {name} · {page_index + 1}",
                                 fontsize=13, fontname="hebo", color=tint)
                y = 53
                for size in (4, 5, 6, 7, 8, 9, 10, 12):
                    page.insert_text((24, y), f"{size} pt  Serif: Affinity fi fl — qualità caffè 0123456789",
                                     fontsize=size, fontname="tiro")
                    page.insert_text((300, y), "Sans bold italic: MmWw Il1 O0",
                                     fontsize=size, fontname="hebo" if size % 2 else "heit")
                    y += max(12, size + 4)
                page.draw_line((24, 180), (570, 180), color=(0, 0, 0), width=.1)
                page.draw_line((24, 186), (570, 186), color=(0, 0, 0), width=.2)
                page.draw_line((24, 192), (570, 192), color=(0, 0, 0), width=.4)
                for offset in range(0, 270, 15):
                    page.draw_line((24 + offset, 215), (294, 300 - offset / 4),
                                   color=(.12, .22, .55), width=.25)
                page.draw_circle((445, 258), 62, color=(.8, .15, .12), width=.35)
                page.draw_rect((350, 200, 540, 315), color=(.1, .55, .25), width=.3)
                page.insert_image((24, 330, 284, 510), pixmap=image)
                body = "\n".join(
                    f"{line + 1:02d}  Corpo 7 pt con grazie sottili, neretto e segni: àèìòù € § ¶"
                    for line in range(28))
                page.insert_textbox((310, 330, 570, 650), body, fontsize=7,
                                    fontname="tiro", lineheight=1.15)
                page.draw_rect((24, 680, 570, 810), color=tint, fill=(.965, .97, .98), width=.4)
                page.insert_textbox((36, 695, 558, 798),
                                    "Mixed content: 5 pt serif beside vectors and an RGB image.\n"
                                    "The PDF geometry must not change when raster density changes.",
                                    fontsize=5, fontname="tiro", lineheight=1.25)
            doc.save(directory / f"{name}.pdf", deflate=True)


def _qimage(pix):
    from PySide6.QtGui import QImage

    return QImage(pix.samples, pix.width, pix.height, pix.stride,
                  QImage.Format.Format_RGB888).copy()


def _render_view(page, source_scale: float, display_scale: float, *, smooth: bool = True,
                 native_clip: bool = False):
    import pymupdf
    from PySide6.QtCore import Qt

    target_max = (660, 560)
    clip = pymupdf.Rect(18, 18,
                        min(page.rect.width, 18 + target_max[0] / display_scale),
                        min(page.rect.height, 18 + target_max[1] / display_scale))
    scale = display_scale if native_clip else source_scale
    started = perf_counter()
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                          colorspace=pymupdf.csRGB, alpha=False, annots=False,
                          clip=clip if native_clip else None)
    elapsed = (perf_counter() - started) * 1000
    image = _qimage(pix)
    if not native_clip:
        x = max(0, math.floor(clip.x0 * scale) - pix.x)
        y = max(0, math.floor(clip.y0 * scale) - pix.y)
        width = min(image.width() - x, math.ceil(clip.width * scale))
        height = min(image.height() - y, math.ceil(clip.height * scale))
        image = image.copy(x, y, width, height)
    target_width = max(1, round(clip.width * display_scale))
    target_height = max(1, round(clip.height * display_scale))
    mode = (Qt.TransformationMode.SmoothTransformation if smooth else
            Qt.TransformationMode.FastTransformation)
    if image.width() != target_width or image.height() != target_height:
        image = image.scaled(target_width, target_height,
                             Qt.AspectRatioMode.IgnoreAspectRatio, mode)
    return image, elapsed, pix.stride * pix.height, (pix.width, pix.height), clip


def _sheet(images, labels):
    from PySide6.QtGui import QColor, QImage, QPainter

    cell_width = max(image.width() for image in images)
    cell_height = max(image.height() for image in images)
    sheet = QImage(cell_width * len(images), cell_height + 30, QImage.Format.Format_RGB32)
    sheet.fill(QColor("white"))
    painter = QPainter(sheet)
    for column, (image, label) in enumerate(zip(images, labels)):
        x = column * cell_width
        painter.drawImage(x, 30, image)
        painter.drawText(x + 8, 20, label)
    painter.end()
    return sheet


def render_comparisons(directory: Path) -> None:
    import pymupdf
    from PySide6.QtGui import QGuiApplication
    from pdf_annotator.worker.render import bounded_scale

    app = QGuiApplication([])
    rows = []
    with pymupdf.open(directory / "quality.pdf") as doc:
        page = doc[0]
        for zoom in ZOOMS:
            display_scale = zoom * DPR
            images = []
            for factor in FACTORS:
                requested = display_scale * factor
                scale = bounded_scale(page.rect.width, page.rect.height, requested, SINGLE_RASTER)
                timings = []
                for _ in range(5):
                    image, elapsed, byte_count, dimensions, _ = _render_view(
                        page, scale, display_scale)
                    timings.append(elapsed)
                images.append(image)
                rows.append({"zoom": zoom, "factor": factor, "requested_scale": requested,
                             "raster_scale": scale, "quality_degraded": scale < requested,
                             "bytes": byte_count, "dimensions": dimensions,
                             "render_median_ms": statistics.median(timings),
                             "render_p95_ms": sorted(timings)[-1]})
            sheet = _sheet(images, [f"{factor:g}x · {zoom * 100:g}%" for factor in FACTORS])
            assert sheet.save(str(directory / f"quality-{round(zoom * 100):03d}.png"))

        display_scale = DPR
        scale = bounded_scale(page.rect.width, page.rect.height, display_scale * 2, SINGLE_RASTER)
        smooth, *_ = _render_view(page, scale, display_scale, smooth=True)
        fast, *_ = _render_view(page, scale, display_scale, smooth=False)
        assert _sheet([smooth, fast], ["Qt SmoothPixmapTransform", "Qt fast/nearest reference"]).save(
            str(directory / "downsampling-smooth-v-fast.png"))

        clip_rows = []
        for zoom in (3.0, 4.0, 6.0, 8.0):
            display_scale = zoom * DPR
            full_scale = bounded_scale(page.rect.width, page.rect.height, display_scale, SINGLE_RASTER)
            full, full_ms, full_bytes, full_dimensions, clip = _render_view(
                page, full_scale, display_scale)
            clipped, clip_ms, clip_bytes, clip_dimensions, _ = _render_view(
                page, display_scale, display_scale, native_clip=True)
            assert _sheet([full, clipped],
                          [f"full page capped {full_scale:.2f}px/pt",
                           f"native clip {display_scale:.2f}px/pt"]).save(
                               str(directory / f"clip-{round(zoom * 100):03d}.png"))
            clip_rows.append({"zoom": zoom, "clip": tuple(clip),
                              "full": {"scale": full_scale, "ms": full_ms,
                                       "bytes": full_bytes, "dimensions": full_dimensions},
                              "clip_render": {"scale": display_scale, "ms": clip_ms,
                                              "bytes": clip_bytes, "dimensions": clip_dimensions}})
    (directory / "direct-report.json").write_text(
        json.dumps({"pymupdf": pymupdf.VersionBind, "qt_platform": app.platformName(),
                    "dpr": DPR, "single_raster": SINGLE_RASTER,
                    "samples": rows, "clip_spike": clip_rows}, indent=2), encoding="utf-8")


def native_smoke(directory: Path, supersampling: float) -> dict:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.viewer import render_scheduler

    render_scheduler.QUALITY_SUPERSAMPLING = supersampling

    app = QApplication([])
    assert sys.platform == "win32" and app.platformName() == "windows"
    window = MainWindow()
    window.resize(1280, 900)
    window.show()
    window.raise_()
    window.activateWindow()
    frames, samples = [], []
    state = {"failed": None}

    def ready(name):
        return next((view for view in window._widgets.values()
                     if Path(view.session.canonical_path).stem == name and
                     view.session.lifecycle == Lifecycle.READY), None)

    def workflow():
        window.open_paths([str(directory / "quality.pdf")])
        yield lambda: ready("quality") is not None
        quality = ready("quality")
        for zoom in ZOOMS:
            quality.set_zoom(zoom)
            yield lambda: quality.scheduler.settled
            raster = quality.scheduler.rasters[min(quality.scheduler.visible)]
            samples.append({"document": "quality", "zoom": zoom,
                            "layout": quality.session.ui.layout, "visible": quality.scheduler.visible,
                            "requested_scale": quality.scheduler._key(min(quality.scheduler.visible)).scale,
                            "raster_scale": raster.raster_scale,
                            "image": (raster.image.width(), raster.image.height()),
                            "resident": window.manager.resident_budget.bytes,
                            "gui_working_set": working_set(os.getpid()),
                            "worker_working_set": working_set(quality.session.worker.pid)})
            frames.append((f"native-{supersampling:g}x-{round(zoom * 100):03d}",
                           window.grab().toImage()))
        quality_position = (quality.horizontalScrollBar().value(), quality.verticalScrollBar().value())
        window.open_paths([str(directory / "quality-alt.pdf")])
        yield lambda: ready("quality-alt") is not None
        alternate = ready("quality-alt")
        alternate.set_layout("double")
        alternate.set_zoom(1.5)
        yield lambda: alternate.scheduler.settled
        samples.append({"document": "quality-alt", "zoom": alternate.session.ui.zoom,
                        "layout": alternate.session.ui.layout, "visible": alternate.scheduler.visible,
                        "resident": window.manager.resident_budget.bytes,
                        "gui_working_set": working_set(os.getpid()),
                        "worker_working_set": working_set(alternate.session.worker.pid)})
        window.manager.activate(quality.session.document_id)
        yield lambda: quality.scheduler.settled
        assert quality.session.ui.zoom == 8 and quality.session.ui.layout == "single"
        assert (quality.horizontalScrollBar().value(), quality.verticalScrollBar().value()) == quality_position
        frames.append((f"native-{supersampling:g}x-tab-return-800", window.grab().toImage()))

    iterator, condition = workflow(), [lambda: True]
    timer, started = QTimer(), perf_counter()

    def tick():
        try:
            if perf_counter() - started > 90:
                raise TimeoutError("quality native smoke")
            if condition[0]():
                condition[0] = next(iterator)
        except StopIteration:
            timer.stop()
            window.close()
        except Exception as exc:
            state["failed"] = repr(exc)
            timer.stop()
            window.close()

    timer.timeout.connect(tick)
    timer.start(10)
    app.exec()
    assert state["failed"] is None, state["failed"]
    assert window.manager.is_shutdown and not mp.active_children()
    for name, frame in frames:
        assert frame.save(str(directory / f"{name}.png"))
    return {"platform": app.platformName(), "quality_supersampling": supersampling,
            "samples": samples,
            "resident_peak": window.manager.resident_budget.peak_bytes,
            "cache_hits": window.manager.resident_budget.hits,
            "cache_misses": window.manager.resident_budget.misses,
            "evictions": window.manager.resident_budget.evictions,
            "children_after_close": len(mp.active_children())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--render", type=Path)
    parser.add_argument("--native-dir", type=Path)
    parser.add_argument("--supersampling", type=float, choices=(1.25, 1.5))
    args = parser.parse_args()
    if args.fixtures:
        create_fixtures(args.fixtures)
        return
    if args.render:
        render_comparisons(args.render)
        return
    if args.native_dir:
        if args.supersampling is None:
            parser.error("--native-dir requires --supersampling")
        report = native_smoke(args.native_dir, args.supersampling)
        label = f"{args.supersampling:g}".replace(".", "_")
        (args.native_dir / f"native-{label}x-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        print(f"RENDER_QUALITY_NATIVE_{label}X_PASS {args.native_dir}")
        return
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="render-quality-", dir=root)).resolve()
    subprocess.run([sys.executable, "-m", "tests.render_quality_benchmark",
                    "--fixtures", str(directory)], check=True, timeout=60)
    subprocess.run([sys.executable, "-m", "tests.render_quality_benchmark",
                    "--render", str(directory)], check=True, timeout=120)
    for factor in (1.25, 1.5):
        subprocess.run([sys.executable, "-m", "tests.render_quality_benchmark",
                        "--native-dir", str(directory), "--supersampling", str(factor)],
                       check=True, timeout=120)
    print("RENDER_QUALITY_PASS " + str(directory))


if __name__ == "__main__":
    mp.freeze_support()
    main()
