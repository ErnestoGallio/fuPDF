"""External profile edits through service/worker, with isolated PDF inspection."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pdf_annotator.app.document_manager import DocumentManager
from pdf_annotator.domain.annotation_commands import CommandContext, Undo, Redo
from pdf_annotator.domain.annotation_inventory import external_command_annotation_id, is_attached_reply
from pdf_annotator.domain.external_edits import EditExternalAnnotation, ExternalEditKind
from pdf_annotator.domain.geometry import PdfPoint, PdfRect
from pdf_annotator.domain.rich_text import RichText, RichTextRun
from pdf_annotator.domain.serialization import command_from_payload, command_to_payload
from pdf_annotator.domain.styles import RgbColor, FreeTextStyle
from pdf_annotator.worker import document_service
from pdf_annotator.worker.protocol import Operation
from tests.conftest import request, wait_until
from tests.m151_helpers import candidate_worker
from tests.m151_fixtures import BOX, BOX_PAGE, CALLOUT, CALLOUT_PAGE, OTHER_CALLOUT
from tests.test_m6_service_persistence import _opened
from tests.test_save_m15 import save, submit


def test_plain_native_bold_default_is_not_downgraded():
    from types import SimpleNamespace
    from pdf_annotator.worker.external_profiles import _text_profile

    class NativeValues:
        def xref_get_key(self, xref, key):
            return ("string", "0 0 0 rg /HeBo 10 Tf") if key == "DA" else ("null", "null")

    annot = SimpleNamespace(xref=1, info={"content": "Bold native"},
                            border={"width": 1, "style": "S"}, opacity=1)
    rich, _style = _text_profile(NativeValues(), annot)
    assert rich.runs[0].bold
    assert rich.runs[0].font_family == "Helvetica"


def test_editor_font_mapping_uses_bundled_canonical_files(app):
    """Qt presentation is application-private, never a Windows font alias."""
    from PySide6.QtGui import QFont, QFontInfo
    from pdf_annotator.app.canonical_fonts import ensure_canonical_editor_fonts
    from pdf_annotator.viewer.pdf_view import _editor_font_family, _pdf_font_family

    ensure_canonical_editor_fonts()
    assert _editor_font_family("Helvetica") == "Source Sans 3"
    assert _editor_font_family("Courier") == "Courier Prime"
    assert _pdf_font_family("Source Sans 3") == "Helvetica"
    assert _pdf_font_family("Courier Prime") == "Courier"
    assert QFontInfo(QFont("Source Sans 3")).family() == "Source Sans 3"
    assert QFontInfo(QFont("Courier Prime")).family() == "Courier Prime"


def helper(operation, *paths):
    completed = subprocess.run([sys.executable, "-m", "tests.m151_helpers", operation, *map(str, paths)],
                               check=True, capture_output=True, timeout=60)
    return json.loads(completed.stdout) if completed.stdout else None


@pytest.fixture(scope="module")
def native_source(tmp_path_factory):
    path = tmp_path_factory.mktemp("m151") / "native.pdf"
    helper("fixture", path)
    return path


@pytest.fixture(scope="module")
def imported_source(tmp_path_factory):
    path = tmp_path_factory.mktemp("m151-imported") / "imported.pdf"
    subprocess.run([sys.executable, "-m", "tests.m151_fixtures", str(path)],
                   check=True, timeout=30)
    return path


@pytest.fixture
def candidate_session(app, tmp_path, native_source, monkeypatch):
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "source.pdf"
    shutil.copyfile(native_source, path)
    manager = DocumentManager()
    session = _opened(manager, path)
    try:
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        yield manager, session, path
    finally:
        wait_until(lambda: not session.saving, timeout=60)
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def find_native(snapshot, name, page=0):
    return next(row for row in snapshot["annotations"] if row["nm"] == name and row["page"] == page)


def assert_native_object_restored(actual, original):
    """Compare PDF object semantics, not PyMuPDF stream serialization."""
    from pdf_annotator.worker.save_validation import _parse

    expected, observed = _parse(original["source"]), _parse(actual["source"])
    if original["stream"] is not None:
        # /Length and stream filter spelling are transport details. The
        # decoded stream digest below proves the AP bytes themselves restored.
        expected = {key: value for key, value in expected.items()
                    if key not in ("/Length", "/Filter", "/DecodeParms")}
        observed = {key: value for key, value in observed.items()
                    if key not in ("/Length", "/Filter", "/DecodeParms")}
    assert observed == expected
    assert actual["stream"] == original["stream"]


def command_for(session, row, edit, **kwargs):
    logical_id = f'native:{row["page"]}:{row["xref"]}'
    return EditExternalAnnotation(CommandContext.create(session.document_id, session.document_generation),
        external_command_annotation_id(logical_id), logical_id, row["page"], edit, **kwargs)


def assert_preserved(before, after, root, allowed):
    original = next(row for row in before["annotations"] if row["xref"] == root)
    changed = next(row for row in after["annotations"] if row["xref"] == root)
    keys = original["keys"].keys() | changed["keys"].keys()
    actual = {key for key in keys if original["keys"].get(key) != changed["keys"].get(key)}
    assert actual <= allowed
    assert actual
    # Native dictionaries AND every pre-existing decoded stream (AP/resources,
    # popup/replies, font/page content) remain byte/semantically unchanged.
    for xref, data in before["objects"].items():
        if xref != str(root):
            assert_native_object_restored(after["objects"][xref], data)


def test_untouched_external_oblique_callout_is_not_normalized(candidate_session, tmp_path):
    manager, session, path = candidate_session
    source_bytes = path.read_bytes()
    before = helper("inspect", path)
    original = find_native(before, "callout")
    anchor, knee = original["vertices"][:2]
    assert anchor[0] != knee[0] and anchor[1] != knee[1]
    destination = tmp_path / "untouched-oblique-callout.pdf"
    save(manager, session, destination)
    preserved = find_native(helper("inspect", destination), "callout")
    assert preserved["keys"]["CL"] == original["keys"]["CL"]
    assert len(preserved["vertices"]) == len(original["vertices"])
    for actual, expected in zip(preserved["vertices"], original["vertices"], strict=True):
        assert actual == pytest.approx(expected, abs=.001)
    assert path.read_bytes() == source_bytes


@pytest.mark.parametrize("page", range(4))
@pytest.mark.parametrize("name,edit,allowed", [
    ("plain", ExternalEditKind.MOVE, {"Rect", "M"}),
    ("plain", ExternalEditKind.RESIZE, {"Rect", "RD", "Contents", "RC", "C", "DA", "DS", "BS", "CA", "AP", "M"}),
    ("rich", ExternalEditKind.TEXT, {"Contents", "RC", "C", "DA", "DS", "BS", "CA", "AP", "M"}),
    ("rich", ExternalEditKind.STYLE, {"Contents", "RC", "C", "DA", "DS", "BS", "CA", "AP", "M"}),
    ("plain-full", ExternalEditKind.MOVE, {"Rect", "M"}),
    ("plain-full", ExternalEditKind.RESIZE, {"Rect", "RD", "Contents", "RC", "C", "DA", "DS", "BS", "CA", "AP", "M"}),
    ("plain-full", ExternalEditKind.TEXT, {"Contents", "RC", "C", "DA", "DS", "BS", "CA", "AP", "M"}),
    ("plain-full", ExternalEditKind.STYLE, {"Contents", "RC", "C", "DA", "DS", "BS", "CA", "AP", "M"}),
    ("font", ExternalEditKind.MOVE, {"Rect", "M"}),
    ("callout", ExternalEditKind.MOVE, {"Rect", "RD", "CL", "AP", "M"}),
    ("callout", ExternalEditKind.RESIZE, {"Rect", "RD", "CL", "Contents", "RC", "C", "DA", "DS", "BS", "CA", "AP", "M"}),
    ("callout", ExternalEditKind.ANCHOR, {"Rect", "RD", "CL", "AP", "M"}),
    ("ink", ExternalEditKind.MOVE, {"Rect", "InkList", "M"}),
])
def test_native_edit_save_history_preservation(candidate_session, tmp_path, page, name, edit, allowed):
    manager, session, path = candidate_session
    source_bytes = path.read_bytes()
    before = helper("inspect", path)
    row = find_native(before, name, page)
    kwargs = ({"delta": (8, 6)} if edit == ExternalEditKind.MOVE else
              {"rect": PdfRect(row["box"][0], row["box"][1], row["box"][2]+12, row["box"][3]+15)}
              if edit == ExternalEditKind.RESIZE else
              {"anchor": PdfPoint(230, 475)} if edit == ExternalEditKind.ANCHOR else
              {"rich_text": RichText.plain_text("Caffè & tè <ok>", font_family="Courier", font_size=10,
                                                color=RgbColor(0, 0, .8))} if edit == ExternalEditKind.TEXT else
              {"style": FreeTextStyle(RgbColor(.2, .4, .8), RgbColor(.8, .9, 1), font_size=10)})
    command = command_for(session, row, edit, **kwargs)
    assert command_from_payload(command_to_payload(command)) == command
    result = submit(manager, command)
    assert result.history_delta.inventory_updated[0].logical_id == command.logical_id
    assert not result.refresh_inventory
    assert result.annotation is None
    assert session.annotation_inventory[command.logical_id].provenance == "external"
    output = tmp_path / "edited.pdf"
    save(manager, session, output)
    after = helper("inspect", output)
    assert_preserved(before, after, row["xref"], allowed)
    new_row = find_native(after, name, page)
    if name == "plain-full":
        if edit == ExternalEditKind.MOVE:
            assert new_row["profile"] == "freetext-plain-v1"
            assert new_row["keys"]["RC"] == row["keys"]["RC"] == ["null", "null"]
        else:
            assert new_row["profile"] == "freetext-rich-v1"
            assert new_row["keys"]["RC"][0] == "string"
    if edit == ExternalEditKind.MOVE and name != "callout":
        assert new_row["rect"] == pytest.approx([v + (8 if i % 2 == 0 else 6)
                                               for i, v in enumerate(row["rect"])], abs=.001)
    if name == "ink":
        for old_path, new_path in zip(row["vertices"], new_row["vertices"], strict=True):
            assert len(old_path) == len(new_path)
            for old, new in zip(old_path, new_path, strict=True):
                assert new == pytest.approx([old[0]+8, old[1]+6], abs=.001)
    if name == "callout":
        assert new_row["keys"]["IT"] == row["keys"]["IT"]
        if edit != ExternalEditKind.ANCHOR:
            assert new_row["vertices"][0] == pytest.approx(row["vertices"][0], abs=.001)
        else:
            assert new_row["vertices"][0] == pytest.approx([230, 475], abs=.001)
    undone = submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
    assert undone.history_delta.inventory_updated[0].logical_id == command.logical_id
    save(manager, session)
    restored = helper("inspect", output)
    for xref, original in before["objects"].items():
        assert_native_object_restored(restored["objects"][xref], original)
    submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation)))
    save(manager, session)
    redone = helper("inspect", output)
    for xref, data in after["objects"].items():
        assert_native_object_restored(redone["objects"][xref], data)
    assert path.read_bytes() != output.read_bytes()
    assert path.read_bytes() == source_bytes


def test_production_gate_and_unsupported_profiles(app, native_source, tmp_path):
    from pdf_annotator.domain.external_edits import CAPABILITY_FOR_EDIT
    from pdf_annotator.worker.external_profiles import VERIFIED_EXTERNAL_EDITS

    expected_registry = frozenset({
        *(("freetext-plain-v1", operation) for operation in ("move", "resize", "text", "style")),
        *(("freetext-rich-v1", operation) for operation in ("move", "resize", "text", "style")),
        *(("callout-rich-v1", operation) for operation in ("move", "resize", "anchor", "text", "style")),
    })
    assert VERIFIED_EXTERNAL_EDITS == expected_registry
    manager = DocumentManager()
    path = tmp_path / "gate.pdf"
    shutil.copyfile(native_source, path)
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        for summary in session.annotation_inventory.values():
            caps = summary.capabilities
            profile = summary.style.get("external_profile")
            expected = {field for operation, field in CAPABILITY_FOR_EDIT.items()
                        if (profile, operation.value) in expected_registry}
            assert {field for field in CAPABILITY_FOR_EDIT.values() if getattr(caps, field)} == expected
        before = helper("inspect", path)
        assert submit(manager, command_for(
            session, find_native(before, "rich"), ExternalEditKind.MOVE, delta=(5, 5))).success
        cmd = command_for(session, find_native(before, "fitz-A0"), ExternalEditKind.MOVE, delta=(5, 5))
        assert manager.submit_annotation(cmd).result.error.code.value == "read_only_annotation"
        assert session.confirmed_revision == 1
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_unmapped_native_profiles_and_overflow_remain_editable(candidate_session):
    manager, session, path = candidate_session
    before = helper("inspect", path)
    for name, operation, kwargs in (("unknown", ExternalEditKind.MOVE, {"delta": (5, 5)}),
            ("font", ExternalEditKind.RESIZE, {"rect": PdfRect(310, 40, 570, 130)})):
        cmd = command_for(session, find_native(before, name), operation, **kwargs)
        assert submit(manager, cmd).success
    cmd = command_for(session, find_native(before, "rich"), ExternalEditKind.RESIZE,
                      rect=PdfRect(40, 160, 45, 165))
    assert submit(manager, cmd).success
    assert session.confirmed_revision == 3


def test_imported_freetext_normalizes_on_first_layout_edit_and_history_restores_native(candidate_session):
    """Move is native-preserving; resize adopts the deterministic app roles."""
    manager, session, path = candidate_session
    before = helper("inspect", path)
    original = find_native(before, "font")
    identity = original["appearance"]["fonts"]

    resize = command_for(session, original, ExternalEditKind.RESIZE,
                         rect=PdfRect(original["box"][0], original["box"][1],
                                      original["box"][2] + 24, original["box"][3] + 16))
    move = command_for(session, original, ExternalEditKind.MOVE, delta=(8, 6))
    assert submit(manager, move).success
    assert submit(manager, resize).success
    resized_summary = session.annotation_inventory[resize.logical_id]
    assert resized_summary.geometry.text_rect.x1 > original["box"][2]

    assert submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation))).success
    undone_summary = session.annotation_inventory[resize.logical_id]
    assert undone_summary.geometry.text_rect.x1 == pytest.approx(original["box"][2] + 8)
    assert submit(manager, Redo(CommandContext.create(session.document_id, session.document_generation))).success

    # Follow-up text keeps the normalised semantic run distinction.
    text = command_for(session, original, ExternalEditKind.TEXT,
                       rich_text=RichText((
                           RichTextRun("Native ", "Helvetica", 10, RgbColor(0, 0, 0)),
                           RichTextRun("rick", "Courier", 10, RgbColor(0, 0, .8), True, True),
                       )))
    assert submit(manager, text).success

    style = command_for(session, original, ExternalEditKind.STYLE,
                        style=FreeTextStyle(RgbColor(.2, .4, .8), RgbColor(.8, .9, 1),
                                            font_size=10, font_family="Helvetica"))
    assert submit(manager, style).success
    save(manager, session)
    styled = find_native(helper("inspect", path), "font")
    assert styled["keys"]["Contents"][1] == "Native rick"
    assert "font-family:Helvetica" in styled["keys"]["RC"][1]
    assert "Courier Std" in styled["keys"]["RC"][1]
    assert styled["appearance"]["fonts"] != identity
    assert styled["appearance"]["stream"] != original["appearance"]["stream"]
    assert "/Helv" in styled["keys"]["DA"][1]
    assert styled["profile"] == "freetext-rich-v1"
    child = styled["appearance"]["canonical_text"]
    assert "/Type /XObject" in child["form"] and "/Subtype /Form" in child["form"]
    assert "/FSource" in child["resources"] and "/FCourier" in child["resources"]
    assert all("/ToUnicode" in font[1] and "/FontFile" in font[3] for font in child["fonts"])


def test_imported_font_roles_map_one_and_many_families_deterministically(candidate_session):
    manager, session, path = candidate_session
    before = helper("inspect", path)
    single = find_native(before, "single-font")
    multi = find_native(before, "multi-font")
    for row in (single, multi):
        command = command_for(session, row, ExternalEditKind.RESIZE,
                              rect=PdfRect(row["box"][0], row["box"][1],
                                           row["box"][2] + 20, row["box"][3] + 10))
        assert submit(manager, command).success
    save(manager, session)
    normalized = helper("inspect", path)
    one = find_native(normalized, "single-font")
    many = find_native(normalized, "multi-font")
    assert [run["font_family"] for run in one["rich_text"]] == ["Helvetica"]
    assert [run["font_family"] for run in many["rich_text"]] == ["Helvetica", "Courier", "Courier"]
    assert [(run["font_size"], run["color"], run["bold"], run["italic"])
            for run in many["rich_text"]] == [(10.0, [0.0, 0.0, 0.0], False, False),
                                                (11.0, [26 / 255, 51 / 255, 204 / 255], True, False),
                                                (9.0, [204 / 255, 26 / 255, 51 / 255], False, True)]
    canonical = many["appearance"]["canonical_text"]
    assert "/FSource" in canonical["resources"]
    assert "/FCourierBold" in canonical["resources"]
    assert "/FCourierItalic" in canonical["resources"]
    assert canonical["stream"].count(" Tj") >= 3


def test_move_external_freetext_does_not_normalize_imported_appearance(candidate_session, tmp_path):
    manager, session, path = candidate_session
    before = helper("inspect", path)
    row = find_native(before, "font")
    command = command_for(session, row, ExternalEditKind.MOVE, delta=(12, 7))
    assert submit(manager, command).success
    output = tmp_path / "moved-native.pdf"
    save(manager, session, output)
    moved = find_native(helper("inspect", output), "font")
    assert moved["appearance"] == row["appearance"]
    assert moved["keys"]["RC"] == row["keys"]["RC"]


def test_external_freetext_without_border_does_not_gain_one(candidate_session):
    """A zero-width imported shell stays borderless through normalization."""
    manager, session, path = candidate_session
    before = helper("inspect", path)
    row = find_native(before, "no-border")
    assert row["keys"]["BS"][1] == "<</Type/Border/W 0>>"
    assert not row["appearance"]["paint"]["stroke"]
    command = command_for(session, row, ExternalEditKind.RESIZE,
                          rect=PdfRect(row["box"][0], row["box"][1],
                                       row["box"][2] + 16, row["box"][3] + 10))
    assert submit(manager, command).success
    save(manager, session)
    normalized = find_native(helper("inspect", path), "no-border")
    assert normalized["keys"]["BS"][1] == "<</W 0/S/S>>"
    assert not normalized["appearance"]["paint"]["stroke"]
    assert normalized["appearance"]["paint"]["widths"] == ["0"]


def test_first_normalization_undo_restores_exact_native_ap(candidate_session):
    manager, session, path = candidate_session
    before = helper("inspect", path)
    row = find_native(before, "font")
    command = command_for(session, row, ExternalEditKind.RESIZE,
                          rect=PdfRect(row["box"][0], row["box"][1],
                                       row["box"][2] + 30, row["box"][3] + 18))
    assert submit(manager, command).success
    assert submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation))).success
    save(manager, session)
    restored = find_native(helper("inspect", path), "font")
    assert restored["keys"] == row["keys"]
    assert restored["appearance"] == row["appearance"]


def test_imported_box_move_resize_uses_canonical_child_and_history(
        app, tmp_path, monkeypatch, imported_source):
    """Preserve a translated multiline imported Form before canonical resize."""
    source = imported_source
    path = tmp_path / "imported-box.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    manager = DocumentManager()
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        before = helper("inspect", path)
        row = find_native(before, BOX, BOX_PAGE)
        assert row["profile"] == "freetext-rich-v1"
        assert "/ImportedSans-Regular" in row["appearance"]["resources"]
        # Acrobat's native split is fill in /C and border/default color in
        # /DA / /DS. PyMuPDF's imported ``annot.colors`` misleadingly reports
        # the fill as stroke for this producer, so it is not the semantic
        # border source.
        assert row["keys"]["C"][1] == "[.988235 .956863 .521576]"
        assert "0.9725 0.3922 0.3922 rg" in row["keys"]["DA"][1]
        assert "#F86464" in row["keys"]["DS"][1]
        assert row["native_paint"]["colors"]["stroke"] == pytest.approx([.988235, .956863, .521576])
        trace = helper("trace_imported_box", path)
        stages = {stage["stage"]: stage for stage in trace}
        assert stages["B_semantic_native_style"]["semantic_style"]["border"] == pytest.approx(
            [.9725, .3922, .3922])
        assert stages["C_before_annot_update"]["keys"]["BS"][1] == "<</W 1/S/S>>"
        for stage in ("D_after_annot_update", "E_after_canonical_patch"):
            parent = stages[stage]["parent"]
            assert parent["stroke"] and parent["widths"] == ["1"]
            assert parent["stroke_colors"] == [[".9725", ".3922", ".3922"]]
        assert not stages["D_after_annot_update"]["parent"]["canonical_child"]
        assert stages["E_after_canonical_patch"]["parent"]["canonical_child"]

        move = command_for(session, row, ExternalEditKind.MOVE, delta=(8, 6))
        move_payload = command_to_payload(move)
        assert move_payload["page_index"] == BOX_PAGE
        assert "destination_page_index" not in move_payload
        assert submit(manager, move).success
        assert session.annotation_inventory[move.logical_id].page_index == BOX_PAGE
        # Persist this pre-normalization state so Undo can be compared to the
        # actual native snapshot immediately before the resize.
        save(manager, session)
        wait_until(lambda: session.inventory_complete)
        moved = helper("inspect", path)
        moved_row = find_native(moved, BOX, BOX_PAGE)

        resize = command_for(session, moved_row, ExternalEditKind.RESIZE,
                             rect=PdfRect(moved_row["box"][0], moved_row["box"][1],
                                          moved_row["box"][2] + 12, moved_row["box"][3] + 8))
        resize_payload = command_to_payload(resize)
        assert resize_payload["page_index"] == BOX_PAGE
        assert "destination_page_index" not in resize_payload
        assert submit(manager, resize).success
        assert session.annotation_inventory[resize.logical_id].page_index == BOX_PAGE
        save(manager, session)
        normalized = helper("inspect", path)
        normalized_row = find_native(normalized, BOX, BOX_PAGE)
        child = normalized_row["appearance"]["canonical_text"]
        assert "/Type /XObject" in child["form"] and "/Subtype /Form" in child["form"]
        assert "/FSource" in child["resources"]
        assert all("/ToUnicode" in font[1] and "/FontFile" in font[3]
                   for font in child["fonts"])
        assert any("/BaseFont /Source#20Sans#203#20Regular" in font[1]
                   and "/BaseFont /SourceSans3-Regular" in font[2]
                   for font in child["fonts"])
        # The child carries glyphs only. Fill, border, clipping and geometry
        # remain in the PyMuPDF-generated parent Form.
        assert " re" not in child["stream"] and " RG" not in child["stream"]
        assert " rg" not in child["stream"] and " S" not in child["stream"]
        parent_paint = normalized_row["appearance"]["paint"]
        assert parent_paint["stroke"] and parent_paint["widths"] == ["1"]
        assert parent_paint["stroke_colors"] == [[".9725", ".3922", ".3922"]]

        assert submit(manager, Undo(CommandContext.create(
            session.document_id, session.document_generation))).success
        save(manager, session)
        undone = helper("inspect", path)
        undone_row = find_native(undone, BOX, BOX_PAGE)
        assert undone_row["keys"] == moved_row["keys"]
        assert undone_row["appearance"] == moved_row["appearance"]
        for xref, object_data in moved["objects"].items():
            assert_native_object_restored(undone["objects"][xref], object_data)

        assert submit(manager, Redo(CommandContext.create(
            session.document_id, session.document_generation))).success
        save(manager, session)
        redone = helper("inspect", path)
        redone_row = find_native(redone, BOX, BOX_PAGE)
        assert {entry["xref"] for entry in (moved_row, normalized_row, undone_row, redone_row)} == {row["xref"]}
        assert redone_row["appearance"]["canonical_text"]["stream"] == child["stream"]
        assert redone_row["appearance"]["paint"] == parent_paint
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_imported_callout_callout_cr_normalization_is_native_and_reversible(
        app, tmp_path, monkeypatch, imported_source):
    """Regression for an imported Callout with CR paragraph text.

    The direct adapter trace isolates every supported operation.  The second
    half deliberately takes the normal GUI -> service -> worker path so history
    and the persisted production appearance are covered as well.
    """
    source = imported_source
    original = find_native(helper("inspect", source), CALLOUT, CALLOUT_PAGE)
    trace = {item["command_type"]: item for item in helper("trace_imported_callout", source)}
    assert set(trace) == {operation.value for operation in ExternalEditKind}
    for operation in trace.values():
        assert operation["page_index"] == CALLOUT_PAGE and operation["xref"] == original["xref"]
        assert operation["exception_type"] is None, operation["traceback"]
    for name in ("move", "anchor"):
        operation = trace[name]
        assert not operation["normalization_requested"]
        assert not operation["canonical_text_ap_patch_ran"]
        assert not operation["after"]["parent"]["canonical_child"]
        # Geometry edits regenerate only the PyMuPDF-native parent AP. They
        # never select the isolated canonical text child.
        assert operation["after"]["parent"]["normal"] != operation["before"]["parent"]["normal"]
    for name in ("resize", "text", "style"):
        operation = trace[name]
        assert operation["normalization_requested"]
        assert operation["annot_update_ran"] and operation["canonical_text_ap_patch_ran"]
        stages = {stage["stage"]: stage for stage in operation["normalization_trace"]}
        fresh, patched = (stages["D_after_annot_update"]["parent"],
                          stages["E_after_canonical_patch"]["parent"])
        # The patch can replace only text: the PyMuPDF-native parent form keeps
        # its BBox, Matrix, leader/arrow geometry, clipping and box paint.
        assert (patched["bbox"], patched["matrix"], patched["geometry_stream_sha256"]) == (
            fresh["bbox"], fresh["matrix"], fresh["geometry_stream_sha256"])

    path = tmp_path / "imported-x474.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    manager = DocumentManager()
    try:
        def native_appearance(item):
            # A full save may reorder untouched font dictionary keys. These
            # are the immutable parent-AP facts for non-normalizing commands.
            appearance = item["appearance"]
            return {key: appearance[key] for key in
                    ("normal", "stream", "bbox", "matrix", "resources", "paint")}

        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        before = helper("inspect", path)
        row = find_native(before, CALLOUT, CALLOUT_PAGE)
        assert row["profile"] == "callout-rich-v1"
        assert row["reason"] == "Mapped Acrobat text/style and native geometry; Acrobat gate pending"
        assert row["keys"]["IT"][1] == "/FreeTextCallout"
        assert row["keys"]["LE"][1] == "/OpenArrow"
        assert row["keys"]["C"][1] == "[.988235 .956863 .521576]"
        assert "0.9725 0.3922 0.3922 rg" in row["keys"]["DA"][1]
        assert row["rich_text"][0]["bold"] and "\r" in row["rich_text"][1]["text"]

        # Box move and anchor move retain the exact Acrobat AP; neither is a
        # typographic normalization trigger.  The adapter trace above records
        # the persisted AP facts; this service-path probe verifies both commands
        # also enter and leave history without converting the native shell.
        anchored = command_for(session, row, ExternalEditKind.ANCHOR,
                               anchor=PdfPoint(row["vertices"][0][0] + 8,
                                               row["vertices"][0][1] + 6))
        assert submit(manager, anchored).success
        assert submit(manager, Undo(CommandContext.create(
            session.document_id, session.document_generation))).success
        move = command_for(session, row, ExternalEditKind.MOVE, delta=(8, 6))
        assert submit(manager, move).success

            # Resize is the first Callout layout edit.  It creates only a child
        # text Form; the Callout parent still owns its three-point leader,
        # OpenArrow, Rect/RD/CL, native clip and border/fill appearance.
        resize = command_for(session, row, ExternalEditKind.RESIZE,
                             rect=PdfRect(row["box"][0] + 8, row["box"][1] + 6,
                                          row["box"][2] + 20, row["box"][3] + 14))
        assert submit(manager, resize).success
        # Undo restores the exact moved native snapshot before first layout.
        assert submit(manager, Undo(CommandContext.create(
            session.document_id, session.document_generation))).success
        save(manager, session)
        undone = helper("inspect", path)
        undone_row = find_native(undone, CALLOUT, CALLOUT_PAGE)
        assert undone_row["keys"]["Contents"] == row["keys"]["Contents"]
        assert "canonical_text" not in undone_row["appearance"]
        assert submit(manager, Redo(CommandContext.create(
            session.document_id, session.document_generation))).success
        save(manager, session)
        normalized_row = find_native(helper("inspect", path), CALLOUT, CALLOUT_PAGE)
        assert normalized_row["xref"] == undone_row["xref"] == row["xref"]
        child = normalized_row["appearance"]["canonical_text"]
        assert normalized_row["keys"]["Contents"] == row["keys"]["Contents"]
        assert normalized_row["keys"]["IT"] == row["keys"]["IT"]
        assert normalized_row["keys"]["LE"] == row["keys"]["LE"]
        assert len(normalized_row["vertices"]) == 3  # one /CL leader, not reconstruction
        assert normalized_row["keys"]["C"] == row["keys"]["C"]
        assert "0.9725 0.3922 0.3922 rg" in normalized_row["keys"]["DA"][1]
        assert "/FSource" in child["resources"]
        assert all("/Subtype /Type0" in font[1] and "/Encoding /Identity-H" in font[1]
                   and "/FontFile" in font[3] for font in child["fonts"])
        assert any("/BaseFont /SourceSans3-Regular" in font[2] for font in child["fonts"])
        assert any("/BaseFont /SourceSans3-Bold" in font[2] for font in child["fonts"])
        assert "Helvetica" not in child["resources"] and "Arial" not in child["resources"]
        assert all(token not in child["stream"] for token in (" re", " RG", " rg", " m", " l", " S"))

    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_imported_callout_box_and_anchor_geometry_keep_distinct_invariants(
        app, tmp_path, monkeypatch, imported_source):
    """Imported Callouts move their box and anchor by different rules."""
    from pdf_annotator.domain.callout_geometry import callout_attachment_side, callout_line

    source = imported_source
    path = tmp_path / "imported-callout-geometry.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    manager = DocumentManager()
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        original = helper("inspect", path)

        def assert_rect(actual, expected) -> None:
            assert (actual.x0, actual.y0, actual.x1, actual.y1) == pytest.approx(
                (expected.x0, expected.y0, expected.x1, expected.y1), abs=.001)

        for name in (CALLOUT, OTHER_CALLOUT):
            row = find_native(original, name, CALLOUT_PAGE)
            logical_id = f'native:{CALLOUT_PAGE}:{row["xref"]}'
            before = session.annotation_inventory[logical_id]
            box = before.geometry.text_rect
            line = before.geometry.callout_line
            assert box is not None and len(line) == 3

            move = command_for(session, row, ExternalEditKind.MOVE, delta=(8, 6))
            assert submit(manager, move).success
            moved = session.annotation_inventory[logical_id]
            moved_box, moved_line = moved.geometry.text_rect, moved.geometry.callout_line
            assert moved_box is not None and len(moved_line) == 3
            assert_rect(moved_box, PdfRect(box.x0 + 8, box.y0 + 6, box.x1 + 8, box.y1 + 6))
            assert moved_line[0] == line[0]  # OpenArrow endpoint is fixed.
            expected_line = callout_line(moved_box, line[0])
            for actual, expected in zip(moved_line, expected_line, strict=True):
                assert (actual.x, actual.y) == pytest.approx((expected.x, expected.y), abs=.001)
            assert not moved.geometry.bbox == PdfRect(before.geometry.bbox.x0 + 8,
                                                       before.geometry.bbox.y0 + 6,
                                                       before.geometry.bbox.x1 + 8,
                                                       before.geometry.bbox.y1 + 6)

            requested_anchor = PdfPoint(line[0].x + 12, line[0].y + 9)
            anchor = command_for(session, row, ExternalEditKind.ANCHOR, anchor=requested_anchor)
            assert submit(manager, anchor).success
            anchored = session.annotation_inventory[logical_id]
            assert_rect(anchored.geometry.text_rect, moved_box)
            expected_line = callout_line(moved_box, requested_anchor)
            for actual, expected in zip(anchored.geometry.callout_line, expected_line, strict=True):
                assert (actual.x, actual.y) == pytest.approx((expected.x, expected.y), abs=.001)

        save(manager, session)
        persisted = helper("inspect", path)
        for name in (CALLOUT, OTHER_CALLOUT):
            before = find_native(original, name, CALLOUT_PAGE)
            after = find_native(persisted, name, CALLOUT_PAGE)
            assert after["xref"] == before["xref"]
            assert after["keys"]["Contents"] == before["keys"]["Contents"]
            assert after["keys"]["RC"] == before["keys"]["RC"]
            assert after["keys"]["DA"] == before["keys"]["DA"]
            assert not after["appearance"].get("canonical_text")
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_imported_callout_repeated_resize_preserves_detail_style_and_runs(
        app, tmp_path, monkeypatch, imported_source):
    """A second larger resize must read back the same rich canonical Callout state."""
    source = imported_source
    trace = helper("trace_imported_callout_repeated_resize", source)
    native, first, second = trace["native"], trace["first"], trace["second"]
    expected_runs = native["rich_text"]
    expected_style = native["edit_style"]
    for stage in (first, second):
        assert stage["profile"] == "callout-rich-v1"
        assert stage["rich_text"] == expected_runs
        assert stage["edit_style"] == expected_style
        assert stage["keys"]["Contents"] == native["keys"]["Contents"]
        assert stage["keys"]["C"] == native["keys"]["C"]
        assert stage["canonical_child"]
    for resize_trace in (trace["first_trace"], trace["second_trace"]):
        stages = {item["stage"]: item for item in resize_trace}
        assert tuple(stages) == ("A_before_resize", "B_semantic_native_style",
                                 "C_before_annot_update", "D_after_annot_update",
                                 "E_after_canonical_patch")
        semantic_style = stages["B_semantic_native_style"]["semantic_style"]
        assert {key: semantic_style[key] for key in
                ("border", "fill", "text", "opacity", "border_width", "font_family")} == {
                    key: expected_style[key] for key in
                    ("border", "fill", "text", "opacity", "border_width", "font_family")}
        assert stages["E_after_canonical_patch"]["parent"]["canonical_child"]

    path = tmp_path / "imported-callout-detail.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    manager = DocumentManager()
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        row = find_native(helper("inspect", path), CALLOUT, CALLOUT_PAGE)
        logical_id = f'native:{row["page"]}:{row["xref"]}'

        def detail() -> None:
            manager.request_annotation_detail(session.document_id, logical_id)
            wait_until(lambda: logical_id in session.annotation_details)
            value = session.annotation_details[logical_id]
            assert value.contents == native["keys"]["Contents"][1]
            assert value.edit_rich_text is not None and value.edit_style is not None
            assert [(run.text, run.font_family, run.font_size, run.bold, run.italic)
                    for run in value.edit_rich_text.runs] == [
                        (run["text"], run["font_family"], run["font_size"],
                         run["bold"], run["italic"])
                        for run in expected_runs]
            assert value.edit_style.fill is not None
            assert (value.edit_style.fill.red, value.edit_style.fill.green,
                    value.edit_style.fill.blue) == pytest.approx(tuple(expected_style["fill"]))
            assert value.edit_style.font_size == expected_style["font_size"]

        first_resize = command_for(session, row, ExternalEditKind.RESIZE,
                                   rect=PdfRect(row["box"][0], row["box"][1] - 8,
                                                row["box"][2] + 12, row["box"][3]))
        assert submit(manager, first_resize).success
        detail()
        second_resize = command_for(session, row, ExternalEditKind.RESIZE,
                                    rect=PdfRect(row["box"][0], row["box"][1] - 40,
                                                 row["box"][2] + 68, row["box"][3]))
        assert submit(manager, second_resize).success
        detail()
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_canonical_child_word_wraps_rich_text_and_only_breaks_overlong_tokens():
    """The child Form is word-aware without altering its semantic rich runs."""
    trace = helper("trace_canonical_word_wrap")
    words = trace["text"].split()
    prose = trace["prose"]
    assert [len(item["lines"]) for item in prose] == sorted(
        len(item["lines"]) for item in prose)
    for layout in prose:
        lines = layout["lines"]
        # All ordinary source words, including mappa whose style changes
        # from bold to regular mid-word, occupy exactly one visual line.
        for word in words:
            assert sum(line.split().count(word) for line in lines) == words.count(word), (word, lines)
        assert all(not line.startswith(" ") for line in lines)
        assert "FSourceBold" in {run["resource"] for run in layout["runs"]}
        assert "FSource" in {run["resource"] for run in layout["runs"]}
    long_token = trace["long_token"]
    assert len(long_token["lines"]) > 1
    assert "".join(long_token["lines"]) == "render_invoice_with_a_very_long_identifier()"
    assert all(run["resource"] == "FSource" for run in long_token["runs"])


def test_imported_callout_resize_layout_failure_rolls_back_atomically(
        app, tmp_path, monkeypatch, imported_source):
    """The historical resize failure point must leave the native Callout intact."""
    from tests.m151_helpers import callout_layout_failure_worker

    source = imported_source
    path = tmp_path / "imported-callout-rollback.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", callout_layout_failure_worker)
    manager = DocumentManager()
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        before = helper("inspect", path)
        row = find_native(before, CALLOUT, CALLOUT_PAGE)
        resize = command_for(session, row, ExternalEditKind.RESIZE,
                             rect=PdfRect(row["box"][0], row["box"][1],
                                          row["box"][2] + 12, row["box"][3] + 8))
        results = []
        manager.annotation_result.connect(results.append)
        try:
            assert manager.submit_annotation(resize).result is None
            wait_until(lambda: any(item.command_id == resize.context.command_id for item in results))
            result = next(item for item in results if item.command_id == resize.context.command_id)
        finally:
            manager.annotation_result.disconnect(results.append)
        assert not result.success and result.error.code.value == "internal_pdf_mutation"
        assert session.confirmed_revision == 0 and session.history_length == 0
        # Save after the failed post-update layout proves the persistent worker
        # survived. The parent AP has no reachable canonical child or fonts.
        output = tmp_path / "imported-callout-rollback-output.pdf"
        save(manager, session, output)
        after = helper("inspect", output)
        restored = find_native(after, CALLOUT, CALLOUT_PAGE)
        assert restored["xref"] == row["xref"]
        assert restored["keys"] == row["keys"]
        assert restored["rect"] == row["rect"] and restored["vertices"] == row["vertices"]
        assert "canonical_text" not in restored["appearance"]
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_external_callout_offpage_geometry_and_overflow_are_operation_scoped(candidate_session):
    manager, session, path = candidate_session
    before = helper("inspect", path)
    row = find_native(before, "callout")
    # Native annotation coordinates may extend beyond the CropBox.  These are
    # not creation targets, therefore no page-bound or text-fit precondition
    # may reject resize, anchor or text operations.
    anchored = command_for(session, row, ExternalEditKind.ANCHOR,
                           anchor=PdfPoint(-180, 492))
    assert submit(manager, anchored).success
    current = find_native(helper("inspect", path), "callout")
    resized = command_for(session, current, ExternalEditKind.RESIZE,
                          rect=PdfRect(-28, 286, 575, 408))
    assert submit(manager, resized).success
    current = find_native(helper("inspect", path), "callout")
    text = command_for(session, current, ExternalEditKind.TEXT,
                       rich_text=RichText.plain_text("x" * 4000, font_family="Helvetica",
                                                     font_size=10, color=RgbColor(0, 0, 0)))
    assert submit(manager, text).success
    assert session.confirmed_revision == 3


def test_post_write_external_rejection_rolls_back_and_recovers(app, native_source, tmp_path, monkeypatch):
    """No unacknowledged native change may survive a failed adoption step."""
    from tests.m151_helpers import fail_once_external_adoption_worker
    monkeypatch.setattr(document_service, "document_worker_main", fail_once_external_adoption_worker)
    manager = DocumentManager()
    path = tmp_path / "atomic-rejection.pdf"
    shutil.copyfile(native_source, path)
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        before = helper("inspect", path)
        row = find_native(before, "callout")
        rejected = command_for(session, row, ExternalEditKind.TEXT,
            rich_text=RichText.plain_text("Must not persist", font_family="Helvetica", font_size=10,
                                          color=RgbColor(0, 0, 0)))
        results = []
        manager.annotation_result.connect(results.append)
        try:
            assert manager.submit_annotation(rejected).result is None
            wait_until(lambda: any(item.command_id == rejected.context.command_id for item in results))
            result = next(item for item in results if item.command_id == rejected.context.command_id)
        finally:
            manager.annotation_result.disconnect(results.append)
        assert not result.success and result.error.code.value == "internal_pdf_mutation"
        assert session.confirmed_revision == 0
        output = tmp_path / "atomic-rejection-output.pdf"
        save(manager, session, output)
        after = helper("inspect", output)
        assert find_native(after, "callout")["keys"] == row["keys"]
        assert find_native(after, "callout")["vertices"] == row["vertices"]
        assert_native_object_restored(after["objects"][str(row["xref"])],
                                      before["objects"][str(row["xref"])])
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_no_pymupdf_in_pytest_process():
    assert "pymupdf" not in sys.modules and "fitz" not in sys.modules


def test_strict_canonical_ap_guard_rejects_without_heuristic_mutation(tmp_path):
    report = helper("patch_guard", tmp_path / "strict-guard.pdf")
    assert report == {"rejected": True, "root_unchanged": True,
                      "parent_not_heuristically_changed": True,
                      "parent_was_mutated_only_for_fixture": True}


def test_strict_canonical_ap_failure_rolls_back_external_normalization(
        app, native_source, tmp_path, monkeypatch):
    from tests.m151_helpers import shape_rejection_worker
    monkeypatch.setattr(document_service, "document_worker_main", shape_rejection_worker)
    path = tmp_path / "strict-rollback.pdf"
    shutil.copyfile(native_source, path)
    manager = DocumentManager()
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        before = helper("inspect", path)
        row = find_native(before, "rich")
        rejected = command_for(session, row, ExternalEditKind.RESIZE,
                               rect=PdfRect(row["box"][0], row["box"][1],
                                            row["box"][2] + 10, row["box"][3] + 10))
        results = []
        manager.annotation_result.connect(results.append)
        try:
            result = manager.submit_annotation(rejected)
            assert result.result is None
            wait_until(lambda: any(item.command_id == rejected.context.command_id for item in results))
        finally:
            manager.annotation_result.disconnect(results.append)
        assert not results[-1].success
        assert session.confirmed_revision == 0
        # The failed worker must remain usable: saving after rollback proves
        # that no partially-normalised parent or reachable child survives.
        output = tmp_path / "strict-rollback-output.pdf"
        save(manager, session, output)
        after = helper("inspect", output)
        assert find_native(after, "rich")["keys"] == row["keys"]
        assert find_native(after, "rich")["appearance"] == row["appearance"]
        assert_native_object_restored(after["objects"][str(row["xref"])],
                                      before["objects"][str(row["xref"])])
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_incremental_inventory_sidebar_and_detail(app, native_source, tmp_path, monkeypatch):
    from tests.m151_helpers import no_rescan_worker
    from pdf_annotator.app.annotation_sidebar import AnnotationSidebar
    monkeypatch.setattr(document_service, "document_worker_main", no_rescan_worker)
    manager = DocumentManager()
    path = tmp_path / "incremental.pdf"
    shutil.copyfile(native_source, path)
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        sidebar = AnnotationSidebar()
        sidebar.set_session(session)
        rows = {sidebar.list.item(i).data(256): sidebar.list.item(i) for i in range(sidebar.list.count())}
        manager.history_delta_changed.connect(lambda _id, delta: sidebar.apply_history_delta(session, delta))
        native = find_native(helper("inspect", path), "rich")
        cmd = command_for(session, native, ExternalEditKind.TEXT,
            rich_text=RichText.plain_text("Updated external detail", font_family="Helvetica", font_size=10,
                                         color=RgbColor(0, 0, 0)))
        submit(manager, cmd)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_request_id is None)
        assert sum(not is_attached_reply(item, session.annotation_inventory)
                   for item in session.annotation_inventory.values()) == len(rows)
        manager.request_annotation_detail(session.document_id, cmd.logical_id)
        wait_until(lambda: cmd.logical_id in session.annotation_details)
        assert session.annotation_details[cmd.logical_id].contents == "Updated external detail"
        for i in range(sidebar.list.count()):
            item = sidebar.list.item(i)
            if item.data(256) != cmd.logical_id:
                assert item is rows[item.data(256)]
        submit(manager, Undo(CommandContext.create(session.document_id, session.document_generation)))
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_request_id is None)
        assert session.annotation_inventory[cmd.logical_id].preview == "Native rich"
        sidebar.close()
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


def test_stale_detail_result_cannot_overwrite_external_edit(app, native_source, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from pdf_annotator.worker.protocol import Response
    from pdf_annotator.domain.results import HistoryDelta
    manager = DocumentManager()
    path = tmp_path / "stale.pdf"
    shutil.copyfile(native_source, path)
    try:
        session = _opened(manager, path)
        manager.start_annotation_inventory(session.document_id)
        wait_until(lambda: session.inventory_complete)
        summary = next(s for s in session.annotation_inventory.values() if s.kind == "text_box")
        worker = session.worker
        session.inventory_request_id = 900
        session.inventory_operation = Operation.ANNOTATION_DETAIL
        session.inventory_detail_id = summary.logical_id
        session.inventory_detail_buffer = "stale prefix"
        session.inventory_detail_offset = 12
        manager._apply_history_delta(session, HistoryDelta(inventory_updated=(replace(summary, preview="new"),)))
        requests = []
        response = SimpleNamespace(success=True, document_id=session.document_id,
            document_generation=session.document_generation, request_id=900,
            operation=Operation.ANNOTATION_DETAIL, payload={"logical_id": summary.logical_id,
                "contents": "stale text", "next_offset": 22, "complete": True})
        class Delivery:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        delivery = Delivery()
        delivery.response = response
        session.worker = SimpleNamespace(poll=lambda request_id: delivery,
            submit=lambda op, payload: requests.append((op, payload)) or 901)
        try:
            manager._poll_inventory(session)
            assert summary.logical_id not in session.annotation_details
            assert session.inventory_detail_buffer == ""
            assert requests == [(Operation.ANNOTATION_DETAIL, {"logical_id": summary.logical_id, "offset": 0})]
        finally:
            session.worker = worker
            session.inventory_request_id = None
    finally:
        manager.request_shutdown(discard_changes=True)
        wait_until(lambda: manager.is_shutdown)


@pytest.mark.parametrize("name,action", [("rich", "move"), ("font", "move"),
    ("ink", "move"), ("callout", "move"), ("callout", "resize"), ("callout", "anchor")])
def test_external_viewer_gestures_emit_one_command(app, native_source, tmp_path, monkeypatch, name, action):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtTest import QTest
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open, _point
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "viewer.pdf"
    shutil.copyfile(native_source, path)
    row = find_native(helper("inspect", path), name)
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        logical_id = f'native:0:{row["xref"]}'
        summary = session.annotation_inventory[logical_id]
        box = summary.geometry.text_rect or summary.geometry.bbox
        xy = ((box.x1, box.y1) if action == "resize" else
              (summary.geometry.callout_line[0].x, summary.geometry.callout_line[0].y) if action == "anchor" else
              ((box.x0+box.x1)/2, (box.y0+box.y1)/2))
        start, end = _point(view, 0, xy), _point(view, 0, (xy[0]+8, xy[1]+6))
        before = session.worker.submitted_counts[Operation.ANNOTATION_COMMAND]
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end, delay=2)
        assert view.external_interaction.gesture is not None
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == before
        # Preview is local geometry only.  It must not borrow toolbar style or
        # rewrite the external summary before the one release command.
        if action == "resize":
            # Normalisation emits the native solid-border token.  The visual
            # style inputs themselves must remain unchanged.
            for key in ("stroke", "fill", "text", "opacity", "width"):
                assert session.annotation_inventory[logical_id].style.get(key) == summary.style.get(key)
        else:
            assert session.annotation_inventory[logical_id].style == summary.style
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        wait_until(lambda: session.document_revision == 1)
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == before + 1
        assert session.annotation_inventory[logical_id] != summary
        if action == "resize":
            for key in ("stroke", "fill", "text", "opacity", "width"):
                assert session.annotation_inventory[logical_id].style.get(key) == summary.style.get(key)
        else:
            assert session.annotation_inventory[logical_id].style == summary.style
        if name == "callout" and action == "move":
            assert session.annotation_inventory[logical_id].geometry.callout_line[0] == \
                summary.geometry.callout_line[0]
        if name == "callout" and action == "anchor":
            assert session.annotation_inventory[logical_id].geometry.callout_line[0] != \
                summary.geometry.callout_line[0]
        assert view.selected_inventory_id == logical_id
        assert not session.annotations
        # A subsequent cancelled gesture changes no native state/revision.
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        QTest.mouseMove(view.viewport(), start, delay=2)
        QTest.keyClick(view, Qt.Key.Key_Escape)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        assert view.external_interaction.gesture is None
        assert session.worker.submitted_counts[Operation.ANNOTATION_COMMAND] == before + 1
    finally:
        close_window_discard(window)


@pytest.mark.parametrize("name,position", [("rich", (150, 200)), ("callout", (430, 350))])
def test_external_existing_editor_and_read_only_affordances(app, native_source, tmp_path, monkeypatch, name, position):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open, _point
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "editor.pdf"
    shutil.copyfile(native_source, path)
    rows = helper("inspect", path)
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        row = find_native(rows, name)
        logical_id = f'native:0:{row["xref"]}'
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, position))
        wait_until(lambda: view._text_editor is not None)
        assert view._editing_text_box_id == logical_id
        assert view._text_editor.toPlainText() == "Native rich"
        view._text_editor.setPlainText("Updated in the existing editor")
        view.set_text_box_fill("pale-blue")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        assert view._text_editor is None
        assert session.annotation_inventory[logical_id].preview == "Updated in the existing editor"
        assert not session.annotations
        font = find_native(rows, "font")
        summary = session.annotation_inventory[f'native:0:{font["xref"]}']
        assert view.external_interaction.action_at(summary, _point(view, 0, (560, 120))) == "br"
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (430, 80)))
        wait_until(lambda: view._text_editor is not None)
        QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)
        unknown = find_native(rows, "unknown")
        summary = session.annotation_inventory[f'native:0:{unknown["xref"]}']
        assert view.external_interaction.action_at(summary, _point(view, 0, (560, 240))) is not None
        assert session.document_revision == 1
    finally:
        close_window_discard(window)


def test_external_editor_hides_only_its_native_text_appearance_until_escape_or_commit(
        app, native_source, tmp_path, monkeypatch):
    """Qt regression: an imported AP never paints below its active QTextEdit."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtTest import QTest
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open, _point
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "external-overlay.pdf"
    shutil.copyfile(native_source, path)
    row = find_native(helper("inspect", path), "font")
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        logical_id = f'native:0:{row["xref"]}'
        summary = session.annotation_inventory[logical_id]
        painted: list[str] = []

        def native_appearance(_painter, _mapper, item, **_kwargs):
            painted.append(item.logical_id)
            return True

        monkeypatch.setattr(view, "_paint_native_appearance", native_appearance)

        def paint_page():
            image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            try:
                view._paint_overlays(painter, summary.page_index)
            finally:
                painter.end()

        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (430, 80)))
        wait_until(lambda: view._text_editor is not None and view._editing_text_box_id == logical_id)
        painted.clear()
        paint_page()
        assert logical_id not in painted

        QTest.keyClick(view._text_editor, Qt.Key.Key_Escape)
        wait_until(lambda: view._text_editor is None)
        painted.clear()
        paint_page()
        assert painted.count(logical_id) == 1

        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                           pos=_point(view, 0, (430, 80)))
        wait_until(lambda: view._text_editor is not None)
        view._text_editor.setPlainText("Native rick")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1 and view._text_editor is None)
        painted.clear()
        paint_page()
        assert painted.count(logical_id) == 1
    finally:
        close_window_discard(window)


