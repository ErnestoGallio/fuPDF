"""Real application entrypoint: startup paths use normal asynchronous tabs."""

from pathlib import Path
import subprocess
import sys

import pytest
from tests.test_document_lifecycle import pdf_files


@pytest.mark.parametrize('scenario', ['empty', 'single', 'multiple', 'relative', 'missing', 'invalid', 'mixed'])
def test_startup_paths_and_shutdown(pdf_files, tmp_path, scenario):
    result = subprocess.run([sys.executable, '-m', 'tests.startup_paths_probe',
                             str(pdf_files), str(tmp_path), scenario],
                            capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'STARTUP_PATHS_PASS' in result.stdout
