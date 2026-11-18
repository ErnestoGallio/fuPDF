"""Read-only, serializable annotation inventory shared by viewer and sidebar.

This deliberately is not an ``AnnotationModel``: it describes arbitrary native
PDF annotations, including ones which the application must never rewrite.
"""

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID
from collections.abc import Mapping

from .geometry import PdfPoint, PdfQuad, PdfRect
from .identifiers import AnnotationId
from .rich_text import RichText
from .styles import FreeTextStyle


def external_command_annotation_id(logical_id: str) -> AnnotationId:
    """Stable v4-shaped command identity for an immutable native logical id."""
    digest = bytearray(sha256(logical_id.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return AnnotationId(UUID(bytes=bytes(digest)).hex)


@dataclass(frozen=True, slots=True)
class AnnotationCapabilities:
    selectable: bool = True
    deletable: bool = False
    movable: bool = False
    resizable: bool = False
    editable_text: bool = False
    editable_style: bool = False
    logical_group: bool = False
    movable_anchor: bool = False

    @property
    def read_only(self) -> bool:
        return not any((self.deletable, self.movable, self.resizable,
                        self.editable_text, self.editable_style, self.movable_anchor))


@dataclass(frozen=True, slots=True)
class AnnotationGeometry:
    bbox: PdfRect
    quads: tuple[PdfQuad, ...] = ()
    paths: tuple[tuple[PdfPoint, ...], ...] = ()
    callout_line: tuple[PdfPoint, ...] = ()
    caret: tuple[PdfPoint, PdfPoint] | None = None
    text_rect: PdfRect | None = None


@dataclass(frozen=True, slots=True)
class AnnotationReply:
    """Original PDF relationship plus its read-only logical resolution."""

    irt_xref: int | None
    irt_value: str | None
    reply_type: str | None
    parent_logical_id: str | None
    status: str


@dataclass(frozen=True, slots=True)
class AnnotationSummary:
    logical_id: str
    page_index: int
    kind: str
    subtype: str
    geometry: AnnotationGeometry
    preview: str
    author: str | None
    modified: str | None
    created: str | None
    style: dict[str, object]
    capabilities: AnnotationCapabilities
    component_xrefs: tuple[int, ...]
    appearance_available: bool
    provenance: str = "external"
    native_name: str | None = None
    reply: AnnotationReply | None = None
    reply_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.logical_id, str) or not self.logical_id
                or type(self.page_index) is not int or self.page_index < 0
                or not isinstance(self.kind, str) or not isinstance(self.subtype, str)
                or not isinstance(self.preview, str)):
            raise ValueError("Invalid annotation summary")


def is_attached_reply(summary: AnnotationSummary,
                      inventory: Mapping[str, AnnotationSummary]) -> bool:
    # A survivor must become accessible immediately if its parent disappears,
    # including during a delta/reload transition before the next native scan.
    return (summary.reply is not None and summary.reply.status == "linked"
            and summary.reply.parent_logical_id in inventory)


@dataclass(frozen=True, slots=True)
class AnnotationDetail:
    logical_id: str
    contents: str
    rich_contents: str | None = None
    subject: str | None = None
    edit_rich_text: RichText | None = None
    edit_style: FreeTextStyle | None = None
