"""Source-independent edit intents for an observed native annotation profile."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import ClassVar

from .annotation_inventory import external_command_annotation_id
from .geometry import PdfPoint, PdfRect
from .identifiers import AnnotationId
from .kinds import CommandKind
from .rich_text import RichText
from .styles import FreeTextStyle, RgbColor


class ExternalEditKind(str, Enum):
    MOVE = "move"
    RESIZE = "resize"
    ANCHOR = "anchor"
    TEXT = "text"
    STYLE = "style"
    COLOR = "color"


CAPABILITY_FOR_EDIT = {
    ExternalEditKind.MOVE: "movable", ExternalEditKind.RESIZE: "resizable",
    ExternalEditKind.ANCHOR: "movable_anchor", ExternalEditKind.TEXT: "editable_text",
    ExternalEditKind.STYLE: "editable_style", ExternalEditKind.COLOR: "editable_style",
}


@dataclass(frozen=True, slots=True)
class EditExternalAnnotation:
    kind: ClassVar[CommandKind] = CommandKind.EDIT_EXTERNAL_ANNOTATION
    context: CommandContext
    annotation_id: AnnotationId
    logical_id: str
    page_index: int
    edit: ExternalEditKind
    delta: tuple[float, float] | None = None
    rect: PdfRect | None = None
    anchor: PdfPoint | None = None
    rich_text: RichText | None = None
    style: FreeTextStyle | None = None
    color: RgbColor | None = None

    def __post_init__(self) -> None:
        from .annotation_commands import CommandContext
        if (not isinstance(self.context, CommandContext)
                or type(self.page_index) is not int or self.page_index < 0
                or not isinstance(self.logical_id, str)
                or not isinstance(self.edit, ExternalEditKind)):
            raise ValueError("Invalid external edit")
        parts = self.logical_id.split(":")
        if (len(parts) != 3 or parts[0] != "native" or parts[1] != str(self.page_index)
                or not parts[2].isdigit() or int(parts[2]) < 1
                or self.annotation_id != external_command_annotation_id(self.logical_id)):
            raise ValueError("Invalid external identity")
        fields = (self.delta, self.rect, self.anchor, self.rich_text, self.style, self.color)
        selected = list(ExternalEditKind).index(self.edit)
        if any((value is not None) != (index == selected) for index, value in enumerate(fields)
               if not (self.edit == ExternalEditKind.TEXT and index == 4)):
            raise ValueError("External edit must change one property")
        if self.delta is not None and (not isinstance(self.delta, tuple) or len(self.delta) != 2
                or any(type(v) not in (int, float) or not isfinite(v) for v in self.delta)
                or self.delta == (0, 0)):
            raise ValueError("Invalid external translation")
        for value, expected in ((self.rect, PdfRect), (self.anchor, PdfPoint),
                                (self.rich_text, RichText), (self.style, FreeTextStyle), (self.color, RgbColor)):
            if value is not None and not isinstance(value, expected):
                raise ValueError("Invalid external edit value")