def test_imported_callout_move_and_anchor_retire_previous_native_appearance(
        app, tmp_path, monkeypatch, imported_source):
    """A committed native edit must not reuse an AP from the prior geometry."""
    from PySide6.QtGui import QImage, QPainter
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.viewer.external_interaction import NativeGesture
    from tests.test_freetext_viewer_m8 import _open
    from tests.conftest import close_window_discard

    source = imported_source
    path = tmp_path / "imported-callout-native-preview.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        row = find_native(helper("inspect", path), CALLOUT, CALLOUT_PAGE)
        logical_id = f'native:{CALLOUT_PAGE}:{row["xref"]}'
        view.verticalScrollBar().setValue(round(view.page_layout.pages[CALLOUT_PAGE].y))
        wait_until(lambda: CALLOUT_PAGE in view.scheduler.visible)
        summary = session.annotation_inventory[logical_id]
        wait_until(lambda: view.scheduler.budget.get(view._appearance_key(summary), record=False) is not None)
        fallback: list[str] = []
        original_fallback = view._paint_external_freetext

        def record_fallback(*args, **kwargs):
            fallback.append(args[2].logical_id)
            return original_fallback(*args, **kwargs)

        monkeypatch.setattr(view, "_paint_external_freetext", record_fallback)

        def paint_current() -> None:
            current = session.annotation_inventory[logical_id]
            image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            try:
                view.external_interaction.paint(
                    painter, view.coordinate_mapper(current.page_index), current, False)
            finally:
                painter.end()

        def assert_native_bridge(command, action: str) -> None:
            before = session.annotation_inventory[logical_id]
            text_box = before.geometry.text_rect or before.geometry.bbox
            source_key = view._appearance_key(before)
            preview = NativeGesture(
                before, PdfPoint(before.geometry.bbox.x0, before.geometry.bbox.y0), action, text_box,
                before.geometry.callout_line[0] if before.geometry.callout_line else None, command,
                source_key)
            # Exercise the same result handoff as a mouse release. The worker
            # mutates only through the command; this is transient paint state.
            view.external_interaction.pending = command
            view.external_interaction.pending_preview = preview
            # Stop the next asynchronous AP request so the invalidation state
            # is inspectable before a fresh worker raster can arrive.
            view.scheduler.active = False
            assert submit(window.manager, command).success
            current = session.annotation_inventory[logical_id]
            current_key = view._appearance_key(current)
            assert view.scheduler.budget.get(current_key, record=False) is None
            # The prior AP cannot describe the committed geometry, even when
            # the logical cache key is stable across unrelated revisions.
            assert view._native_appearance_entry(current) is None
            assert view.external_interaction.transition is None
            fallback.clear()
            view._last_native_appearance_paint = None
            paint_current()
            assert fallback == []
            assert view._last_native_appearance_paint is None
            view.scheduler.active = True
            wait_until(lambda: view.scheduler.budget.get(view._appearance_key(
                session.annotation_inventory[logical_id]), record=False) is not None)
            fallback.clear()
            paint_current()
            assert fallback == []
            assert view.external_interaction.transition is None
            current_paint = view._last_native_appearance_paint
            assert current_paint is not None
            assert current_paint.source == "native_ap_raster"
            assert current_paint.source_key == view._appearance_key(
                session.annotation_inventory[logical_id])

        assert_native_bridge(command_for(session, row, ExternalEditKind.MOVE, delta=(8, 6)), "move")
        assert_native_bridge(command_for(session, row, ExternalEditKind.ANCHOR,
                                         anchor=PdfPoint(row["vertices"][0][0] + 12,
                                                         row["vertices"][0][1] + 140)), "anchor")
    finally:
        close_window_discard(window)


