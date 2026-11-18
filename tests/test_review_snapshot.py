from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from pdf_annotator.domain.annotation_commands import CommandContext, DeleteExternalAnnotation, MoveInk, Redo, Undo
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
from pdf_annotator.services.review_batches import REVIEW_PDF_LIMIT, ReviewSnapshot, plan_batches
from pdf_annotator.worker import review_snapshot
from pdf_annotator.worker.protocol import Operation
from .conftest import request, wait_until
from .m21_review_fixtures import run_fixture as runner
from .test_save_m15 import _opened, ink, manager, save, submit


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.pdf"
    runner("make", path, 7)
    return path


def read(path):
    return json.loads(runner("inspect", path))


def capture(session):
    result = request(session.worker, Operation.REVIEW_SNAPSHOT)
    assert result.success, result.error
    return ReviewSnapshot(**result.payload["snapshot"])


def extract(session, snapshot, first, last, **options):
    wait_until(lambda: session.worker.pending_count == 0)
    result = request(session.worker, Operation.REVIEW_EXTRACT, {
        "snapshot_id": snapshot.snapshot_id, "first_page": first, "last_page": last, **options})
    assert result.success, result.error
    return result.payload["plan"]


def checkpoint(session):
    return (session.document_generation, session.document_revision, session.saved_revision,
            session.history_position, session.history_length, session.history_state_id,
            session.dirty, session.saving, session.can_undo, session.can_redo)


def release_extract(session, snapshot, extract_id):
    return request(session.worker, Operation.REVIEW_RELEASE_EXTRACT, {
        "snapshot_id": snapshot.snapshot_id, "extract_id": extract_id})


def test_clean_capture_and_controlled_lifecycle(manager, source):
    baseline = read(source)
    original = source.read_bytes()
    session = _opened(manager, source)
    state = checkpoint(session)
    first = capture(session)
    target = Path(first.path)
    assert first.document_id == session.document_id and first.document_generation == session.document_generation
    assert first.document_revision == 0 and first.page_count == 7
    assert first.size_bytes == target.stat().st_size and first.sha256 == sha256(target.read_bytes()).hexdigest()
    assert read(target) == baseline and checkpoint(session) == state and source.read_bytes() == original
    duplicate = request(session.worker, Operation.REVIEW_SNAPSHOT)
    assert not duplicate.success and duplicate.error.code == "REVIEW_SNAPSHOT_ACTIVE"
    assert request(session.worker, Operation.REVIEW_RELEASE, {"snapshot_id": first.snapshot_id}).success
    assert not target.exists()
    second = capture(session)
    assert first.snapshot_id != second.snapshot_id and first.sha256 == second.sha256
    assert first.path != second.path and not target.exists()
    assert manager.request_close(session.document_id)
    wait_until(lambda: session.document_id not in manager.sessions)
    assert not Path(second.path).parent.exists()


def test_unsaved_create_delete_snapshot_immutability_and_undo_redo(manager, source):
    baseline = read(source)
    original = source.read_bytes()
    session = _opened(manager, source)
    manager.start_annotation_inventory(session.document_id)
    wait_until(lambda: session.inventory_complete)
    logical_id = next(row.logical_id for row in session.annotation_inventory.values()
                      if row.page_index == 2 and row.preview == "Nota da cancellare")
    submit(manager, DeleteExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
                                              external_command_annotation_id(logical_id), logical_id, 2))
    command = ink(session)
    submit(manager, command)
    state = checkpoint(session)
    first = capture(session)
    snapshot_path = Path(first.path)
    snapshot_data = snapshot_path.read_bytes()
    assert checkpoint(session) == state and first.document_revision == 2
    recorded = read(snapshot_path)
    assert recorded["mode"] == baseline["mode"] == "/UseOutlines"
    assert recorded["attachments"] == baseline["attachments"]
    annots = recorded["pages"][0]["annotations"]
    assert any(row["NM"] == command.annotation_id.value for row in annots)
    assert any(row["Contents"] == "Nota originaria" and row in baseline["pages"][0]["annotations"] for row in annots)
    assert recorded["pages"][2]["annotations"] == []
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert session.can_redo
    redo_state = checkpoint(session)
    reused = extract(session, first, 0, 0)["batches"][0]
    assert any(row["NM"] == command.annotation_id.value for row in read(reused["path"])["pages"][0]["annotations"])
    assert checkpoint(session) == redo_state
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    assert session.can_undo and session.dirty
    assert snapshot_path.read_bytes() == snapshot_data and source.read_bytes() == original


