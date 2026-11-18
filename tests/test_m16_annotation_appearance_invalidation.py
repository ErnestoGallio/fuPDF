"""Regression coverage for annotation-local native appearance invalidation."""

from pathlib import Path
import subprocess
import sys

from pdf_annotator.domain.external_edits import ExternalEditKind
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.rich_text import RichText
from pdf_annotator.domain.styles import FreeTextStyle, RgbColor
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from tests.conftest import close_window_discard, wait_until
from tests.m151_helpers import candidate_worker
from tests.test_freetext_viewer_m8 import _open
from tests.test_save_m15 import submit


def test_external_edits_keep_unrelated_native_appearances_resident(app, tmp_path, monkeypatch):
    """A native edit retires and re-requests only its own authoritative AP."""
    from pdf_annotator.app.main_window import MainWindow

    source = tmp_path / "multiple-native-appearances.pdf"
    subprocess.run([sys.executable, "-m", "tests.m151_helpers", "fixture", str(source)],
                   check=True, capture_output=True, timeout=60)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, source)
        wait_until(lambda: session.inventory_complete)
        native = tuple(summary for summary in session.annotation_inventory.values()
                       if (summary.page_index == 0 and summary.provenance == "external"
                           and summary.kind in ("text_box", "callout")
                           and summary.appearance_available))
        text_box = next(summary for summary in native
                        if summary.kind == "text_box" and summary.capabilities.movable
                        and summary.capabilities.resizable and summary.capabilities.editable_text
                        and summary.capabilities.editable_style)
        callout = next(summary for summary in native
                       if summary.kind == "callout" and summary.capabilities.movable_anchor)
        unrelated = tuple(summary for summary in native
                          if summary.logical_id not in (text_box.logical_id, callout.logical_id))[:3]
        assert len(unrelated) == 3
        wait_until(lambda: all(view._has_current_native_appearance(summary) for summary in native))
        appearance_requests: list[str] = []
        original_submit = session.worker.submit

        def record_appearance_request(operation, payload):
            if operation == Operation.ANNOTATION_APPEARANCE:
                appearance_requests.append(payload["logical_id"])
            return original_submit(operation, payload)

        monkeypatch.setattr(session.worker, "submit", record_appearance_request)

        def assert_local(target_id: str, edit: ExternalEditKind, **kwargs) -> None:
            target = session.annotation_inventory[target_id]
            old_target_entry = view.scheduler.budget.get(view._appearance_key(target), record=False)
            unrelated_entries = {
                summary.logical_id: view.scheduler.budget.get(
                    view._appearance_key(session.annotation_inventory[summary.logical_id]), record=False)
                for summary in unrelated
            }
            assert old_target_entry is not None
            assert all(entry is not None for entry in unrelated_entries.values())
            requests_before = len(appearance_requests)
            view.scheduler.active = False
            try:
                command = view.external_interaction.command(target, edit, **kwargs)
                assert submit(window.manager, command).success
                current = session.annotation_inventory[target_id]
                assert view.scheduler.budget.get(view._appearance_key(current), record=False) is None
                assert view.external_interaction.transition is None
                assert {summary.logical_id for summary in view._appearance_targets()} >= {
                    summary.logical_id for summary in unrelated}
                for summary in unrelated:
                    current_unrelated = session.annotation_inventory[summary.logical_id]
                    assert view._has_current_native_appearance(current_unrelated)
                    assert (view.scheduler.budget.get(view._appearance_key(current_unrelated), record=False)
                            is unrelated_entries[summary.logical_id])
            finally:
                view.scheduler.active = True
            wait_until(lambda: view._has_current_native_appearance(
                session.annotation_inventory[target_id]))
            new_requests = appearance_requests[requests_before:]
            assert new_requests.count(target_id) == 1
            assert not set(new_requests) & {summary.logical_id for summary in unrelated}
            assert (view.scheduler.budget.get(
                view._appearance_key(session.annotation_inventory[target_id]), record=False)
                    is not old_target_entry)

        assert_local(text_box.logical_id, ExternalEditKind.MOVE, delta=(8, 6))
        box = session.annotation_inventory[text_box.logical_id].geometry.text_rect
        assert box is not None
        assert_local(text_box.logical_id, ExternalEditKind.RESIZE,
                     rect=PdfRect(box.x0, box.y0, box.x1 + 12, box.y1 + 8))
        assert_local(text_box.logical_id, ExternalEditKind.TEXT,
                     rich_text=RichText.plain_text("M16 native AP cache", font_family="Helvetica",
                                                   font_size=10, color=RgbColor(0, 0, 0)))
        assert_local(text_box.logical_id, ExternalEditKind.STYLE,
                     style=FreeTextStyle(RgbColor(.2, .3, .4), RgbColor(.9, .95, .8), font_size=11))
        anchor = session.annotation_inventory[callout.logical_id].geometry.callout_line[0]
        assert_local(callout.logical_id, ExternalEditKind.ANCHOR,
                     anchor=PdfPoint(anchor.x + 14, anchor.y - 10))
    finally:
        close_window_discard(window)