def test_imported_callout_history_reuses_only_the_matching_native_ap_snapshot(app, tmp_path, monkeypatch, imported_source):
    """Undo/Redo must not stretch a raster from a different Callout history step."""
    from PySide6.QtGui import QImage, QPainter
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.domain.callout_geometry import callout_attachment_side, callout_line
    from tests.test_freetext_viewer_m8 import _open
    from tests.conftest import close_window_discard

    source = imported_source
    path = tmp_path / "imported-callout-history-appearance.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    window = MainWindow(); window.resize(1100, 1050); window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        row = find_native(helper("inspect", path), CALLOUT, CALLOUT_PAGE)
        logical_id = f'native:{CALLOUT_PAGE}:{row["xref"]}'
        view.verticalScrollBar().setValue(round(view.page_layout.pages[CALLOUT_PAGE].y))
        wait_until(lambda: CALLOUT_PAGE in view.scheduler.visible)

        def current():
            return session.annotation_inventory[logical_id]

        semantic_fallback: list[str] = []
        original_fallback = view._paint_external_freetext

        def record_fallback(*args, **kwargs):
            if args[2].logical_id == logical_id:
                semantic_fallback.append(logical_id)
            return original_fallback(*args, **kwargs)

        monkeypatch.setattr(view, "_paint_external_freetext", record_fallback)

        def cache_current():
            state = current()
            wait_until(lambda: view._has_current_native_appearance(state))
            assert view._appearance_sources[view._appearance_key(state)] == state
            return state

        def paint_history_state(expected):
            state = current()
            assert state.geometry == expected.geometry
            assert view.external_interaction.transition is None
            assert view._has_current_native_appearance(state) is False
            # Undo/Redo has no GUI-side AP identity. It must never choose a
            # retained combined Callout raster merely because its summary
            # resembles the restored geometry.
            assert view._native_appearance_entry(state) is None
            view._last_native_appearance_paint = None
            semantic_fallback.clear()
            image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            try:
                view.external_interaction.paint(
                    painter, view.coordinate_mapper(state.page_index), state, False)
            finally:
                painter.end()
            assert view._last_native_appearance_paint is None
            assert semantic_fallback == []
            view.scheduler.active = True
            wait_until(lambda: view._has_current_native_appearance(current()))
            image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            try:
                view.external_interaction.paint(
                    painter, view.coordinate_mapper(state.page_index), current(), False)
            finally:
                painter.end()
            frame = view._last_native_appearance_paint
            assert frame is not None
            assert frame.source == "native_ap_raster"
            assert frame.source_key == frame.cache_key
            assert frame.annotation_rect == state.geometry.bbox
            assert semantic_fallback == []
            return frame

        initial = cache_current()
        box0, line0 = initial.geometry.text_rect, initial.geometry.callout_line
        assert box0 is not None and len(line0) == 3
        assert submit(window.manager, command_for(
            session, row, ExternalEditKind.MOVE, delta=(8, 6))).success
        move_a = cache_current()
        live_image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
        live_painter = QPainter(live_image)
        try:
            view.external_interaction.paint(
                live_painter, view.coordinate_mapper(move_a.page_index), move_a, False)
        finally:
            live_painter.end()
        move_a_frame = view._last_native_appearance_paint
        assert move_a_frame is not None
        assert move_a.geometry.callout_line[0] == line0[0]
        assert submit(window.manager, command_for(
            session, row, ExternalEditKind.MOVE, delta=(-5, 11))).success
        move_b = cache_current()
        assert move_b.geometry.callout_line[0] == line0[0]
        assert submit(window.manager, command_for(
            session, row, ExternalEditKind.ANCHOR,
            anchor=PdfPoint(line0[0].x + 22, line0[0].y - 17))).success
        anchored = cache_current()
        assert (anchored.geometry.text_rect.x0, anchored.geometry.text_rect.y0,
                anchored.geometry.text_rect.x1, anchored.geometry.text_rect.y1) == pytest.approx(
                    (move_b.geometry.text_rect.x0, move_b.geometry.text_rect.y0,
                     move_b.geometry.text_rect.x1, move_b.geometry.text_rect.y1), abs=.001)
        expected_line = callout_line(move_b.geometry.text_rect, anchored.geometry.callout_line[0])
        for actual, expected in zip(anchored.geometry.callout_line, expected_line, strict=True):
            assert (actual.x, actual.y) == pytest.approx((expected.x, expected.y), abs=.001)

        for expected in (move_b, move_a, initial):
            view.scheduler.active = False
            assert submit(window.manager, Undo(CommandContext.create(
                session.document_id, session.document_generation))).success
            frame = paint_history_state(expected)
            if expected is move_a:
                # The Callout sequence is move A -> move B -> Undo.
                # Its restored PDF shell and fresh native AP must be A exactly,
                # not a geometry reconstruction from B.
                assert frame.appearance_coverage == move_a_frame.appearance_coverage
                assert frame.clip_to == move_a_frame.clip_to
                assert frame.destination == pytest.approx(move_a_frame.destination)
        for expected in (move_a, move_b, anchored):
            view.scheduler.active = False
            assert submit(window.manager, Redo(CommandContext.create(
                session.document_id, session.document_generation))).success
            paint_history_state(expected)
    finally:
        close_window_discard(window)