def test_snapshot_survives_save_as_in_same_worker(manager, tmp_path, pdf_fixture_copy):
    source = pdf_fixture_copy(tmp_path / "original.pdf")
    original = source.read_bytes()
    session = _opened(manager, source)
    pid = session.worker.pid
    submit(manager, ink(session))
    first = capture(session)
    snapshot_data = Path(first.path).read_bytes()
    save(manager, session, tmp_path / "saved.pdf")
    assert session.worker.pid == pid
    assert not session.dirty and session.document_generation != first.document_generation
    assert extract(session, first, 0, 0)["snapshot"]["document_revision"] == first.document_revision
    assert Path(first.path).read_bytes() == snapshot_data and source.read_bytes() == original


def test_snapshot_includes_unsaved_edit_of_saved_annotation(manager, tmp_path, pdf_fixture_copy):
    source = pdf_fixture_copy(tmp_path / "original.pdf")
    session = _opened(manager, source)
    command = ink(session)
    submit(manager, command)
    saved = tmp_path / "saved.pdf"
    save(manager, session, saved)
    original = saved.read_bytes()

    def annotation(path):
        return next(row for row in read(path)["pages"][0]["annotations"]
                    if row["NM"] == command.annotation_id.value)

    baseline = annotation(saved)
    submit(manager, MoveInk(CommandContext.create(session.document_id, session.document_generation),
                            command.annotation_id, 0, 15, 0))
    checkpoint_before_capture = checkpoint(session)
    edited = annotation(capture(session).path)
    before = tuple(map(float, baseline["Rect"].strip("[]").split()))
    after = tuple(map(float, edited["Rect"].strip("[]").split()))
    assert after == pytest.approx((before[0] + 15, before[1], before[2] + 15, before[3]))
    assert session.dirty and checkpoint(session) == checkpoint_before_capture
    assert saved.read_bytes() == original


def test_capture_orders_pending_mutations_at_the_worker_barrier(manager, source):
    session = _opened(manager, source)
    first, later = ink(session), ink(session)
    assert manager.submit_annotation(first).request_id is not None
    snapshot_request = session.worker.submit(Operation.REVIEW_SNAPSHOT)
    assert snapshot_request is not None
    assert session.worker.submit(Operation.REVIEW_SNAPSHOT) is None  # one review operation in flight
    assert manager.submit_annotation(later).request_id is not None

    deliveries = []
    def collect():
        delivery = session.worker.poll(snapshot_request)
        if delivery is not None:
            deliveries.append(delivery)
        return bool(deliveries)

    wait_until(collect)
    with deliveries[0] as delivery:
        captured = ReviewSnapshot(**delivery.response.payload["snapshot"])
    wait_until(lambda: session.document_revision == 2)
    assert captured.document_revision == 1
    names = {row["NM"] for row in read(captured.path)["pages"][0]["annotations"]}
    assert first.annotation_id.value in names and later.annotation_id.value not in names


def test_extract_preserves_rotated_cropbox_popup_relation_and_exact_page_map(manager, source):
    session = _opened(manager, source)
    snapshot = capture(session)
    baseline = read(snapshot.path)
    batch, = extract(session, snapshot, 1, 4)["batches"]
    assert batch["batch_id"] == plan_batches(snapshot, 1, 4).batches[0].batch_id
    assert batch["target_pages"] == [1, 2, 3, 4] and batch["context_pages"] == [0, 5]
    path = Path(batch["path"])
    assert batch["sha256"] == sha256(path.read_bytes()).hexdigest()
    observed = read(path)
    assert [p["text"] for p in observed["pages"]] == [p["text"] for p in baseline["pages"][:6]]
    page = observed["pages"][0]
    assert page["rotation"] == 90 and page["cropbox"] == baseline["pages"][0]["cropbox"]
    root = next(row for row in page["annotations"] if row["Contents"] == "Nota originaria")
    reply = next(row for row in page["annotations"] if row["Contents"] == "Risposta originaria")
    assert reply["IRT"] == f"{root['xref']} 0 R" and reply["RT"] == "/R"
    assert root["NM"] == baseline["pages"][0]["annotations"][0]["NM"]
    assert sum(row["Subtype"] == "/Popup" for row in page["annotations"]) == 2
    assert observed["attachments"] == []
    runner("verify-m20", path, 1, 4)
    repeated, = extract(session, snapshot, 1, 4)["batches"]
    assert repeated["batch_id"] == batch["batch_id"] and repeated["sha256"] == batch["sha256"]
    assert repeated["path"] != batch["path"] and path.exists()
    assert repeated["extract_id"] != batch["extract_id"]


