"""Small, reusable M17A.2 presentation primitives for the Qt chrome."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractButton, QGridLayout, QMenu, QStyle, QStyleOptionToolButton,
                               QStylePainter, QTabBar, QToolButton, QWidget, QWidgetAction)

from ..domain.styles import RgbColor


@dataclass(frozen=True, slots=True)
class UiColors:
    app: str = "#202124"
    chrome: str = "#25272A"
    surface: str = "#303236"
    hover: str = "#3A3D42"
    pressed: str = "#41454B"
    primary: str = "#F1F3F4"
    secondary: str = "#AEB3B8"
    disabled: str = "#6F757B"
    border: str = "#464A50"
    accent: str = "#8AB4F8"
    active: str = "#33445F"


COLORS = UiColors()


class DocumentTabBar(QTabBar):
    """Paint a padded X on Qt's original close buttons, preserving their signals."""

    def tabInserted(self, index: int) -> None:
        super().tabInserted(index)
        for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
            button = self.tabButton(index, side)
            if isinstance(button, QAbstractButton):
                button.setFixedSize(22, 22)
                button.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Paint and isinstance(watched, QAbstractButton):
            painter = QPainter(watched)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if watched.underMouse() or watched.hasFocus():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(COLORS.pressed if watched.isDown() else COLORS.hover))
                painter.drawRoundedRect(watched.rect(), 6, 6)
            painter.setPen(QPen(QColor(COLORS.primary if watched.isEnabled() else COLORS.disabled),
                                1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            center = QRectF(watched.rect()).center()
            painter.drawLine(center + QPointF(-3.5, -3.5), center + QPointF(3.5, 3.5))
            painter.drawLine(center + QPointF(-3.5, 3.5), center + QPointF(3.5, -3.5))
            painter.end()
            return True
        return super().eventFilter(watched, event)


class CenteredMenuButton(QToolButton):
    """Keep the existing InstantPopup behavior with an explicit right chevron."""

    def content_rects(self) -> tuple[QRect, QRect]:
        chevron = QRect(self.width() - 11, (self.height() - 10) // 2, 10, 10)
        content = QRect(2, 0, chevron.left() - 3, self.height())
        return content, chevron

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.features &= ~QStyleOptionToolButton.ToolButtonFeature.HasMenu
        option.icon, option.text = QIcon(), ""
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
        content, chevron = self.content_rects()
        if self.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly:
            extent = min(20, content.width())
            rect = QRect(content.x(), (self.height() - extent) // 2, extent, extent)
            self.icon().paint(painter, rect, Qt.AlignmentFlag.AlignCenter,
                              QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled)
        else:
            painter.setPen(QColor(COLORS.primary if self.isEnabled() else COLORS.disabled))
            painter.drawText(content, Qt.AlignmentFlag.AlignCenter, self.text())
        icon("chevron-down").paint(painter, chevron)


def application_stylesheet() -> str:
    """One shared graphite theme; selectors keep feature widgets behavioral."""
    return f"""
    QMainWindow, QStatusBar {{ background: {COLORS.app}; color: {COLORS.primary}; }}
    QWidget {{ color: {COLORS.primary}; }}
    QMenuBar#documentChromeMenu {{ background: transparent; color: {COLORS.primary};
                                   border: 0; padding: 2px 0; spacing: 2px; }}
    QWidget#chromeBrand {{ background: transparent; border-right: 1px solid {COLORS.border}; }}
    QLabel#chromeBrandName {{ font-size: 16px; background: transparent; }}
    QMenuBar#documentChromeMenu::item {{ background: transparent; border-radius: 7px; padding: 4px 10px; }}
    QMenuBar#documentChromeMenu::item:selected {{ background: {COLORS.hover}; }}
    QMenuBar#documentChromeMenu::item:pressed {{ background: {COLORS.pressed}; }}
    QToolBar {{ background: {COLORS.chrome}; border: 0; spacing: 4px; padding: 4px 6px; }}
    QToolBar::separator {{ background: {COLORS.border}; width: 1px; margin: 6px; }}
    QTabWidget#documentTabs {{ background: {COLORS.chrome}; }}
    QTabWidget::pane {{ border: 0; border-top: 1px solid {COLORS.border};
                        top: 1px; background: {COLORS.chrome}; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{ background: transparent; color: {COLORS.secondary}; border: 1px solid transparent;
                    border-radius: 8px;
                    padding: 2px 12px 2px 9px; margin: 1px 2px; }}
    QTabBar::tab:selected {{ color: {COLORS.primary};
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #414750, stop:1 #303740);
        border-color: #6686AD; }}
    QTabBar::tab:hover:!selected {{ background: {COLORS.hover}; }}
    QTabBar::close-button {{ subcontrol-position: right; width: 22px; height: 22px; }}
    QToolButton, QPushButton {{ background: transparent; color: {COLORS.primary}; border: 0;
                                border-radius: 8px; padding: 4px 7px; }}
    QToolButton:hover, QPushButton:hover {{ background: {COLORS.hover}; }}
    QToolButton:pressed, QPushButton:pressed {{ background: {COLORS.pressed}; }}
    QToolButton:checked {{ background: {COLORS.active}; color: {COLORS.accent}; }}
    QToolButton:disabled, QPushButton:disabled {{ color: {COLORS.disabled}; }}
    QToolButton#windowCaptionButton, QToolButton#windowCloseButton {{ padding: 0;
        border: 0; border-radius: 5px; }}
    QToolButton#windowCloseButton:hover {{ background: #C42B1C; }}
    QToolButton#windowCloseButton:pressed {{ background: #A52519; }}
    QToolButton#paletteToolButton {{ border: 1px solid transparent; border-radius: 12px; padding: 0; }}
    QToolButton#paletteToolButton:hover {{ background: rgba(255, 255, 255, 18); }}
    QToolButton#paletteToolButton:checked {{ border-color: #6686AD;
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #43546B, stop:1 #303F54); }}
    QToolButton#paletteToolButton:checked:hover {{ border-color: {COLORS.accent}; }}
    QToolButton#paletteToolButton:pressed {{ background: {COLORS.pressed}; }}
    QToolButton#contextIconButton {{ min-width: 30px; min-height: 30px;
                                                                 max-width: 34px; max-height: 34px;
                                                                 border: 1px solid transparent;
                                                                 border-radius: 10px; padding: 0; }}
    QToolButton#formatToggle {{ min-width: 36px; min-height: 36px; max-width: 36px; max-height: 36px;
        border: 1px solid transparent; border-radius: 12px; padding: 0; font-size: 16px; }}
    QToolButton#formatToggle:checked {{ background: {COLORS.active}; color: {COLORS.primary};
                                       border-color: #6686AD; }}
    QToolButton#paletteToolButton:focus, QToolButton#contextIconButton:focus,
    QToolButton#formatToggle:focus {{ border-color: {COLORS.accent}; }}
    QToolButton#paletteToolButton:disabled, QToolButton#contextIconButton:disabled,
    QToolButton#formatToggle:disabled {{ background: transparent; border-color: transparent; }}
    QToolButton#colorControl {{ min-width: 30px; min-height: 30px; max-width: 34px;
                                 max-height: 34px; border-radius: 8px; padding: 2px; }}
    QFrame#floatingAnnotationPalette, QFrame#contextualCreationProperties,
    QFrame#pdfTextSelectionBar {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(64, 68, 74, 238), stop:1 rgba(40, 43, 48, 235)); border: 1px solid #555A62; }}
    QFrame#floatingAnnotationPalette {{ border-radius: 21px; }}
    QFrame#contextualCreationProperties, QFrame#pdfTextSelectionBar {{ border-radius: 21px; }}
    QFrame#paletteDragHandle {{ background: {COLORS.secondary}; border: 0; border-radius: 2px;
                                               min-width: 20px; max-width: 20px; min-height: 3px; max-height: 3px; }}
    QMenu {{ background: {COLORS.surface}; color: {COLORS.primary}; border: 1px solid {COLORS.border};
             border-radius: 10px; padding: 4px; }}
    QMenu::item {{ padding: 6px 22px 6px 10px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {COLORS.hover}; }}
    QLineEdit, QPlainTextEdit, QSpinBox {{ background: {COLORS.surface}; color: {COLORS.primary};
                                            border: 1px solid {COLORS.border}; border-radius: 8px; padding: 4px 7px; }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus {{ border-color: {COLORS.accent}; }}
    QToolButton#contextFontFamily {{ background: rgba(255, 255, 255, 6);
        border: 1px solid {COLORS.border}; border-radius: 9px; padding: 5px 9px; }}
    QToolButton#contextFontFamily:hover {{ background: {COLORS.hover}; }}
    CenteredMenuButton::menu-indicator {{ image: none; width: 0; height: 0; }}
    QSpinBox#contextFontSize {{ min-width: 36px; max-width: 36px; padding: 3px 4px; }}
    QToolButton#contextStepButton {{ min-width: 23px; min-height: 23px; max-width: 23px; max-height: 23px;
                                     border-radius: 10px; padding: 0; background: rgba(255, 255, 255, 10);
                                     border: 1px solid {COLORS.border}; }}
    QToolButton#contextStepButton:hover {{ background: {COLORS.pressed}; border-color: #555A61; }}
    QToolButton#contextStepButton:pressed {{ background: {COLORS.chrome}; border-color: {COLORS.border}; }}
    QWidget#viewZoomRow {{ background: transparent; }}
    QToolButton#viewZoomButton {{ min-width: 24px; min-height: 24px; max-width: 24px; max-height: 24px;
                                  border-radius: 12px; padding: 0; background: {COLORS.hover};
                                  border: 1px solid {COLORS.border}; }}
    QToolButton#viewZoomButton:hover {{ background: {COLORS.pressed}; }}
    QToolButton#viewZoomButton:pressed {{ background: {COLORS.chrome}; }}
    QLabel#viewZoomLabel {{ min-width: 46px; color: {COLORS.primary}; }}
    QDockWidget {{ background: {COLORS.chrome}; color: {COLORS.primary}; }}
    QDockWidget::title {{ background: {COLORS.chrome}; padding: 7px 9px; border-bottom: 1px solid {COLORS.border}; }}
    QListWidget {{ background: {COLORS.chrome}; border: 0; }}
    QDockWidget#annotation_sidebar {{ background: transparent; }}
    QDockWidget#annotation_sidebar::title {{ background: transparent; }}
    QListWidget#annotation_list, QTextEdit#annotation_detail {{ background: transparent; border: 0; }}
    QLabel#annotation_author {{ color: {COLORS.secondary}; }}
    QWidget#annotation_card QPushButton {{ text-align: left; }}
    QListWidget::item {{ padding: 7px 8px; border-radius: 6px; }}
    QListWidget#annotation_list::item {{ padding: 0; }}
    QListWidget::item:hover {{ background: {COLORS.hover}; }}
    QListWidget::item:selected {{ background: {COLORS.active}; color: {COLORS.primary}; }}
    QSplitter::handle {{ background: {COLORS.border}; }}
    """


def rgb_hex(color: RgbColor | None) -> str:
    if color is None:
        return COLORS.surface
    return "#%02X%02X%02X" % (round(color.red * 255), round(color.green * 255),
                               round(color.blue * 255))


def icon(name: str, *, color: str | None = None, size: int = 20) -> QIcon:
    """Rasterize the same line drawing at common Windows display densities."""
    result = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0, 3.0):
        for mode, foreground in ((QIcon.Mode.Normal, color or COLORS.primary),
                                 (QIcon.Mode.Disabled, COLORS.disabled)):
            result.addPixmap(_line_icon_pixmap(name, foreground, size, dpr), mode)
    return result


def _line_icon_pixmap(name: str, color: str, size: int, dpr: float) -> QPixmap:
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name == "minimize":
        painter.drawLine(6, 12, 18, 12)
    elif name == "maximize":
        painter.drawRect(6, 6, 12, 12)
    elif name == "restore":
        painter.drawRect(5, 9, 10, 10)
        outline = QPainterPath()
        outline.moveTo(9, 7)
        outline.lineTo(9, 5)
        outline.lineTo(19, 5)
        outline.lineTo(19, 15)
        outline.lineTo(17, 15)
        painter.drawPath(outline)
    elif name == "close":
        painter.drawLine(7, 7, 17, 17)
        painter.drawLine(7, 17, 17, 7)
    elif name == "pen":
        # An open, fluid stroke: no enclosed loop that can read as an eye.
        stroke = QPainterPath()
        stroke.moveTo(4, 18)
        stroke.cubicTo(7, 15, 11, 9, 15, 5)
        stroke.moveTo(9, 12)
        stroke.cubicTo(12, 9, 15, 10, 14, 14)
        stroke.cubicTo(13, 19, 18, 17, 21, 13)
        painter.drawPath(stroke)
    elif name == "textbox":
        painter.drawRoundedRect(4, 4, 16, 16, 3, 3)
        painter.drawLine(8, 8, 16, 8)
        painter.drawLine(12, 8, 12, 16)
        painter.drawLine(10, 16, 14, 16)
    elif name == "callout":
        outline = QPainterPath()
        outline.moveTo(14, 16)
        outline.lineTo(6, 16)
        outline.quadTo(3, 16, 3, 13)
        outline.lineTo(3, 7)
        outline.quadTo(3, 4, 6, 4)
        outline.lineTo(16, 4)
        outline.quadTo(19, 4, 19, 7)
        outline.lineTo(19, 11)
        outline.quadTo(19, 14, 16, 14)
        outline.lineTo(21, 21)
        painter.drawPath(outline)
        painter.drawLine(21, 21, 17, 20)
        painter.drawLine(21, 21, 21, 17)
    elif name in {"highlight", "delete", "replace"}:
        # Related text-operation marks share the same monochrome A geometry.
        painter.drawLine(6, 17, 11, 5)
        painter.drawLine(11, 5, 16, 17)
        painter.drawLine(8, 12, 14, 12)
        if name == "highlight":
            painter.drawLine(5, 21, 18, 21)
            painter.drawLine(5, 19, 5, 21)
            painter.drawLine(18, 19, 18, 21)
        else:
            painter.drawLine(4, 14, 18, 10)
            if name == "replace":
                painter.drawLine(13, 21, 21, 21)
                painter.drawLine(18, 18, 21, 21)
                painter.drawLine(18, 23, 21, 21)
    elif name == "layout":
        for x, y in ((4, 4), (14, 4), (4, 14), (14, 14)):
            painter.drawRoundedRect(x, y, 6, 6, 1, 1)
    elif name == "chevron-up":
        painter.drawLine(7, 14, 12, 9)
        painter.drawLine(12, 9, 17, 14)
    elif name == "chevron-down":
        painter.drawLine(7, 10, 12, 15)
        painter.drawLine(12, 15, 17, 10)
    else:
        painter.drawEllipse(6, 6, 12, 12)
    painter.end()
    return pixmap


def fill_swatch_icon(color: RgbColor | None, *, size: int = 20) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(COLORS.primary), 1.5))
    painter.setBrush(QColor(rgb_hex(color)) if color is not None else Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(3, 3, size - 6, size - 6, 3, 3)
    if color is None:
        painter.setPen(QPen(QColor(COLORS.secondary), 1.4))
        painter.drawLine(4, size - 4, size - 4, 4)
    painter.end()
    return QIcon(pixmap)


def border_swatch_icon(color: RgbColor | None, *, size: int = 20) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(rgb_hex(color)) if color is not None else QColor(COLORS.secondary), 2.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(3, 3, size - 6, size - 6, 3, 3)
    if color is None:
        painter.drawLine(4, size - 4, size - 4, 4)
    painter.end()
    return QIcon(pixmap)


def stroke_swatch_icon(color: RgbColor, *, size: int = 20) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(rgb_hex(color)), 2.4, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    stroke = QPainterPath()
    stroke.moveTo(3, size - 6)
    stroke.cubicTo(7, size - 12, 12, size - 2, size - 3, 5)
    painter.drawPath(stroke)
    painter.end()
    return QIcon(pixmap)


def letter_icon(letter: str, color: RgbColor | str, *, bold: bool = False,
                italic: bool = False, size: int = 20) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    font = QFont("Segoe UI", max(9, size - 5))
    font.setBold(bold)
    font.setItalic(italic)
    painter.setFont(font)
    glyph_color = QColor(rgb_hex(color) if isinstance(color, RgbColor) else color)
    # A near-black letter must remain its real selected color, but needs the
    # faintest keyline against a graphite popup to remain discoverable.
    if glyph_color.lightness() < 70:
        painter.setPen(QPen(QColor(COLORS.secondary), 1.0))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(pixmap.rect().translated(dx, dy), Qt.AlignmentFlag.AlignCenter, letter)
    painter.setPen(glyph_color)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return QIcon(pixmap)


def configure_swatch_menu(menu: QMenu, entries: tuple[tuple[str, RgbColor | None, QAction], ...],
                          *, role: str = "fill") -> None:
    """Present existing QAction choices as one reusable compact swatch grid."""
    host = QWidget(menu)
    layout = QGridLayout(host)
    layout.setContentsMargins(5, 5, 5, 5)
    layout.setSpacing(4)
    icon_for_role = {"stroke": stroke_swatch_icon, "fill": fill_swatch_icon,
                     "border": border_swatch_icon,
                     "text": lambda color: letter_icon("A", color, bold=True)}[role]
    for index, (name, color, action) in enumerate(entries):
        swatch = icon_for_role(color)
        # QToolButton mirrors its default QAction. Keeping the swatch on the
        # QAction avoids a later action-state update restoring an empty icon.
        action.setIcon(swatch)
        button = QToolButton(host)
        button.setObjectName("colorControl")
        button.setProperty("swatch_role", role)
        button.setProperty("swatch_renderer", "letter" if role == "text" else role)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setFixedSize(QSize(28, 28))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(name.replace("-", " ").capitalize())
        button.setDefaultAction(action)
        layout.addWidget(button, index // 4, index % 4)
    container = QWidgetAction(menu)
    container.setDefaultWidget(host)
    menu.addAction(container)
