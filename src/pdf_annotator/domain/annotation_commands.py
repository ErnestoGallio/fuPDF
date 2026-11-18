"""Discriminated immutable commands shared by every future caller."""

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import ClassVar, TypeAlias

from .identifiers import AnnotationId, new_annotation_id, new_command_id, validate_command_id
from .kinds import CommandKind
from .external_edits import EditExternalAnnotation
from .rich_text import RichText
from .editorial_metadata import EditorialMetadata, validate_metadata
from .styles import FreeTextStyle, HighlightStyle, InkStyle
from .targets import (CalloutTarget, InkTarget, RectTarget, TextInsertionTarget,
                      TextRangeTarget, plain_text, target_page)

COMMAND_SCHEMA_VERSION = 1


class CommandSource(str, Enum):
    UI = "ui"
    AUTOMATION = "automation"
    AI = "ai"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class CommandContext:
    command_id: str
    document_id: str
    document_generation: int
    source: CommandSource
    schema_version: int = COMMAND_SCHEMA_VERSION
    review_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", validate_command_id(self.command_id))
        if not isinstance(self.document_id, str) or not 0 < len(self.document_id) <= 128:
            raise ValueError("Invalid document identity")
        if type(self.document_generation) is not int or self.document_generation < 1:
            raise ValueError("Invalid document generation")
        if not isinstance(self.source, CommandSource):
            raise ValueError("Invalid command source")
        if self.schema_version != COMMAND_SCHEMA_VERSION:
            raise ValueError("Unsupported command schema")
        if self.review_snapshot_id is not None:
            if self.source != CommandSource.AI:
                raise ValueError("Review proof requires the AI source")
            object.__setattr__(self, "review_snapshot_id", validate_command_id(self.review_snapshot_id))

    @classmethod
    def create(cls, document_id: str, document_generation: int,
               source: CommandSource = CommandSource.UI) -> "CommandContext":
        return cls(new_command_id(), document_id, document_generation, source)


@dataclass(frozen=True, slots=True)
class CreateInk:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_INK
    context: CommandContext
    target: InkTarget
    style: InkStyle
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        _creation(self.context, self.target, self.style, self.annotation_id, InkTarget, InkStyle)
        validate_metadata(self.metadata, editorial=False)


@dataclass(frozen=True, slots=True)
class CreateHighlight:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_HIGHLIGHT
    context: CommandContext
    target: TextRangeTarget
    style: HighlightStyle = field(default_factory=HighlightStyle)
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        _creation(self.context, self.target, self.style, self.annotation_id,
                  TextRangeTarget, HighlightStyle)
        validate_metadata(self.metadata, editorial=False)


@dataclass(frozen=True, slots=True)
class CreateFreeText:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_FREE_TEXT
    context: CommandContext
    target: RectTarget
    text: str
    style: FreeTextStyle
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)
    rich_text: RichText | None = None
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        _creation(self.context, self.target, self.style, self.annotation_id,
                  RectTarget, FreeTextStyle)
        validate_metadata(self.metadata, editorial=False)
        plain_text(self.text, allow_empty=True)
        rich_text = self.rich_text or RichText.plain_text(
            self.text, font_family=self.style.font_family, font_size=self.style.font_size,
            color=self.style.text)
        if rich_text.plain != self.text:
            raise ValueError("FreeText rich text must match plain text")
        object.__setattr__(self, "rich_text", rich_text)


@dataclass(frozen=True, slots=True)
class CreateCallout:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_CALLOUT
    context: CommandContext
    target: CalloutTarget
    text: str
    style: FreeTextStyle
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)
    rich_text: RichText | None = None
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        _creation(self.context, self.target, self.style, self.annotation_id,
                  CalloutTarget, FreeTextStyle)
        validate_metadata(self.metadata, editorial=False)
        plain_text(self.text, allow_empty=True)
        rich_text = self.rich_text or RichText.plain_text(
            self.text, font_family=self.style.font_family, font_size=self.style.font_size,
            color=self.style.text)
        if rich_text.plain != self.text:
            raise ValueError("Callout rich text must match plain text")
        object.__setattr__(self, "rich_text", rich_text)


