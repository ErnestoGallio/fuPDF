import os
from pathlib import Path
import subprocess
import sys
from shutil import copyfile
from time import monotonic

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from pdf_annotator.spikes.fixtures import FixtureSpec
from pdf_annotator.worker.client import RasterBudget, WorkerClient
from tests.m6_fixtures import create_in_subprocess


@pytest.fixture(scope="session")
def m19_samples(tmp_path_factory):
    """Generate the unchanged Acrobat probe in a separate PDF-owning process."""
    output = tmp_path_factory.mktemp("m19-samples") / "probe"
    subprocess.run([sys.executable, "-m", "experiments.m19.motivated_corrections",
                    "--output-dir", str(output)], check=True, capture_output=True, timeout=40)
    return output


@pytest.fixture(scope="session")
def acrobat_reference():
    supplied = os.environ.get("FUPDF_TEST_ACROBAT_REFERENCE")
    if supplied:
        return Path(supplied).resolve(strict=True)
    return Path(__file__).parent / "fixtures/m10/acrobat-reader-editorials.pdf"


@pytest.fixture(scope="session", params=["synthetic", "acrobat"])
def external_editorials(tmp_path_factory, request, acrobat_reference):
    """Exercise both the generated graph and the bundled Acrobat reference."""
    if request.param == "acrobat":
        assert acrobat_reference.is_file(), "The Acrobat reference PDF is missing"
        return acrobat_reference
    path = tmp_path_factory.mktemp("external-editorials") / "editorials.pdf"
    subprocess.run([sys.executable, "-m", "tests.m10_helpers", "--external-editorials", str(path)],
                   check=True, timeout=30)
    return path


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session")
def immutable_pdf_source(tmp_path_factory):
    """Create each deterministic PDF shape once, then hand tests private copies."""
    source_dir = tmp_path_factory.mktemp("immutable_pdf_sources")
    sources = {}

    def get(*, geometry: bool = False, pages: int = 6, dense: bool = False):
        key = geometry, pages, dense
        if key not in sources:
            path = source_dir / f"source-{len(sources)}.pdf"
            create_in_subprocess(path, geometry=geometry, pages=pages, dense=dense)
            sources[key] = path
        return sources[key]

    return get


@pytest.fixture
def pdf_fixture_copy(tmp_path, immutable_pdf_source):
    """Copy a generated input to a test-private path before a worker can own it."""
    def copy(destination, *, geometry: bool = False, pages: int = 6, dense: bool = False):
        copyfile(immutable_pdf_source(geometry=geometry, pages=pages, dense=dense), destination)
        return destination

    return copy


def wait_until(predicate, timeout=15.0):
    """Wait through Qt events, never by blocking the GUI on worker I/O."""
    deadline = monotonic() + timeout
    loop, timer = QEventLoop(), QTimer()
    timer.setInterval(5)

    def tick():
        if predicate() or monotonic() > deadline:
            loop.quit()

    timer.timeout.connect(tick)
    timer.start()
    if not predicate():
        loop.exec()
    timer.stop()
    assert predicate(), "Timed out waiting for asynchronous worker result"


def close_window_discard(window) -> None:
    """Close a viewer test without exercising the interactive close dialog.

    Tests that cover the close lifecycle drive Save / Discard / Cancel through
    the UI.  Historical viewer tests instead need deterministic cleanup even
    when they intentionally leave annotations or an editor dirty.
    """
    assert window.manager.request_shutdown(discard_changes=True)
    wait_until(lambda: window.manager.is_shutdown)
    window.close()


def receive(client, timeout=15.0):
    found = []

    def poll():
        if not found:
            delivery = client.poll()
            if delivery is not None:
                found.append(delivery)
        return bool(found) or client.failure is not None

    wait_until(poll, timeout)
    assert found, f"Worker failed: {client.failure}"
    return found[0]


def request(client, operation, payload=None):
    request_id = client.submit(operation, payload)
    assert request_id is not None
    with receive(client) as delivery:
        response = delivery.response
        assert response.request_id == request_id
        return response


@pytest.fixture
def clients(tmp_path, app):
    created = []
    budget = RasterBudget()

    def make(name="A", *, geometry=False, large=False, **kwargs):
        spec = FixtureSpec(str(tmp_path / f"{name}.pdf"), name, geometry, large)
        client = WorkerClient(name, spec, budget, **kwargs)
        created.append(client)
        return client

    yield make
    for client in created:
        client.close_async()
    wait_until(lambda: all(c.stopped.is_set() for c in created))
    assert budget.bytes == 0
