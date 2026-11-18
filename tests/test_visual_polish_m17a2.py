"""Focused structural checks for M17A.2's presentation-only chrome."""

from pathlib import Path

import pytest
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidgetAction

from pdf_annotator.app.main_window import MainWindow
from pdf_annotator.app.ui_chrome import COLORS, CenteredMenuButton, icon, stroke_swatch_icon
from pdf_annotator.domain.styles import PEN_COLOR_PRESETS
from tests.conftest import close_window_discard, wait_until
from tests.test_m6_viewer import _open


@pytest.fixture
def polished_window(tmp_path: Path, app, pdf_fixture_copy):
    path = tmp_path / "polish.pdf"
    pdf_fixture_copy(path)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    yield window, path
    close_window_discard(window)


def test_vertical_icon_palette_preserves_existing_tool_actions(polished_window):
    window, path = polished_window
    session, view = _open(window, path)
    layout = window.annotation_palette.layout()
    assert isinstance(layout, QVBoxLayout)
    assert [button.defaultAction() for button in (window.palette_pen_button,
                                                   window.palette_text_box_button,
                                                   window.palette_callout_button)] == [
        window.pen_action, window.text_box_action, window.callout_action]
    assert all(button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
               and button.width() == button.height() == 38
               and not button.icon().isNull()
               for button in (window.palette_pen_button, window.palette_text_box_button,
                              window.palette_callout_button))
    QTest.mouseClick(window.palette_pen_button, Qt.MouseButton.LeftButton)
    assert view.pen_enabled and window.palette_pen_button.isChecked()
    assert session.ui.pen_color == "red" and window.pen_color_actions["red"].isChecked()
    actual = window.context_pen_color_button.icon().pixmap(20, 20).toImage()
    expected = stroke_swatch_icon(dict(PEN_COLOR_PRESETS)["red"]).pixmap(20, 20).toImage()
    assert tuple(actual.pixel(x, y) for y in range(20) for x in range(20)) == tuple(
        expected.pixel(x, y) for y in range(20) for x in range(20))


def test_pdf_text_operation_icons_are_distinct_painter_glyphs(polished_window):
    window, _path = polished_window
    buttons = (window.context_highlight_button, window.context_delete_button,
               window.context_replace_button)
    assert all(button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
               and not button.icon().isNull() for button in buttons)

    def pixels(name: str) -> tuple[int, ...]:
        image = icon(name, size=24).pixmap(24, 24).toImage()
        return tuple(image.pixel(x, y) for y in range(image.height()) for x in range(image.width()))

    # The shared A keeps these operations related; marker, strike and
    # replacement arrow still distinguish the three meanings.
    assert len({pixels("highlight"), pixels("delete"), pixels("replace")}) == 3


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0, 3.0])
@pytest.mark.parametrize("size", [20, 24])
@pytest.mark.parametrize("name", ["pen", "textbox", "callout", "highlight", "delete", "replace",
                                 "chevron-up", "chevron-down", "minimize", "maximize", "restore", "close"])
def test_line_icons_have_crisp_monochrome_pixels_at_display_density(app, name, dpr, size):
    drawing = icon(name, size=size)
    images = []
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
        pixmap = drawing.pixmap(QSize(size, size), dpr, mode)
        assert pixmap.width() == pixmap.height() == round(size * dpr)
        assert pixmap.devicePixelRatio() == pytest.approx(dpr)
        image = pixmap.toImage()
        # Thin axis-aligned caption strokes can straddle two physical rows at
        # 100% density; both antialiased rows still need visible foreground.
        alpha_floor = 128 if name in {"minimize", "maximize", "restore", "close"} else 200
        visible = [image.pixelColor(x, y) for y in range(image.height())
                   for x in range(image.width()) if image.pixelColor(x, y).alpha() > alpha_floor]
        assert len(visible) > 8
        expected = QColor(COLORS.primary if mode == QIcon.Mode.Normal else COLORS.disabled)
        # Unpremultiplication of antialiased edge pixels can round RGB by one.
        assert all(abs(actual - target) <= 2 for pixel in visible
                   for actual, target in zip(pixel.getRgb()[:3], expected.getRgb()[:3]))
        images.append(image)
    assert images[0] != images[1]


@pytest.mark.parametrize("tool", ["pen", "text_box", "callout"])
def test_polished_creation_buttons_keep_toggle_and_neutral_behavior(polished_window, tool):
    window, path = polished_window
    session, view = _open(window, path)
    button = getattr(window, f"palette_{tool}_button")
    action = getattr(window, f"{tool}_action")
    assert button.defaultAction() is action
    before = button.icon().pixmap(20, 20).toImage()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert button.isChecked() and action.isChecked()
    assert getattr(view, f"{tool}_enabled")
    assert button.icon().pixmap(20, 20).toImage() != before
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert not button.isChecked() and not action.isChecked()
    assert not getattr(view, f"{tool}_enabled")
    assert session.document_revision == 0 and not session.annotations


