from hashlib import sha256
from pathlib import Path
import sys
from unicodedata import normalize

import pytest

from pdf_annotator.spikes.annotations import (CALLOUT_POINTS, FILL_COLOR,
                                            PLAIN_TEXT, plain_markup)
from pdf_annotator.worker.protocol import Operation
from .conftest import request


@pytest.mark.parametrize("callout", [False, True])
def test_native_freetext_and_callout_roundtrip(clients, callout):
    client = clients()
    baseline = request(client, Operation.INFO)
    assert baseline.success, baseline.error
    created = request(client, Operation.ANNOTATION_SPIKE, {"callout": callout})
    assert created.success, created.error
    saved = request(client, Operation.SAVE_SPIKE)
    assert saved.success, saved.error
    assert saved.payload["phases"] == ["candidate_created", "candidate_written", "validator_exited",
                                       "live_closed", "replaced", "reopened"]
    assert saved.payload["validator_pid"] != client.pid
    assert client.generation == 2
    assert saved.payload["saved_revision"] == 1
    reopened = request(client, Operation.INFO)
    assert reopened.success, reopened.error
    assert reopened.payload["path"] == client.fixture.path
    assert reopened.payload["marker"] == baseline.payload["marker"]
    assert reopened.payload["base_content_hash"] == baseline.payload["base_content_hash"]
    assert reopened.payload["page_count"] == 5
    annots = reopened.payload["annotations"]
    assert len(annots) == 1
    annot = annots[0]
    assert annot["type"][0] == 2
    assert annot["keys"]["Subtype"] == ["name", "/FreeText"]
    assert annot["text"] == PLAIN_TEXT
    # Shaping can expose the fi ligature in appearance extraction; /Contents above
    # must still preserve the exact original plain string, without normalization.
    assert "".join(normalize("NFKC", annot["appearance_text"]).split()) == "".join(PLAIN_TEXT.split())
    assert "&lt;dato&gt;" in annot["keys"]["RC"][1]
    assert annot["colors"]["stroke"] == pytest.approx(FILL_COLOR)
    assert annot["blue_pixels"] > 20
    assert annot["fill_pixels"] > 100
    assert "0 0 1 RG" in annot["appearance_strokes"]
    assert "0 0 1 rg" in annot["keys"]["DA"][1]
    assert "0 0 0 rg" in annot["appearance_fills"]  # actual user text stays black
    assert annot["rect"] == pytest.approx(created.payload["rect"])
    # Expected native appearance bounds for the fixed 1pt-border probe; the
    # callout rectangle also encloses its line/anchor, not just the text box.
    expected_rect = (99, 99, 481.5, 341.5) if callout else (199.5, 199.5, 480.5, 340.5)
    assert annot["rect"] == pytest.approx(expected_rect)
    if callout:
        assert annot["keys"]["IT"] == ["name", "/FreeTextCallout"]
        assert annot["keys"]["CL"][0] == "array"
        assert annot["keys"]["LE"] == ["name", "/OpenArrow"]
        for actual, expected in zip(annot["vertices"], CALLOUT_POINTS, strict=True):
            assert actual == pytest.approx(expected)
    else:
        assert annot["keys"]["CL"] == ["null", "null"]
        assert annot["keys"]["IT"] == ["null", "null"]
    assert annot["pymupdf_version"].startswith("1.28.")
    assert not list(Path(client.fixture.path).parent.glob(".m0-candidate-*.pdf"))


def test_plain_markup_does_not_accept_user_html():
    assert plain_markup('<b>è & "x"</b>\nnext') == '<p>&lt;b&gt;è &amp; &quot;x&quot;&lt;/b&gt;<br/>next</p>'


@pytest.mark.parametrize("failure", ["write", "candidate", "validator", "replace"])
def test_save_failures_preserve_original_and_dirty_changes(clients, failure):
    client = clients()
    assert request(client, Operation.INFO).success
    source = Path(client.fixture.path)
    original = sha256(source.read_bytes()).digest()
    assert request(client, Operation.ANNOTATION_SPIKE, {"callout": True}).success
    reply = request(client, Operation.SAVE_SPIKE, {"failure": failure})
    assert not reply.success
    assert sha256(source.read_bytes()).digest() == original
    assert reply.payload["dirty"] is True
    assert reply.payload["saved_revision"] == 0
    current = request(client, Operation.INFO)
    assert current.success, current.error
    assert current.payload["annotations"][0]["text"] == PLAIN_TEXT
    if failure == "replace":
        candidate = Path(reply.payload["recovery_path"])
        assert candidate.exists()
        assert current.payload["path"] == str(candidate)
        assert "validator_exited" in reply.payload["phases"]
        assert "live_closed" in reply.payload["phases"]
    else:
        assert reply.payload["phase"] == "before_close"
        assert current.payload["path"] == str(source)
        assert not list(source.parent.glob(".m0-candidate-*.pdf"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing semantics")
def test_real_windows_handle_blocks_replace_and_retains_recovery(clients):
    client = clients()
    assert request(client, Operation.INFO).success
    source = Path(client.fixture.path)
    original = sha256(source.read_bytes()).digest()
    assert request(client, Operation.ANNOTATION_SPIKE, {"callout": True}).success
    # The CRT read handle does not grant FILE_SHARE_DELETE. This exercises a real
    # Windows replace failure, independently of the explicit injected fault.
    with source.open("rb"):
        reply = request(client, Operation.SAVE_SPIKE)
    assert not reply.success
    assert reply.error.code == "PermissionError"
    assert reply.payload["phase"] == "replace"
    assert reply.payload["dirty"] is True
    assert sha256(source.read_bytes()).digest() == original
    candidate = Path(reply.payload["recovery_path"])
    assert candidate.is_file()
    current = request(client, Operation.INFO)
    assert current.success
    assert current.payload["path"] == str(candidate)
    assert current.payload["annotations"][0]["text"] == PLAIN_TEXT
