"""Small style families for the seven v1 command semantics."""

from dataclasses import dataclass
from math import isfinite


def _unit(value: float, name: str) -> float:
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"Invalid {name}")
    return float(value)


@dataclass(frozen=True, slots=True)
class RgbColor:
    red: float
    green: float
    blue: float

    def __post_init__(self) -> None:
        for name in ("red", "green", "blue"):
            object.__setattr__(self, name, _unit(getattr(self, name), "RGB color"))


BLACK = RgbColor(0, 0, 0)
YELLOW = RgbColor(1, .92, 0)

HIGHLIGHT_COLOR_PRESETS = (
    ("Giallo", YELLOW),
    ("Verde", RgbColor(.35, 1, .35)),
    ("Azzurro", RgbColor(.25, .80, 1)),
    ("Rosa", RgbColor(1, .45, .70)),
    ("Viola", RgbColor(.70, .45, 1)),
)

PEN_COLOR_PRESETS = (
    ("black", RgbColor(0, 0, 0)),
    ("red", RgbColor(.86, .12, .12)),
    ("orange", RgbColor(.95, .42, .08)),
    ("gold", RgbColor(.90, .68, .04)),
    ("green", RgbColor(.12, .58, .22)),
    ("blue", RgbColor(.08, .32, .86)),
    ("purple", RgbColor(.55, .20, .72)),
    ("gray", RgbColor(.42, .42, .42)),
)

# Deliberately limited to PDF Base 14 families.  These are semantic style
# values, not host-font names: the worker maps them to the corresponding PDF
# resource names and never scans or embeds arbitrary local fonts.
# Keep Times readable for existing M8 PDFs. M8.2 deliberately removes it from
# the creation/editor control, rather than making an already-saved app box
# invalid or lossy on its next edit.
FREE_TEXT_FONT_FAMILIES = ("Helvetica", "Times", "Courier")
TEXT_BOX_UI_FONT_FAMILIES = ("Helvetica", "Courier")


@dataclass(frozen=True, slots=True)
class InkStyle:
    color: RgbColor = BLACK
    width: float = 1.5
    opacity: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.color, RgbColor):
            raise ValueError("Invalid ink color")
        if type(self.width) not in (int, float) or not isfinite(self.width) or not 0 < self.width <= 20:
            raise ValueError("Invalid ink width")
        object.__setattr__(self, "width", float(self.width))
        object.__setattr__(self, "opacity", _unit(self.opacity, "opacity"))


@dataclass(frozen=True, slots=True)
class HighlightStyle:
    color: RgbColor = YELLOW
    opacity: float = .35

    def __post_init__(self) -> None:
        if not isinstance(self.color, RgbColor):
            raise ValueError("Invalid highlight color")
        object.__setattr__(self, "opacity", _unit(self.opacity, "opacity"))


@dataclass(frozen=True, slots=True)
class FreeTextStyle:
    border: RgbColor = BLACK
    fill: RgbColor | None = None
    text: RgbColor = BLACK
    opacity: float = 1.0
    border_width: float = 1.0
    font_size: float = 12.0
    font_family: str = "Helvetica"

    def __post_init__(self) -> None:
        if not isinstance(self.border, RgbColor) or not isinstance(self.text, RgbColor):
            raise ValueError("Invalid FreeText color")
        if self.fill is not None and not isinstance(self.fill, RgbColor):
            raise ValueError("Invalid FreeText fill")
        object.__setattr__(self, "opacity", _unit(self.opacity, "opacity"))
        if not isinstance(self.font_family, str) or self.font_family not in FREE_TEXT_FONT_FAMILIES:
            raise ValueError("Invalid FreeText font family")
        for field_name in ("border_width", "font_size"):
            value = getattr(self, field_name)
            minimum = 6 if field_name == "font_size" else 0
            if (type(value) not in (int, float) or not isfinite(value) or value > 72
                    or (value < minimum if field_name == "font_size" else value <= minimum)):
                raise ValueError(f"Invalid FreeText {field_name}")
            object.__setattr__(self, field_name, float(value))


TEXT_BOX_BORDER_PRESETS = PEN_COLOR_PRESETS
TEXT_BOX_FILL_PRESETS = (
    ("transparent", None),
    ("white", RgbColor(1, 1, 1)),
    ("pale-yellow", RgbColor(1, .96, .70)),
    ("pale-pink", RgbColor(1, .82, .82)),
    ("pale-green", RgbColor(.80, .95, .82)),
    ("pale-blue", RgbColor(.79, .89, 1)),
)