@pytest.mark.parametrize("cleanup", ["release", "close"])
def test_concurrent_extract_readers_and_independent_release(manager, source, cleanup):
    original = source.read_bytes()
    session = _opened(manager, source)
    snapshot = capture(session)
    first, = extract(session, snapshot, 0, 3)["batches"]
    first_path = Path(first["path"])
    with first_path.open("rb") as first_reader:
        before = first_reader.read()
        second, = extract(session, snapshot, 4, 6)["batches"]
        second_path = Path(second["path"])
        assert first["batch_id"] != second["batch_id"]
        assert first["extract_id"] != second["extract_id"] and first_path != second_path
        submit(manager, ink(session))
        state = checkpoint(session)
        with second_path.open("rb") as second_reader:
            first_reader.seek(0)
            assert first_reader.read() == before == first_path.read_bytes()
            assert sha256(before).hexdigest() == first["sha256"]
            assert sha256(second_reader.read()).hexdigest() == second["sha256"]
            released = release_extract(session, snapshot, "unknown")
            assert released.success and released.payload == {"released": False}
    released = release_extract(session, snapshot, first["extract_id"])
    assert released.success and released.payload == {"released": True}
    repeated = release_extract(session, snapshot, first["extract_id"])
    assert repeated.success and repeated.payload == {"released": False}
    assert not first_path.exists() and second_path.exists()
    assert sha256(second_path.read_bytes()).hexdigest() == second["sha256"]
    third, = extract(session, snapshot, 0, 3)["batches"]
    third_path = Path(third["path"])
    assert third["sha256"] == first["sha256"] and third_path.exists()
    assert checkpoint(session) == state and source.read_bytes() == original

    snapshot_path = Path(snapshot.path)
    if cleanup == "release":
        released = request(session.worker, Operation.REVIEW_RELEASE, {"snapshot_id": snapshot.snapshot_id})
        assert released.success
        new_snapshot = capture(session)
        new_batch, = extract(session, new_snapshot, 4, 6)["batches"]
        stale = release_extract(session, snapshot, new_batch["extract_id"])
        assert not stale.success and stale.error.code == "REVIEW_SNAPSHOT_UNKNOWN"
        assert Path(new_batch["path"]).exists()
    else:
        assert manager.request_close(session.document_id, discard_changes=True)
        wait_until(lambda: session.document_id not in manager.sessions)
        assert not snapshot_path.parent.exists()
    assert not snapshot_path.exists() and not second_path.exists() and not third_path.exists()


def test_adaptation_and_unsendable_single_page_use_serialized_size(manager, source):
    session = _opened(manager, source)
    snapshot = capture(session)
    # Use actual serialized sizes with a smaller cap to exercise the policy
    # without allocating oversized rasters or requiring a large fixture.
    full = extract(session, snapshot, 1, 4)["batches"][0]
    pair = extract(session, snapshot, 1, 2)["batches"][0]
    oversized = extract(session, snapshot, 1, 4, max_bytes=pair["size_bytes"])
    assert full["size_bytes"] > pair["size_bytes"]
    assert [page for row in oversized["batches"] for page in row["target_pages"]] == [1, 2, 3, 4]
    assert all(row["size_bytes"] <= pair["size_bytes"] for row in oversized["batches"])
    failure = extract(session, snapshot, 1, 4, max_bytes=1)
    assert failure["unprocessable_pages"] == [1, 2, 3, 4] and failure["batch_count"] == 4
    assert all(row["status"] == "too_large" and row["path"] is None and row["extract_id"] is None
               and not row["context_pages"]
               for row in failure["batches"])
    invalid = request(session.worker, Operation.REVIEW_EXTRACT, {"snapshot_id": snapshot.snapshot_id,
                         "first_page": 0, "last_page": 0, "max_bytes": REVIEW_PDF_LIMIT + 1})
    assert not invalid.success and invalid.error.code == "REVIEW_INVALID_BATCH"


