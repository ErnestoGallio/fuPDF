"""Isolated M4.1 strategy benchmark; PyMuPDF never enters the GUI process."""

from argparse import ArgumentParser
from collections import OrderedDict
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter


def compare(directory: Path) -> dict:
    import pymupdf
    from pdf_annotator.viewer.regional_render import regions_for_rect, tile_rect

    resident_limit = 128 * 1024 * 1024
    rows = []
    with pymupdf.open(directory / "quality.pdf") as doc:
        page = doc[0]
        page_size = page.rect.width, page.rect.height
        for zoom in (4.0, 8.0):
            scale = zoom * 1.25
            viewport = 1200 / scale, 800 / scale
            maximum = max(0.0, page.rect.height - viewport[1])
            positions = [min(maximum, offset * 40 / scale)
                         for offset in range(int(maximum * scale / 40) + 1)]
            positions = tuple(dict.fromkeys((*positions, maximum)))
            traversal = positions + tuple(reversed(positions))
            for tile_px in (512, 768, 1024):
                cache: OrderedDict[tuple, int] = OrderedDict()
                cache_bytes = hits = misses = evictions = requests = 0
                timings = []
                for y in traversal:
                    clip = (0.0, y, min(page.rect.width, viewport[0]),
                            min(page.rect.height, y + viewport[1]))
                    for region in regions_for_rect(page_size, scale, clip, tile_px):
                        key = region.column, region.row, region.tile_px
                        if key in cache:
                            hits += 1
                            cache.move_to_end(key)
                            continue
                        misses += 1
                        rect = tile_rect(page_size, scale, region)
                        started = perf_counter()
                        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                              clip=pymupdf.Rect(rect), colorspace=pymupdf.csRGB,
                                              alpha=False, annots=False)
                        timings.append((perf_counter() - started) * 1000)
                        size = pix.stride * pix.height
                        requests += 1
                        while cache and cache_bytes + size > resident_limit:
                            _, old = cache.popitem(last=False)
                            cache_bytes -= old
                            evictions += 1
                        cache[key], cache_bytes = size, cache_bytes + size
                rows.append({"strategy": f"tile-{tile_px}", "zoom": zoom,
                             "requests": requests, "hits": hits, "misses": misses,
                             "evictions": evictions, "resident_bytes": cache_bytes,
                             "render_p50_ms": statistics.median(timings),
                             "render_p95_ms": sorted(timings)[max(0, int(len(timings) * .95) - 1)]})

            # Viewport clips with half-viewport vertical overscan: simple, but
            # every scroll position produces a different cache identity.
            seen: set[tuple[float, ...]] = set()
            hits = misses = requests = 0
            timings = []
            peak = 0
            for y in traversal:
                y0 = max(0.0, y - viewport[1] / 2)
                y1 = min(page.rect.height, y + viewport[1] * 1.5)
                key = tuple(round(value, 6) for value in (0.0, y0, min(page.rect.width, viewport[0]), y1))
                if key in seen:
                    hits += 1
                    continue
                misses += 1
                started = perf_counter()
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=pymupdf.Rect(key),
                                      colorspace=pymupdf.csRGB, alpha=False, annots=False)
                timings.append((perf_counter() - started) * 1000)
                peak = max(peak, pix.stride * pix.height)
                seen.add(key)
                requests += 1
            rows.append({"strategy": "viewport-overscan", "zoom": zoom,
                         "requests": requests, "hits": hits, "misses": misses,
                         "evictions": 0, "resident_bytes": peak * len(seen),
                         "render_p50_ms": statistics.median(timings),
                         "render_p95_ms": sorted(timings)[max(0, int(len(timings) * .95) - 1)]})
    return {"pymupdf": pymupdf.VersionBind, "samples": rows}


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.directory:
        report = compare(args.directory)
        (args.directory / "tile-strategy.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return
    root = Path(".m0-artifacts")
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="m41-strategy-", dir=root)).resolve()
    subprocess.run([sys.executable, "-m", "tests.render_quality_benchmark",
                    "--fixtures", str(directory)], check=True, timeout=60)
    subprocess.run([sys.executable, "-m", "tests.m41_benchmark",
                    "--directory", str(directory)], check=True, timeout=120)
    print("M41_STRATEGY_PASS " + str(directory))
    print((directory / "tile-strategy.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