def test_imported_callout_native_ap_gesture_frames_are_rigid(
        app, tmp_path, monkeypatch, imported_source):
    """Trace idle, move and anchor frames without invoking semantic FreeText paint."""
    from PySide6.QtGui import QImage, QPainter
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.domain.callout_geometry import callout_attachment_side, callout_line
    from pdf_annotator.viewer.external_interaction import NativeGesture
    from tests.test_freetext_viewer_m8 import _open
    from tests.conftest import close_window_discard

    source = imported_source
    path = tmp_path / "imported-callout-frames.pdf"
    shutil.copyfile(source, path)
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    window = MainWindow(); window.resize(1100, 1050); window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        fallback: list[str] = []
        original_fallback = view._paint_external_freetext

        def record_fallback(*args, **kwargs):
            fallback.append(args[2].logical_id)
            return original_fallback(*args, **kwargs)

        monkeypatch.setattr(view, "_paint_external_freetext", record_fallback)
        preview_lines: list[tuple[PdfPoint, ...]] = []
        native_leader = view._paint_external_callout_leader

        def record_leader(*args, **kwargs):
            preview_lines.append(args[2])
            return native_leader(*args, **kwargs)

        monkeypatch.setattr(view, "_paint_external_callout_leader", record_leader)

        def paint(summary):
            image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            try:
                view.external_interaction.paint(
                    painter, view.coordinate_mapper(summary.page_index), summary, False)
            finally:
                painter.end()
            frame = view._last_native_appearance_paint
            assert frame is not None
            return frame

        for name in (CALLOUT, OTHER_CALLOUT):
            summary = next(item for item in session.annotation_inventory.values()
                           if item.native_name == name)
            view.verticalScrollBar().setValue(round(view.page_layout.pages[summary.page_index].y))
            wait_until(lambda: summary.page_index in view.scheduler.visible)
            wait_until(lambda: view._has_current_native_appearance(summary))
            fallback.clear()
            idle = paint(summary)
            assert idle.annotation_id == summary.logical_id
            assert idle.source == "native_ap_raster"
            assert idle.source_key == idle.cache_key
            assert idle.pixel_size[0] > 0 and idle.pixel_size[1] > 0
            assert idle.appearance_coverage[2] > idle.appearance_coverage[0]
            assert idle.appearance_coverage[3] > idle.appearance_coverage[1]
            assert idle.annotation_rect == summary.geometry.bbox
            assert fallback == []

            dx, dy = 8, 6
            move = view.external_interaction.command(summary, ExternalEditKind.MOVE, delta=(dx, dy))
            box = summary.geometry.text_rect or summary.geometry.bbox
            view.external_interaction.gesture = NativeGesture(
                summary, PdfPoint(box.x0, box.y0), "move", box,
                summary.geometry.callout_line[0] if summary.geometry.callout_line else None, move)
            fallback.clear()
            moved = paint(summary)
            mapper = view.coordinate_mapper(summary.page_index)
            origin, shifted = mapper.canonical_to_viewport((0, 0)), mapper.canonical_to_viewport((dx, dy))
            assert moved.source == "native_ap_raster"
            assert moved.pixel_size == idle.pixel_size
            assert moved.destination[2:] == pytest.approx(idle.destination[2:])
            assert moved.destination[:2] == pytest.approx((
                idle.destination[0] + shifted[0] - origin[0],
                idle.destination[1] + shifted[1] - origin[1]))
            expected_move = callout_line(
                PdfRect(box.x0 + dx, box.y0 + dy, box.x1 + dx, box.y1 + dy),
                summary.geometry.callout_line[0])
            assert preview_lines[-1] == expected_move
            assert fallback == []

            if name == CALLOUT:
                unrelated = next(item for item in session.annotation_inventory.values()
                                 if item.native_name == OTHER_CALLOUT)
                wait_until(lambda: view._has_current_native_appearance(unrelated))
                fallback.clear()
                unrelated_frame = paint(unrelated)
                assert unrelated_frame.source == "native_ap_raster"
                assert fallback == []

            anchor = summary.geometry.callout_line[0]
            anchor_command = view.external_interaction.command(
                summary, ExternalEditKind.ANCHOR, anchor=PdfPoint(anchor.x + 12, anchor.y + 9))
            view.external_interaction.gesture = NativeGesture(
                summary, PdfPoint(anchor.x, anchor.y), "anchor", box,
                PdfPoint(anchor.x + 12, anchor.y + 9), anchor_command)
            fallback.clear()
            anchored = paint(summary)
            assert anchored.source == "native_ap_raster"
            assert anchored.pixel_size == idle.pixel_size
            assert anchored.destination == pytest.approx(idle.destination)
            expected_anchor = callout_line(box, anchor_command.anchor)
            assert preview_lines[-1] == expected_anchor
            assert fallback == []
            view.external_interaction.gesture = None
    finally:
        close_window_discard(window)


