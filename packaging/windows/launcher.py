"""Dispatch frozen children before importing the GUI application."""

import multiprocessing


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from pdf_annotator.main import main

    raise SystemExit(main())