def test_snapshot_capabilities_are_per_worker_and_reject_modified_snapshot(manager, source, tmp_path):
    first = _opened(manager, source)
    snapshot = capture(first)
    other = tmp_path / "other.pdf"
    other.write_bytes(source.read_bytes())
    second = _opened(manager, other)
    args = {"snapshot_id": snapshot.snapshot_id, "first_page": 0, "last_page": 0}
    stale = request(second.worker, Operation.REVIEW_EXTRACT, args)
    assert not stale.success and stale.error.code == "REVIEW_SNAPSHOT_UNKNOWN"
    with Path(snapshot.path).open("ab") as stream:
        stream.write(b"\n")
    changed = request(first.worker, Operation.REVIEW_EXTRACT, args)
    assert not changed.success and changed.error.code == "REVIEW_SNAPSHOT_CHANGED"


def test_capture_rejects_a_changed_backing_file_without_retiring_worker(manager, source):
    session = _opened(manager, source)
    original = source.read_bytes()
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns - 10_000_000_000))
    changed = request(session.worker, Operation.REVIEW_SNAPSHOT)
    assert not changed.success and changed.error.code == "REVIEW_SOURCE_CHANGED"
    assert request(session.worker, Operation.PAGE_GEOMETRY, {"start": 0}).success
    assert source.read_bytes() == original and not session.dirty


def test_context_is_trimmed_then_larger_targets_split_without_losing_pages(manager, tmp_path):
    image_source = tmp_path / "images.pdf"
    runner("make", image_source, 8, 60)
    session = _opened(manager, image_source)
    snapshot = capture(session)
    single, = extract(session, snapshot, 2, 2)["batches"]
    old_extract = Path(single["path"])
    plan = extract(session, snapshot, 1, 4, max_bytes=single["size_bytes"])
    assert old_extract.exists() and sha256(old_extract.read_bytes()).hexdigest() == single["sha256"]
    assert [row["target_pages"] for row in plan["batches"]] == [[1, 2], [3, 4]]
    assert not plan["unprocessable_pages"]
    assert all(row["status"] == "ready" and len(row["context_pages"]) <= 1
               for row in plan["batches"])


def test_failed_materialization_rolls_back_new_files_only(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "snapshot.pdf"
    snapshot_path.write_bytes(b"snapshot")
    store = review_snapshot.ReviewSnapshotStore(tmp_path)
    store.snapshot = ReviewSnapshot("snapshot", "doc", 1, 0, 4, "sha", 8, str(snapshot_path))
    store.snapshot_stamp = store.stamp(snapshot_path)
    attempts = []

    def write(_snapshot, first, last, path):
        attempts.append((first, last, path))
        path.write_bytes(b"x" * (last - first + 1))
        if first == 1 and last == 3:
            raise OSError("partial write")

    monkeypatch.setattr(review_snapshot, "write_extract", write)
    previous, = store.extract("snapshot", 0, 0, 2)["plan"]["batches"]
    attempts.clear()
    with pytest.raises(review_snapshot.ReviewError) as error:
        store.extract("snapshot", 0, 3, 2)
    assert error.value.code == "REVIEW_EXTRACT_FAILED"
    assert any((first, last) == (0, 1) for first, last, _ in attempts)
    assert attempts[-1][:2] == (1, 3)
    assert all(not path.exists() for _, _, path in attempts)
    assert set(tmp_path.iterdir()) == {snapshot_path, Path(previous["path"])}

    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path == Path(previous["path"]):
            raise PermissionError("file in use")
        unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        with pytest.raises(review_snapshot.ReviewError) as error:
            store.release_extract("snapshot", previous["extract_id"])
        assert error.value.code == "REVIEW_CLEANUP_FAILED"
    assert store.release_extract("snapshot", previous["extract_id"])
    assert not store.release_extract("snapshot", previous["extract_id"])
