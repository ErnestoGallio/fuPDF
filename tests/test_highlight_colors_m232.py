"""Focused M23.2 checks through production commands, worker, history and save."""
import json
import shutil
import subprocess
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.domain.annotation_commands import CommandContext, CreateHighlight, Undo, Redo, DeleteAnnotation
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id
from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
from pdf_annotator.domain.serialization import command_to_payload, command_from_payload
from pdf_annotator.domain.styles import HIGHLIGHT_COLOR_PRESETS, HighlightStyle, RgbColor
from tests.conftest import close_window_discard, wait_until, request
from tests.test_m6_service_persistence import _opened, _target
from tests.test_m6_viewer import _open, _drag
from tests.test_save_m15 import save, submit
from pdf_annotator.worker.protocol import Operation
from pdf_annotator.domain.serialization import result_from_payload


def helper(operation, path):
    result = subprocess.run([sys.executable, "-m", "tests.m232_helpers", operation, str(path)],
                            capture_output=True, check=True, timeout=40)
    return json.loads(result.stdout) if result.stdout else None


@pytest.fixture(scope="module")
def highlight_source(tmp_path_factory):
    path = tmp_path_factory.mktemp("m232") / "highlights.pdf"
    helper("fixture", path)
    return path


@pytest.fixture
def color_session(app, tmp_path, highlight_source):
    path = tmp_path / "highlights.pdf"
    shutil.copyfile(highlight_source, path)
    manager = DocumentManager()
    session = _opened(manager, path)
    manager.start_annotation_inventory(session.document_id)
    wait_until(lambda: session.inventory_complete)
    try:
        yield manager, session, path
    finally:
        wait_until(lambda: not session.saving, timeout=60)
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def recolor(session, logical_id, color):
    return EditExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(logical_id), logical_id, int(logical_id.split(":")[1]),
        ExternalEditKind.COLOR, color=color)


def native_row(snapshot, name, page=0):
    return next(row for row in snapshot["rows"] if row["name"] == name and row["page"] == page)


@pytest.mark.parametrize("page", range(4))
@pytest.mark.parametrize("name", ["custom", "no-ap", "shared-ap"])
def test_external_color_history_save_preserves_native_properties(color_session, tmp_path, page, name):
    manager, session, path = color_session
    before = helper("inspect", path)
    original = native_row(before, name, page)
    summary = next(row for row in session.annotation_inventory.values()
                   if row.native_name == name and row.page_index == page)
    assert summary.capabilities.editable_style
    color = dict(HIGHLIGHT_COLOR_PRESETS)["Viola"]
    command = recolor(session, summary.logical_id, color)
    assert command_from_payload(command_to_payload(command)) == command
    submit(manager, command)
    changed = session.annotation_inventory[summary.logical_id]
    assert changed.style["stroke"] == pytest.approx([color.red, color.green, color.blue])
    assert changed.geometry == summary.geometry
    assert changed.style["opacity"] == summary.style["opacity"]
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert session.annotation_inventory[summary.logical_id] == summary
    assert not session.dirty
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    destination = tmp_path / "colored.pdf"
    save(manager, session, destination)
    after = helper("inspect", destination)
    persisted = native_row(after, name, page)
    changed_keys = {key for key in original["keys"].keys() | persisted["keys"].keys()
                    if original["keys"].get(key) != persisted["keys"].get(key)}
    assert changed_keys == {"C", "AP"}
    assert persisted["rect"] == original["rect"] and persisted["vertices"] == original["vertices"]
    assert after["text"] == before["text"]
    for xref, data in before["objects"].items():
        assert after["objects"][xref]["stream"] == data["stream"]
    for row in before["rows"]:
        if row["xref"] != original["xref"]:
            assert native_row(after, row["name"], row["page"]) == row
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    assert native_row(helper("inspect", destination), name, page)["keys"] == original["keys"]
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    assert native_row(helper("inspect", destination), name, page)["keys"] == persisted["keys"]