def test_callout_native_appearance_clips_synthetic_leader_to_its_exterior(app, native_source, tmp_path,
                                                                            monkeypatch):
    """The AP owns in-Rect leader and arrow; the Qt overlay owns only overflow."""
    from PySide6.QtGui import QImage, QPainter
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "callout-leader-clip.pdf"
    shutil.copyfile(native_source, path)
    row = find_native(helper("inspect", path), "callout")
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        summary = session.annotation_inventory[f'native:0:{row["xref"]}']
        calls = []
        native_clips = []

        def native_appearance(*_args, **kwargs):
            native_clips.append(kwargs.get("clip_to"))
            return True

        monkeypatch.setattr(view, "_paint_native_appearance", native_appearance)
        original = view._paint_external_callout_leader

        def leader(*args, **kwargs):
            calls.append(kwargs.get("covered"))
            return original(*args, **kwargs)

        monkeypatch.setattr(view, "_paint_external_callout_leader", leader)
        image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        try:
            view._paint_inventory_annotation(painter, view.coordinate_mapper(summary.page_index), summary, False)
        finally:
            painter.end()
        assert native_clips == [summary.geometry.text_rect]
        assert calls == [None]
    finally:
        close_window_discard(window)


def test_callout_native_ap_box_clip_leaves_one_overlay_arrow_at_cl_anchor(app, native_source, tmp_path,
                                                                            monkeypatch):
    """The displayed Callout has one Qt OpenArrow, with its tip exactly at /CL[0]."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open
    from tests.conftest import close_window_discard

    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "callout-one-arrow.pdf"
    shutil.copyfile(native_source, path)
    row = find_native(helper("inspect", path), "callout")
    window = MainWindow(); window.resize(1100, 1050); window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        summary = session.annotation_inventory[f'native:0:{row["xref"]}']
        wait_until(lambda: view._has_current_native_appearance(summary))
        box = summary.geometry.text_rect
        anchor = summary.geometry.callout_line[0]
        assert box is not None and not (box.x0 <= anchor.x <= box.x1 and box.y0 <= anchor.y <= box.y1)
        mapper = view.coordinate_mapper(summary.page_index)
        native_only = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
        native_only.fill(Qt.GlobalColor.transparent)
        painter = QPainter(native_only)
        try:
            assert view._paint_native_appearance(painter, mapper, summary, clip_to=box)
        finally:
            painter.end()
        anchor_px = mapper.canonical_to_viewport((anchor.x, anchor.y))
        ax, ay = round(anchor_px[0]), round(anchor_px[1])
        assert all(native_only.pixelColor(px, py).alpha() == 0
                   for px in range(ax - 2, ax + 3) for py in range(ay - 2, ay + 3))
        tips = []
        original = view._paint_open_arrow

        def paint_arrow(painter, tip, toward):
            tips.append((tip.x(), tip.y()))
            return original(painter, tip, toward)

        monkeypatch.setattr(view, "_paint_open_arrow", paint_arrow)
        image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            view._paint_inventory_annotation(painter, mapper, summary, False)
        finally:
            painter.end()
        native = view._last_native_appearance_paint
        leader = view._last_callout_leader_paint
        assert native is not None and native.clip_to == box
        assert leader is not None and leader.arrow_tip == anchor
        expected_tip = mapper.canonical_to_viewport((anchor.x, anchor.y))
        assert tips == [pytest.approx(expected_tip)]
    finally:
        close_window_discard(window)


def test_callout_open_arrow_overlay_keeps_wings_inside_overlay_bounds(app, native_source, tmp_path,
                                                                      monkeypatch):
    """The leader overlay includes all OpenArrow paint beyond its /CL centreline."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.domain.callout_geometry import callout_line
    from tests.test_freetext_viewer_m8 import _open
    from tests.conftest import close_window_discard

    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "callout-open-arrow-overlay.pdf"
    shutil.copyfile(native_source, path)
    row = find_native(helper("inspect", path), "callout")
    window = MainWindow(); window.resize(1100, 1050); window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        summary = session.annotation_inventory[f'native:0:{row["xref"]}']
        box = summary.geometry.text_rect or summary.geometry.bbox
        # This is a near-boundary synthetic anchor. /CL itself is deliberately
        # not persisted or changed here.
        line = callout_line(box, PdfPoint(box.x0 + 1, box.y0 - 1), side="left")
        mapper = view.coordinate_mapper(summary.page_index)
        image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            view._paint_external_callout_leader(painter, mapper, line, QColor("black"), 1.0)
        finally:
            painter.end()
        paint = view._last_callout_leader_paint
        assert paint is not None
        arrow = QRectF(*paint.arrow_bounds)
        overlay = QRectF(*paint.overlay_bounds)
        assert paint.arrow_tip == line[0]
        assert overlay.contains(arrow)
        # The complete arrow stays in its dedicated leader overlay regardless
        # of whether a synthetic near-corner placement overlaps the box clip.
        assert any(image.pixelColor(x, y).alpha() > 0
                   for x in range(max(0, round(arrow.left())),
                                  min(image.width(), round(arrow.right()) + 1))
                   for y in range(max(0, round(arrow.top())),
                                  min(image.height(), round(arrow.bottom()) + 1)))
    finally:
        close_window_discard(window)