@dataclass(frozen=True, slots=True)
class UpdateCallout:
    kind: ClassVar[CommandKind] = CommandKind.UPDATE_CALLOUT
    context: CommandContext
    annotation_id: AnnotationId
    target: CalloutTarget
    text: str
    style: FreeTextStyle
    rich_text: RichText | None = None

    def __post_init__(self) -> None:
        _freetext_identity(self.context, self.annotation_id, self.target.page_index)
        plain_text(self.text, allow_empty=True)
        if not isinstance(self.style, FreeTextStyle):
            raise ValueError("Invalid callout update style")
        rich = self.rich_text or RichText.plain_text(
            self.text, font_family=self.style.font_family, font_size=self.style.font_size,
            color=self.style.text)
        if rich.plain != self.text:
            raise ValueError("Callout rich text must match plain text")
        object.__setattr__(self, "rich_text", rich)


@dataclass(frozen=True, slots=True)
class MoveCallout:
    kind: ClassVar[CommandKind] = CommandKind.MOVE_CALLOUT
    context: CommandContext
    annotation_id: AnnotationId
    page_index: int
    delta_x: float
    delta_y: float
    def __post_init__(self) -> None:
        _freetext_identity(self.context, self.annotation_id, self.page_index); _delta(self, "Callout move requires a displacement")


@dataclass(frozen=True, slots=True)
class ResizeCallout:
    kind: ClassVar[CommandKind] = CommandKind.RESIZE_CALLOUT
    context: CommandContext
    annotation_id: AnnotationId
    target: CalloutTarget
    def __post_init__(self) -> None:
        if not isinstance(self.context, CommandContext) or not isinstance(self.annotation_id, AnnotationId) or not isinstance(self.target, CalloutTarget):
            raise ValueError("Invalid callout resize command")


@dataclass(frozen=True, slots=True)
class MoveCalloutAnchor:
    kind: ClassVar[CommandKind] = CommandKind.MOVE_CALLOUT_ANCHOR
    context: CommandContext
    annotation_id: AnnotationId
    target: CalloutTarget
    def __post_init__(self) -> None:
        if not isinstance(self.context, CommandContext) or not isinstance(self.annotation_id, AnnotationId) or not isinstance(self.target, CalloutTarget):
            raise ValueError("Invalid callout anchor command")


@dataclass(frozen=True, slots=True)
class TransferCallout:
    kind: ClassVar[CommandKind] = CommandKind.TRANSFER_CALLOUT
    context: CommandContext
    annotation_id: AnnotationId
    source_page_index: int
    target: CalloutTarget
    def __post_init__(self) -> None:
        _freetext_identity(self.context, self.annotation_id, self.source_page_index)
        if self.target.page_index == self.source_page_index:
            raise ValueError("Invalid callout transfer target")


@dataclass(frozen=True, slots=True)
class CreateDeleteText:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_DELETE_TEXT
    context: CommandContext
    target: TextRangeTarget
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        _creation(self.context, self.target, None, self.annotation_id, TextRangeTarget, type(None))
        validate_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class CreateInsertText:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_INSERT_TEXT
    context: CommandContext
    target: TextInsertionTarget
    text: str
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        _creation(self.context, self.target, None, self.annotation_id,
                  TextInsertionTarget, type(None))
        plain_text(self.text)
        validate_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class CreateReplaceText:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_REPLACE_TEXT
    context: CommandContext
    target: TextRangeTarget
    text: str
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        _creation(self.context, self.target, None, self.annotation_id, TextRangeTarget, type(None))
        plain_text(self.text)
        validate_metadata(self.metadata)


