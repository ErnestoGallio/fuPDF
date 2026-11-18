"""M13 PDF generators, isolated from the Qt/pytest process."""

from pathlib import Path
import subprocess
import sys


UNICODE_MAPPING_TEXT = "A Straße B ﬁ C caffè D"


def create_unicode_mapping_pdf(path: Path) -> None:
    subprocess.run([sys.executable, "-m", "tests.m13_fixtures", "unicode", str(path)],
                   check=True, timeout=30, capture_output=True)


def create_dense_search_pdf(path: Path, *, lines: int, characters_per_line: int) -> None:
    subprocess.run([sys.executable, "-m", "tests.m13_fixtures", "dense", str(path),
                    str(lines), str(characters_per_line)],
                   check=True, timeout=30, capture_output=True)


def _unicode(path: Path) -> None:
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page(width=500, height=300)
        font = pymupdf.Font("cjk")
        page.insert_font(fontname="F0", fontbuffer=font.buffer)
        page.insert_text((50, 80), UNICODE_MAPPING_TEXT, fontname="F0", fontsize=14)
        document.save(path)


def _dense(path: Path, lines: int, characters_per_line: int) -> None:
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page(width=220, height=max(800, 16 + lines * 4))
        for line in range(lines):
            page.insert_text((5, 8 + line * 4), "x" * characters_per_line, fontsize=.65)
        document.save(path)


if __name__ == "__main__":
    operation, output = sys.argv[1], Path(sys.argv[2])
    if operation == "unicode":
        _unicode(output)
    elif operation == "dense":
        _dense(output, int(sys.argv[3]), int(sys.argv[4]))
    else:
        raise ValueError("Unknown M13 fixture operation")