def test_external_callout_fit_reject_cleans_overlay_and_recovers_interaction(app, native_source, tmp_path,
                                                                               monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open, _point
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "callout-reject-ui.pdf"
    shutil.copyfile(native_source, path)
    rows = helper("inspect", path)
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        callout = find_native(rows, "callout")
        logical_id = f'native:0:{callout["xref"]}'
        other_row = find_native(rows, "callout-small")
        other_id = f'native:0:{other_row["xref"]}'
        leader_paints = []
        native_leader = tuple(session.annotation_inventory[logical_id].geometry.callout_line)
        original_paint_leader = view._paint_external_callout_leader

        def paint_leader(*args):
            if args[2] == native_leader:
                leader_paints.append(args[2])
            return original_paint_leader(*args)

        monkeypatch.setattr(view, "_paint_external_callout_leader", paint_leader)
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_point(view, 0, (430, 350)))
        wait_until(lambda: view._text_editor is not None)
        rejected_editor = view._text_editor
        rejected_editor.setPlainText("x" * 4000)
        QTest.keyClick(rejected_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: view._text_editor is None)
        wait_until(lambda: session.document_revision == 1)
        assert view.external_interaction.pending is None
        assert view.external_interaction.pending_editor is None
        assert view.external_interaction.pending_preview is None
        assert view.external_interaction.gesture is None
        assert view.external_interaction.waiting_editor is None
        assert session.pending_mutations == 0
        assert session.annotation_inventory[logical_id].geometry.callout_line
        assert view.selected_inventory_id == logical_id
        assert QWidget.mouseGrabber() is None
        assert QApplication.focusWidget() is not rejected_editor
        view.viewport().update()
        wait_until(lambda: bool(leader_paints))
        unchanged = find_native(helper("inspect", path), "callout")
        for key in ("Rect", "RD", "CL"):
            assert unchanged["keys"][key] == callout["keys"][key]

        # The rejected editor no longer intercepts a resize or selection click.
        summary = session.annotation_inventory[logical_id]
        box = summary.geometry.text_rect
        corner = _point(view, 0, (box.x1, box.y1))
        larger = _point(view, 0, (box.x1 + 30, box.y1 + 24))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        assert view.external_interaction.gesture is not None
        assert view.external_interaction.gesture.action in ("br", "tr")
        QTest.mouseMove(view.viewport(), larger, delay=2)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=larger)
        wait_until(lambda: session.document_revision == 2)
        other = session.annotation_inventory[other_id]
        other_box = other.geometry.text_rect
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=_point(view, 0, ((other_box.x0 + other_box.x1) / 2,
                                               (other_box.y0 + other_box.y1) / 2)))
        assert view.selected_inventory_id == other_id
        assert view.external_interaction.gesture is None

        summary = session.annotation_inventory[logical_id]
        box = summary.geometry.text_rect
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                          pos=_point(view, 0, ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)))
        wait_until(lambda: view._text_editor is not None and view._editing_text_box_id == logical_id)
        view._text_editor.setPlainText("Recovered callout")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 3)
    finally:
        close_window_discard(window)