@dataclass(frozen=True, slots=True)
class CreateStickyNote:
    kind: ClassVar[CommandKind] = CommandKind.CREATE_STICKY_NOTE
    context: CommandContext
    target: RectTarget
    text: str
    annotation_id: AnnotationId = field(default_factory=new_annotation_id)

    def __post_init__(self) -> None:
        _creation(self.context, self.target, None, self.annotation_id, RectTarget, type(None))
        plain_text(self.text).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MoveInk:
    """Translate one app-created Ink in canonical page coordinates."""

    kind: ClassVar[CommandKind] = CommandKind.MOVE_INK
    context: CommandContext
    annotation_id: AnnotationId
    page_index: int
    delta_x: float
    delta_y: float

    def __post_init__(self) -> None:
        if (not isinstance(self.context, CommandContext)
                or not isinstance(self.annotation_id, AnnotationId)
                or type(self.page_index) is not int or self.page_index < 0
                or type(self.delta_x) not in (int, float)
                or type(self.delta_y) not in (int, float)
                or not isfinite(self.delta_x) or not isfinite(self.delta_y)):
            raise ValueError("Invalid Ink move command")
        object.__setattr__(self, "delta_x", float(self.delta_x))
        object.__setattr__(self, "delta_y", float(self.delta_y))
        if self.delta_x == 0.0 and self.delta_y == 0.0:
            raise ValueError("Ink move requires a displacement")


@dataclass(frozen=True, slots=True)
class UpdateFreeText:
    """Replace app-created FreeText content and style."""

    kind: ClassVar[CommandKind] = CommandKind.UPDATE_FREE_TEXT
    context: CommandContext
    annotation_id: AnnotationId
    page_index: int
    text: str
    style: FreeTextStyle
    rich_text: RichText | None = None

    def __post_init__(self) -> None:
        _freetext_identity(self.context, self.annotation_id, self.page_index)
        plain_text(self.text, allow_empty=True)
        if not isinstance(self.style, FreeTextStyle):
            raise ValueError("Invalid FreeText update style")
        rich_text = self.rich_text or RichText.plain_text(
            self.text, font_family=self.style.font_family, font_size=self.style.font_size,
            color=self.style.text)
        if rich_text.plain != self.text:
            raise ValueError("FreeText rich text must match plain text")
        object.__setattr__(self, "rich_text", rich_text)


@dataclass(frozen=True, slots=True)
class MoveFreeText:
    kind: ClassVar[CommandKind] = CommandKind.MOVE_FREE_TEXT
    context: CommandContext
    annotation_id: AnnotationId
    page_index: int
    delta_x: float
    delta_y: float

    def __post_init__(self) -> None:
        _freetext_identity(self.context, self.annotation_id, self.page_index)
        _delta(self, "FreeText move requires a displacement")


@dataclass(frozen=True, slots=True)
class ResizeFreeText:
    kind: ClassVar[CommandKind] = CommandKind.RESIZE_FREE_TEXT
    context: CommandContext
    annotation_id: AnnotationId
    target: RectTarget

    def __post_init__(self) -> None:
        if not isinstance(self.context, CommandContext) or not isinstance(self.annotation_id, AnnotationId):
            raise ValueError("Invalid FreeText resize command")
        if not isinstance(self.target, RectTarget):
            raise ValueError("Invalid FreeText resize target")


@dataclass(frozen=True, slots=True)
class TransferFreeText:
    """Move one app-created FreeText atomically to another page in this document."""

    kind: ClassVar[CommandKind] = CommandKind.TRANSFER_FREE_TEXT
    context: CommandContext
    annotation_id: AnnotationId
    source_page_index: int
    target: RectTarget

    def __post_init__(self) -> None:
        _freetext_identity(self.context, self.annotation_id, self.source_page_index)
        if (not isinstance(self.target, RectTarget)
                or self.target.page_index == self.source_page_index):
            raise ValueError("Invalid FreeText transfer target")


@dataclass(frozen=True, slots=True)
class DeleteAnnotation:
    """Common future delete path; actual deletion starts with tool adapters."""

    kind: ClassVar[CommandKind] = CommandKind.DELETE_ANNOTATION
    context: CommandContext
    annotation_id: AnnotationId
    page_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.context, CommandContext) or not isinstance(self.annotation_id, AnnotationId):
            raise ValueError("Invalid delete command")
        if type(self.page_index) is not int or self.page_index < 0:
            raise ValueError("Invalid page index")