def test_context_capsule_reuses_swatch_popups_and_never_mutates_document(polished_window):
    window, path = polished_window
    session, view = _open(window, path)
    assert (session.ui.text_box_fill, session.ui.text_box_border,
            session.ui.text_box_font_family, session.ui.text_box_font_size) == (
        "pale-yellow", "red", "Helvetica", 10.0)
    view.set_text_box_enabled(True)
    wait_until(window.creation_properties.isVisible)
    assert not window.context_property_label.isVisible()
    assert window._context_text_controls == (
        window.context_text_fill_button, window.context_text_border_button,
        window.context_text_color_button, window.context_bold_button,
        window.context_italic_button, window.context_text_font_button,
        window.context_text_size_spin, window.context_size_down_button,
        window.context_size_up_button)
    assert window.context_pen_color_button.menu() is window.pen_color_button.menu()
    assert window.context_text_fill_button.menu() is window.text_fill_button.menu()
    assert window.context_text_border_button.menu() is window.text_border_button.menu()
    assert window.context_text_color_button.menu() is window.text_color_button.menu()
    assert all(isinstance(action, QWidgetAction)
               for action in window.pen_color_button.menu().actions())
    swatches = window.pen_color_button.menu().findChildren(QToolButton)
    assert len(swatches) == len(window.pen_color_actions)
    assert all(button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
               and not button.icon().isNull()
               and button.property("swatch_role") == "stroke"
               for button in swatches)
    popup = window.pen_color_button.menu()
    popup.popup(window.pen_color_button.mapToGlobal(window.pen_color_button.rect().bottomLeft()))
    wait_until(popup.isVisible)
    assert all(not button.icon().isNull() for button in popup.findChildren(QToolButton))
    popup.hide()

    assert window.context_text_fill_button.property("color_role") == "fill"
    assert window.context_text_border_button.property("color_role") == "border"
    assert window.context_text_color_button.property("color_role") == "text"
    assert window.context_text_color_button.property("color_renderer") == "letter"
    assert window.text_color_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert not window.text_color_button.icon().isNull()
    fill_swatches = window.text_fill_button.menu().findChildren(QToolButton)
    border_swatches = window.text_border_button.menu().findChildren(QToolButton)
    text_swatches = window.text_color_button.menu().findChildren(QToolButton)
    assert {button.property("swatch_role") for button in fill_swatches} == {"fill"}
    assert {button.property("swatch_role") for button in border_swatches} == {"border"}
    assert {button.property("swatch_role") for button in text_swatches} == {"text"}
    assert {button.property("swatch_renderer") for button in text_swatches} == {"letter"}
    assert all(not button.icon().isNull() for button in text_swatches)
    assert window.context_bold_button.font().bold()
    assert window.context_italic_button.font().italic()
    assert window.context_text_size_spin.width() == 46
    assert window.context_text_size_spin.buttonSymbols().name == "NoButtons"
    assert all(button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
               and button.width() == button.height() == 25
               and not button.icon().isNull()
               for button in (window.context_size_down_button, window.context_size_up_button))
    assert session.document_revision == 0 and not session.annotations


def test_creation_capsule_tracks_its_active_palette_row(polished_window):
    window, path = polished_window
    _session, view = _open(window, path)

    for activate, tool_button in (
        (view.set_pen_enabled, window.palette_pen_button),
        (view.set_text_box_enabled, window.palette_text_box_button),
        (view.set_callout_enabled, window.palette_callout_button),
    ):
        activate(True)
        wait_until(window.creation_properties.isVisible)
        palette = window.annotation_palette
        expected_center = palette.y() + tool_button.y() + tool_button.height() // 2
        actual_center = window.creation_properties.y() + window.creation_properties.height() // 2
        assert abs(actual_center - expected_center) <= 1
        assert window.creation_properties.x() == palette.x() + palette.width() + 2

    window.annotation_palette.move(90, 90)
    window._remember_palette_position(window.annotation_palette.pos())
    palette = window.annotation_palette
    expected_center = palette.y() + window.palette_callout_button.y() + window.palette_callout_button.height() // 2
    actual_center = window.creation_properties.y() + window.creation_properties.height() // 2
    assert abs(actual_center - expected_center) <= 1


def test_context_control_sizes_centering_and_dropdown_hit_targets(polished_window):
    window, path = polished_window
    _session, view = _open(window, path)
    view.set_text_box_enabled(True)
    wait_until(window.creation_properties.isVisible)
    center_y = window.context_bold_button.geometry().center().y()
    for button in (window.context_bold_button, window.context_italic_button):
        assert button.width() == button.height() == window.palette_pen_button.width() == 38
        assert abs(button.geometry().center().y() - center_y) <= 1
    for button in (window.context_text_fill_button, window.context_text_border_button,
                   window.context_text_color_button, window.context_text_font_button):
        assert isinstance(button, CenteredMenuButton)
        content, arrow = button.content_rects()
        assert content.right() < arrow.left()
        assert abs(content.center().y() - arrow.center().y()) <= 1
        assert button.rect().contains(arrow)
        assert abs(button.geometry().center().y() - center_y) <= 1
    for button in (window.context_size_down_button, window.context_size_up_button):
        assert button.width() == button.height() == 25
        assert abs(button.geometry().center().y() - center_y) <= 1
    # Clicking the right-hand chevron opens the original menu.
    button = window.context_text_fill_button
    shown = QSignalSpy(button.menu().aboutToShow)
    QTimer.singleShot(50, button.menu().hide)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=button.content_rects()[1].center())
    assert shown.count() == 1