def test_unmapped_freetext_exposes_full_native_editing(app, native_source, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open, _point
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "geometry-only-ui.pdf"
    shutil.copyfile(native_source, path)
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        row = find_native(helper("inspect", path), "font")
        summary = session.annotation_inventory[f'native:0:{row["xref"]}']
        corner = _point(view, summary.page_index, (summary.geometry.text_rect.x1, summary.geometry.text_rect.y1))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        assert view.external_interaction.gesture is not None
        assert view.external_interaction.gesture.action in ("br", "tr")
        QTest.keyClick(view, Qt.Key.Key_Escape)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        assert view.external_interaction.gesture is None
        assert view.external_interaction.request_editor(summary)
        wait_until(lambda: view._text_editor is not None)
    finally:
        close_window_discard(window)


def test_small_external_callout_editor_preserves_native_geometry_and_recovers(app, native_source, tmp_path,
                                                                                monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open, _point
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "small-callout-ui.pdf"
    shutil.copyfile(native_source, path)
    before = helper("inspect", path)
    row = find_native(before, "callout-small")
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        logical_id = f'native:0:{row["xref"]}'
        summary = session.annotation_inventory[logical_id]
        box = summary.geometry.text_rect
        center = _point(view, 0, ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2))
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        wait_until(lambda: view._text_editor is not None)
        assert session.document_revision == 0
        unchanged = helper("inspect", path)
        current = find_native(unchanged, "callout-small")
        for key in ("Rect", "RD", "CL", "AP"):
            assert current["keys"][key] == row["keys"][key]
        view._text_editor.setPlainText("x" * 4000)
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: view._text_editor is None)
        wait_until(lambda: session.document_revision == 1)
        corner = _point(view, 0, (box.x1, box.y1))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
        assert view.external_interaction.gesture is not None
        action = view.external_interaction.gesture.action
        assert action in ("tl", "tr", "bl", "br")
        x = box.x0 - 36 if "l" in action else box.x1 + 36
        y = box.y0 - 30 if "t" in action else box.y1 + 30
        larger = _point(view, 0, (x, y))
        QTest.mouseMove(view.viewport(), larger, delay=2)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=larger)
        wait_until(lambda: session.document_revision == 2)
        summary = session.annotation_inventory[logical_id]
        box = summary.geometry.text_rect
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                          pos=_point(view, 0, ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)))
        wait_until(lambda: view._text_editor is not None)
        view._text_editor.setPlainText("YES")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 3)
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                          pos=_point(view, 0, ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)))
        wait_until(lambda: view._text_editor is not None)
        view._text_editor.setPlainText("x" * 4000)
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: view._text_editor is None)
        wait_until(lambda: session.document_revision == 4)
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                          pos=_point(view, 0, ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)))
        wait_until(lambda: view._text_editor is not None)
        view._text_editor.setPlainText("Second edit")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 5)
    finally:
        close_window_discard(window)