@dataclass(frozen=True, slots=True)
class DeleteExternalAnnotation:
    """Delete a verified read-only inventory item without reconstructing it."""

    kind: ClassVar[CommandKind] = CommandKind.DELETE_EXTERNAL_ANNOTATION
    context: CommandContext
    annotation_id: AnnotationId
    logical_id: str
    page_index: int

    def __post_init__(self) -> None:
        if (not isinstance(self.context, CommandContext) or not isinstance(self.annotation_id, AnnotationId)
                or not isinstance(self.logical_id, str) or not self.logical_id.startswith("native:")
                or type(self.page_index) is not int or self.page_index < 0):
            raise ValueError("Invalid external delete command")


@dataclass(frozen=True, slots=True)
class Undo:
    """Undo one confirmed document mutation in the owning worker."""

    kind: ClassVar[CommandKind] = CommandKind.UNDO
    context: CommandContext


@dataclass(frozen=True, slots=True)
class Redo:
    """Redo one previously undone document mutation in the owning worker."""

    kind: ClassVar[CommandKind] = CommandKind.REDO
    context: CommandContext


def _creation(context, target, style, annotation_id, target_type, style_type) -> None:
    if (not isinstance(context, CommandContext) or not isinstance(target, target_type)
            or not isinstance(style, style_type) or not isinstance(annotation_id, AnnotationId)):
        raise ValueError("Invalid creation command")


AnnotationCommand: TypeAlias = (CreateInk | CreateHighlight | CreateFreeText | CreateCallout | UpdateCallout | MoveCallout | ResizeCallout | MoveCalloutAnchor | TransferCallout
                                | CreateDeleteText | CreateInsertText | CreateReplaceText | CreateStickyNote
                                | MoveInk | UpdateFreeText | MoveFreeText | ResizeFreeText
                                | TransferFreeText
                                | DeleteAnnotation | DeleteExternalAnnotation | EditExternalAnnotation | Undo | Redo)

COMMAND_TYPES = (CreateInk, CreateHighlight, CreateFreeText, CreateCallout, UpdateCallout, MoveCallout, ResizeCallout, MoveCalloutAnchor, TransferCallout,
                 CreateDeleteText, CreateInsertText, CreateReplaceText, CreateStickyNote, MoveInk,
                 UpdateFreeText, MoveFreeText, ResizeFreeText, TransferFreeText,
                 DeleteAnnotation, DeleteExternalAnnotation, EditExternalAnnotation, Undo, Redo)

AUTHORED_CREATIONS = (CreateInk, CreateHighlight, CreateFreeText, CreateCallout,
                     CreateDeleteText, CreateInsertText, CreateReplaceText)


def command_page(command: AnnotationCommand) -> int:
    if isinstance(command, EditExternalAnnotation):
        return command.page_index
    if isinstance(command, (Undo, Redo)):
        raise ValueError("History commands have no page target")
    if isinstance(command, (DeleteAnnotation, DeleteExternalAnnotation, MoveInk, UpdateFreeText, MoveFreeText, MoveCallout)):
        return command.page_index
    if isinstance(command, (ResizeFreeText, TransferFreeText, UpdateCallout, ResizeCallout,
                            MoveCalloutAnchor, TransferCallout)):
        return target_page(command.target)
    if isinstance(command, (CreateInk, CreateHighlight, CreateFreeText, CreateCallout,
                            CreateDeleteText, CreateInsertText, CreateReplaceText, CreateStickyNote)):
        return target_page(command.target)
    raise ValueError("Invalid annotation command")


def command_annotation_id(command: AnnotationCommand) -> AnnotationId | None:
    if isinstance(command, (Undo, Redo)):
        return None
    if not isinstance(command, COMMAND_TYPES):
        raise ValueError("Invalid annotation command")
    return command.annotation_id


def _freetext_identity(context: CommandContext, annotation_id: AnnotationId, page_index: int) -> None:
    if (not isinstance(context, CommandContext) or not isinstance(annotation_id, AnnotationId)
            or type(page_index) is not int or page_index < 0):
        raise ValueError("Invalid FreeText command")


def _delta(command, error: str) -> None:
    for name in ("delta_x", "delta_y"):
        value = getattr(command, name)
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError(error)
        object.__setattr__(command, name, float(value))
    if command.delta_x == 0.0 and command.delta_y == 0.0:
        raise ValueError(error)
