"""Fixtures are generated inside their owning worker, never in the Qt process."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FixtureSpec:
    path: str
    marker: str
    geometry_cases: bool = False
    large_page: bool = False


def create_fixture(spec: FixtureSpec) -> None:
    import pymupdf

    path = Path(spec.path)
    if path.suffix.lower() != ".pdf" or not 0 < len(spec.marker) <= 80:
        raise ValueError("Invalid disposable fixture")
    # Exclusive creation is the safety boundary: spikes never modify existing PDFs.
    with path.open("xb"):
        pass
    # PyMuPDF resolves a named Python file object to its filename. Release the
    # exclusive creation handle before save(), which may replace that filename.
    with pymupdf.open() as doc:
        cases = [(r, cropped) for cropped in (False, True) for r in (0, 90, 180, 270)]
        if not spec.geometry_cases:
            cases = [(0, False)] * (1 if spec.large_page else 5)
        for rotation, cropped in cases:
            w, h = (10000, 12000) if spec.large_page else (600, 800)
            page = doc.new_page(width=w, height=h)
            page.insert_text((100, 120), spec.marker, fontsize=12)
            page.draw_rect((100, 150, 110, 160), fill=(1, 0, 0), color=None)
            if cropped:
                page.set_cropbox(pymupdf.Rect(50, 70, 550, 730))
            page.set_rotation(rotation)
        doc.save(path)
