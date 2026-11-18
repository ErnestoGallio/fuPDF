"""Small, serializable rich-text value objects for app-owned FreeText boxes."""

from dataclasses import dataclass

from .styles import FREE_TEXT_FONT_FAMILIES, RgbColor
from .targets import plain_text


@dataclass(frozen=True, slots=True)
class RichTextRun:
    """One contiguous text span with only the M8.2-supported formatting."""

    text: str
    font_family: str
    font_size: float
    color: RgbColor
    bold: bool = False
    italic: bool = False

    def __post_init__(self) -> None:
        # FreeText/Callout geometry is meaningful even with no user content.
        # An empty run retains the fixed style needed to serialize their native
        # rich-text appearance without inventing a placeholder character.
        plain_text(self.text, allow_empty=True)
        if self.font_family not in FREE_TEXT_FONT_FAMILIES:
            raise ValueError("Invalid rich-text font family")
        if type(self.font_size) not in (int, float) or not 6 <= float(self.font_size) <= 72:
            raise ValueError("Invalid rich-text font size")
        if not isinstance(self.color, RgbColor):
            raise ValueError("Invalid rich-text color")
        if type(self.bold) is not bool or type(self.italic) is not bool:
            raise ValueError("Invalid rich-text emphasis")
        object.__setattr__(self, "font_size", float(self.font_size))


@dataclass(frozen=True, slots=True)
class RichText:
    """Canonical runs; ``plain`` is the deterministic PDF ``/Contents`` value."""

    runs: tuple[RichTextRun, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.runs, tuple) or not self.runs:
            raise ValueError("Rich text requires at least one run")
        if any(not isinstance(run, RichTextRun) for run in self.runs):
            raise ValueError("Invalid rich-text run")
        plain_text(self.plain, allow_empty=True)

    @property
    def plain(self) -> str:
        return "".join(run.text for run in self.runs)

    @classmethod
    def plain_text(cls, text: str, *, font_family: str, font_size: float,
                   color: RgbColor, bold: bool = False, italic: bool = False) -> "RichText":
        return cls((RichTextRun(text, font_family, font_size, color, bold, italic),))


@dataclass(frozen=True, slots=True)
class RichTextFormatDelta:
    """The explicitly requested subset of rich-text attributes to change."""

    font_family: str | None = None
    font_size: float | None = None
    color: RgbColor | None = None
    bold: bool | None = None
    italic: bool | None = None

    def __post_init__(self) -> None:
        if all(value is None for value in (self.font_family, self.font_size, self.color,
                                            self.bold, self.italic)):
            raise ValueError("Rich-text format delta is empty")
        if self.font_family is not None and self.font_family not in FREE_TEXT_FONT_FAMILIES:
            raise ValueError("Invalid rich-text font family")
        if self.font_size is not None and (type(self.font_size) not in (int, float)
                                           or not 6 <= float(self.font_size) <= 72):
            raise ValueError("Invalid rich-text font size")
        if self.color is not None and not isinstance(self.color, RgbColor):
            raise ValueError("Invalid rich-text color")
        if self.bold is not None and type(self.bold) is not bool:
            raise ValueError("Invalid rich-text bold")
        if self.italic is not None and type(self.italic) is not bool:
            raise ValueError("Invalid rich-text italic")
        if self.font_size is not None:
            object.__setattr__(self, "font_size", float(self.font_size))


def apply_rich_text_format_delta(rich_text: RichText,
                                 delta: RichTextFormatDelta) -> RichText:
    """Apply only ``delta`` fields to every run, preserving all other fields."""
    return RichText(tuple(RichTextRun(
        run.text,
        delta.font_family if delta.font_family is not None else run.font_family,
        delta.font_size if delta.font_size is not None else run.font_size,
        delta.color if delta.color is not None else run.color,
        delta.bold if delta.bold is not None else run.bold,
        delta.italic if delta.italic is not None else run.italic,
    ) for run in rich_text.runs))