def test_five_colors_creation_update_owned_and_history(color_session, tmp_path):
    manager, session, path = color_session
    target = _target(session)
    commands = []
    for name, color in HIGHLIGHT_COLOR_PRESETS:
        command = CreateHighlight(CommandContext.create(session.document_id, session.document_generation),
                                  target, HighlightStyle(color))
        submit(manager, command)
        commands.append(command)
    model = session.annotations[commands[0].annotation_id.value]
    logical_id = f"native:0:{model.backing.xref}"
    pink = dict(HIGHLIGHT_COLOR_PRESETS)["Rosa"]
    submit(manager, recolor(session, logical_id, pink))
    assert session.annotations[model.annotation_id.value].style.color == pink
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert session.annotations[model.annotation_id.value] == model
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    destination = tmp_path / "five.pdf"
    save(manager, session, destination)
    snapshot = helper("inspect", destination)
    for index, command in enumerate(commands):
        row = native_row(snapshot, command.annotation_id.value)
        expected = pink if index == 0 else command.style.color
        assert row["subtype"] == "Highlight"
        assert list(map(float, row["keys"]["C"][1].strip("[]").split())) == pytest.approx(
            [expected.red, expected.green, expected.blue])
        assert float(row["keys"]["CA"][1]) == pytest.approx(.35)
    # A later delete/undo must restore the recolored app model and native AP.
    submit(manager, DeleteAnnotation(CommandContext.create(session.document_id, session.document_generation),
                                    model.annotation_id, 0))
    submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert session.annotations[model.annotation_id.value].style.color == pink


def test_reject_other_annotation_types_and_locked_highlight(color_session):
    manager, session, _path = color_session
    for row in session.annotation_inventory.values():
        if row.kind != "highlight" or row.native_name == "locked":
            result = manager.submit_annotation(recolor(session, row.logical_id, RgbColor(1, 0, 0)))
            assert result.result is not None
            assert not result.result.success
            # The worker must independently reject crafted/bypassed GUI requests.
            response = request(session.worker, Operation.ANNOTATION_COMMAND,
                               command_to_payload(recolor(session, row.logical_id, RgbColor(1, 0, 0))))
            assert not result_from_payload(response.payload).success
    assert session.document_revision == 0


def test_context_palette_presets_selection_custom_and_tabs(app, tmp_path, pdf_fixture_copy, highlight_source):
    path = pdf_fixture_copy(tmp_path / "selection.pdf")
    window = MainWindow()
    window.resize(1100, 750)
    window.show()
    try:
        session, view = _open(window, path)
        _drag(view)
        assert window.pdf_text_bar.isVisible()
        from PySide6.QtWidgets import QToolButton
        assert len(window.pdf_text_bar.findChildren(QToolButton, options=Qt.FindChildOption.FindDirectChildrenOnly)) == 3
        button = window.context_highlight_button
        assert button.popupMode() == QToolButton.ToolButtonPopupMode.MenuButtonPopup
        menu = window.highlight_color_menu
        assert len(menu.actions()) == 5
        swatches = [row.defaultWidget() for row in menu.actions()]
        assert [swatch.accessibleName() for swatch in swatches] == [name for name, _ in HIGHLIGHT_COLOR_PRESETS]
        assert all(swatch.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly for swatch in swatches)
        assert all(swatch.toolTip() == swatch.accessibleName() for swatch in swatches)
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
        wait_until(menu.isVisible)
        assert menu.width() <= 50
        assert all(a.y() < b.y() for a, b in zip(swatches, swatches[1:]))
        assert swatches[0].isChecked()
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        wait_until(lambda: not menu.isVisible())
        assert view.text_layer.confirmed is not None
        assert window.highlight_color_actions["Giallo"].isChecked()
        window.highlight_color_actions["Verde"].trigger()
        assert view.text_layer.confirmed is not None
        window.highlight_action.trigger()
        wait_until(lambda: len(session.annotations) == 1)
        model = next(iter(session.annotations.values()))
        assert model.style.color == dict(HIGHLIGHT_COLOR_PRESETS)["Verde"]
        _drag(view, 12, 20)
        assert window.highlight_color_actions["Verde"].isChecked()
        window.highlight_action.trigger()
        wait_until(lambda: len(session.annotations) == 2)
        view._set_selected_text_markup(model.annotation_id.value)
        window._update_zoom_controls()
        window.highlight_color_actions["Rosa"].trigger()
        wait_until(lambda: session.annotations[model.annotation_id.value].style.color == dict(HIGHLIGHT_COLOR_PRESETS)["Rosa"])
        window.undo_action.trigger()
        wait_until(lambda: session.annotations[model.annotation_id.value].style.color == dict(HIGHLIGHT_COLOR_PRESETS)["Verde"])
        assert window.highlight_color_actions["Verde"].isChecked()
        external, external_view = _open(window, highlight_source)
        wait_until(lambda: external.inventory_complete)
        summary = next(row for row in external.annotation_inventory.values() if row.native_name == "custom")
        external_view.select_inventory_annotation(summary.logical_id, reveal=True)
        window._update_zoom_controls()
        assert window.pdf_text_bar.isVisible() and "personalizzato" in window.context_highlight_button.toolTip()
        assert all(not button.isChecked() for button in window.highlight_color_actions.values())
        assert external.document_revision == 0
        mapper = external_view.coordinate_mapper(summary.page_index)
        quad = summary.geometry.quads[0]
        x = sum(p.x for p in quad.points) / 4
        y = sum(p.y for p in quad.points) / 4
        px, py = map(round, mapper.canonical_to_viewport((x, y)))
        original_pixel = external_view.viewport().grab().toImage().pixelColor(px, py)
        window.highlight_color_actions["Azzurro"].trigger()
        wait_until(lambda: external.document_revision == 1)
        assert "personalizzato" not in window.context_highlight_button.toolTip()
        changed_pixel = external_view.viewport().grab().toImage().pixelColor(px, py)
        assert changed_pixel != original_pixel
        assert changed_pixel.blue() > original_pixel.blue()
        assert session.ui.highlight_color == "Rosa"
    finally:
        close_window_discard(window)