def test_plain_full_external_freetext_opens_existing_editor(app, native_source, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from pdf_annotator.app.main_window import MainWindow
    from tests.test_freetext_viewer_m8 import _open, _point
    from tests.conftest import close_window_discard
    monkeypatch.setattr(document_service, "document_worker_main", candidate_worker)
    path = tmp_path / "plain-full-ui.pdf"
    shutil.copyfile(native_source, path)
    row = find_native(helper("inspect", path), "plain-full")
    assert row["profile"] == "freetext-plain-v1"
    window = MainWindow()
    window.resize(1100, 1050)
    window.show()
    try:
        session, view = _open(window, path)
        wait_until(lambda: session.inventory_complete)
        logical_id = f'native:0:{row["xref"]}'
        summary = session.annotation_inventory[logical_id]
        assert summary.style["external_profile"] == "freetext-plain-v1"
        box = summary.geometry.text_rect
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                          pos=_point(view, 0, ((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)))
        wait_until(lambda: view._text_editor is not None)
        assert view._editing_text_box_id == logical_id
        view._text_editor.setPlainText("Updated plain")
        view.set_text_box_fill("pale-blue")
        QTest.keyClick(view._text_editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        wait_until(lambda: session.document_revision == 1)
        # First confirmed text/style edit is a layout operation: plain native
        # FreeText adopts the normal rich native representation.
        assert session.annotation_inventory[logical_id].style["external_profile"] == "freetext-rich-v1"
    finally:
        close_window_discard(window)
