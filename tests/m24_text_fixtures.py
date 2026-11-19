"""Synthetic M24 input; PDF creation runs in its own process before open."""

from pathlib import Path
import sys


def create(path: Path) -> None:
    import pymupdf

    with pymupdf.open() as document:
        font = pymupdf.Font("cjk")
        page = document.new_page(width=600, height=800)
        page.insert_font(fontname="F0", fontbuffer=font.buffer)
        page.insert_textbox((50, 60, 540, 170), "Primo paragrafo con spazi.\nSeconda riga con sillaba-\nzione e Unicode: caffè Ω ﬁ 漢字.",
                            fontname="F0", fontsize=12)
        page.insert_text((50, 210), "Secondo paragrafo distinto.", fontname="F0", fontsize=12)
        page.insert_text((50, 235), "Spazi   interni     conservati", fontname="F0", fontsize=12)
        page.add_freetext_annot((50, 260, 350, 310), "ANNOTATION_ONLY_SENTINEL", fontsize=12)
        page = document.new_page(width=600, height=800)
        # Deliberately reversed content-stream order, with repeated text.
        page.insert_textbox((340, 60, 560, 200), "Testo ripetuto\nColonna destra continua", fontsize=12)
        page.insert_textbox((50, 60, 270, 200), "Testo ripetuto\nColonna sinistra continua", fontsize=12)
        page.insert_text((50, 250), "Fine pagina prima del seguito.", fontsize=12)
        page = document.new_page(width=600, height=800)
        page.draw_rect((50, 50, 400, 400), fill=(.3, .4, .6))
        for rotation in (0, 90, 180, 270):
            page = document.new_page(width=600, height=800)
            page.insert_text((100, 150), "Geometria verificabile", fontsize=12)
            page.set_cropbox(pymupdf.Rect(50, 70, 550, 730))
            page.set_rotation(rotation)
        page = document.new_page(width=600, height=800)
        page.insert_text((50, 80), "Seguito nella pagina successiva.", fontsize=12)
        document.save(path)


if __name__ == "__main__":
    if sys.argv[1] == "--native":
        # The parent decodes this JSON as UTF-8, independently of Windows'
        # redirected-stream encoding and inherited PYTHONIOENCODING.
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        import json
        import pymupdf
        from pdf_annotator.worker.review_localization_text import base_textpage, EXTRACTION_FLAGS
        with pymupdf.open(sys.argv[2]) as doc:
            print(json.dumps([base_textpage(page, EXTRACTION_FLAGS).extractText(sort=False)
                              for page in doc], ensure_ascii=False))
    else:
        create(Path(sys.argv[1]))
