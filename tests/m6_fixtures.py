"""Deterministic text fixtures for M6; creation happens before worker ownership."""

from pathlib import Path
import subprocess
import sys


UNICODE_TEXT = "caff\u00e8 l\u2019apostrofo \u201cvirgolette\u201d \u20ac \u03a9 \ufb01 \u6f22\u5b57"


def create_text_pdf(path: Path, *, pages: int = 6, dense: bool = False) -> None:
    import pymupdf

    with pymupdf.open() as document:
        font = pymupdf.Font("cjk")
        for index in range(pages):
            page = document.new_page(width=600, height=800)
            page.insert_font(fontname="F0", fontbuffer=font.buffer)
            if index == 0:
                page.insert_text((60, 90), "Prima riga di testo selezionabile", fontname="F0")
                page.insert_text((60, 120), "Seconda riga con spazi e punteggiatura.", fontname="F0")
                page.insert_text((60, 170), UNICODE_TEXT, fontname="F0")
            elif index == 1:
                page.insert_textbox((50, 70, 260, 300),
                                    "Colonna sinistra uno\nColonna sinistra due",
                                    fontname="F0", fontsize=12)
                page.insert_textbox((340, 70, 550, 300),
                                    "Colonna destra uno\nColonna destra due",
                                    fontname="F0", fontsize=12)
            elif index == 2:
                page.draw_rect((80, 80, 260, 180), fill=(.2, .4, .8))
            else:
                if dense:
                    for line in range(40):
                        page.insert_text(
                            (40, 45 + line * 18),
                            f"Pagina {index + 1:03d} riga {line + 1:02d} testo editoriale "
                            "con parole, spazi e punteggiatura.",
                            fontname="F0", fontsize=9)
                else:
                    page.insert_text((60, 90), f"Pagina {index + 1} testo ordinario",
                                     fontname="F0")
            if index in (3, 4, 5):
                page.set_rotation((index - 2) * 90)
        document.save(path)


def create_geometry_text_pdf(path: Path) -> None:
    import pymupdf

    with pymupdf.open() as document:
        for rotation in (0, 90, 180, 270):
            page = document.new_page(width=600, height=800)
            page.insert_text((100, 150), f"Rotazione {rotation}", fontsize=14)
            page.insert_text((300, 420), "Testo verticale", fontsize=14, rotate=90)
            page.set_cropbox(pymupdf.Rect(50, 70, 550, 730))
            page.set_rotation(rotation)
        document.save(path)


def create_text_limit_pdf(path: Path, *, lines: int = 100,
                          characters_per_line: int = 90) -> None:
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page(width=220, height=max(800, 16 + lines * 4))
        for line in range(lines):
            page.insert_text((5, 8 + line * 4),
                             "x" * characters_per_line,
                             fontsize=.65)
        document.save(path)


def quad_center(quad_payload: list[list[float]]) -> list[float]:
    return [sum(point[0] for point in quad_payload) / 4,
            sum(point[1] for point in quad_payload) / 4]


def create_in_subprocess(path: Path, *, geometry: bool = False,
                         pages: int = 6, dense: bool = False) -> None:
    mode = "geometry" if geometry else "text"
    subprocess.run([sys.executable, "-m", "tests.m6_fixtures", mode,
                    str(path), str(pages), "1" if dense else "0"], check=True, timeout=30,
                   capture_output=True)


if __name__ == "__main__":
    mode, destination = sys.argv[1], Path(sys.argv[2])
    if mode == "geometry":
        create_geometry_text_pdf(destination)
    elif mode == "limit":
        create_text_limit_pdf(destination, lines=int(sys.argv[3]),
                              characters_per_line=int(sys.argv[4]))
    else:
        create_text_pdf(destination, pages=int(sys.argv[3]),
                        dense=len(sys.argv) > 4 and sys.argv[4] == "1")