def test_color_rejects_stale_generation_after_save_as(color_session, tmp_path):
    manager, session, _path = color_session
    summary = next(row for row in session.annotation_inventory.values() if row.native_name == "custom")
    stale = recolor(session, summary.logical_id, RgbColor(1, 0, 0))
    save(manager, session, tmp_path / "new-generation.pdf")
    result = manager.submit_annotation(stale)
    assert result.result.error.code.value == "stale_generation"
    assert session.document_revision == 0


def test_color_adoption_failure_rolls_back_native_and_history(app, tmp_path, highlight_source, monkeypatch):
    from pdf_annotator.worker import document_service
    from tests.m151_helpers import fail_once_external_adoption_worker
    monkeypatch.setattr(document_service, "document_worker_main", fail_once_external_adoption_worker)
    path = tmp_path / "rollback.pdf"
    shutil.copyfile(highlight_source, path)
    before = helper("inspect", path)
    manager = DocumentManager()
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        summary = next(row for row in session.annotation_inventory.values() if row.native_name == "custom")
        results = []
        manager.annotation_result.connect(results.append)
        command = recolor(session, summary.logical_id, RgbColor(1, 0, 0))
        manager.submit_annotation(command)
        wait_until(lambda: bool(results))
        assert not results[-1].success
        assert session.document_revision == session.history_position == 0
        assert session.annotation_inventory[summary.logical_id] == summary
        destination = tmp_path / "after-failure.pdf"
        save(manager, session, destination)
        assert helper("inspect", destination)["rows"] == before["rows"]
        wait_until(lambda: session.inventory_complete)
        submit(manager, recolor(session, summary.logical_id, RgbColor(0, 1, 0)))
        assert session.document_revision == 1
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_color_payload_requires_exactly_one_typed_intent():
    from dataclasses import replace
    from pdf_annotator.domain.errors import DomainValidationError
    from pdf_annotator.domain.styles import FreeTextStyle
    logical_id = "native:0:7"
    command = EditExternalAnnotation(CommandContext.create("document", 1),
        external_command_annotation_id(logical_id), logical_id, 0,
        ExternalEditKind.COLOR, color=RgbColor(.2, .4, .8))
    for invalid in (None, (.2, .4, .8)):
        with pytest.raises(ValueError):
            replace(command, color=invalid)
    with pytest.raises(ValueError):
        replace(command, style=FreeTextStyle())
    payload = command_to_payload(command)
    payload["color"] = [1.1, 0, 0]
    with pytest.raises(DomainValidationError):
        command_from_payload(payload)
