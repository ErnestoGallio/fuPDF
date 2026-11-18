"""PyInstaller runtime hook: diagnostics for GUI and child processes."""

import sys

if sys.stderr is None:
    from pdf_annotator.util.logging import configure_logging

    configure_logging()
