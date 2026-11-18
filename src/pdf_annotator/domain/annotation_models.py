"""Serializable frontend annotation model, without inventory behavior."""

from dataclasses import dataclass
from enum import Enum

from .annotation_commands import CommandSource
from .identifiers import AnnotationId, PdfBackingReference
from .kinds import AnnotationKind
from .rich_text import RichText
from .editorial_metadata import EditorialMetadata, validate_metadata
from .styles import FreeTextStyle, HighlightStyle, InkStyle
from .targets import AnnotationTarget, plain_text, target_page


class AnnotationOwnership(str, Enum):
    CREATED_BY_APP = "created_by_app"
    EXTERNAL_LOSSLESSLY_EDITABLE = "external_losslessly_editable"
    EXTERNAL_READ_ONLY = "external_read_only"


@dataclass(frozen=True, slots=True)
class AnnotationModel:
    annotation_id: AnnotationId
    document_id: str
    document_generation: int
    kind: AnnotationKind
    target: AnnotationTarget
    text: str | None
    style: InkStyle | HighlightStyle | FreeTextStyle | None
    ownership: AnnotationOwnership
    source: CommandSource | None
    backing: PdfBackingReference | None = None
    rich_text: RichText | None = None
    metadata: EditorialMetadata | None = None

    def __post_init__(self) -> None:
        validate_metadata(self.metadata, editorial=self.kind in (
            AnnotationKind.DELETE_TEXT, AnnotationKind.INSERT_TEXT, AnnotationKind.REPLACE_TEXT))
        if not isinstance(self.annotation_id, AnnotationId):
            raise ValueError("Invalid annotation identity")
        if not isinstance(self.document_id, str) or not 0 < len(self.document_id) <= 128:
            raise ValueError("Invalid annotation document identity")
        if type(self.document_generation) is not int or self.document_generation < 1:
            raise ValueError("Invalid annotation generation")
        if not isinstance(self.kind, AnnotationKind) or not isinstance(self.ownership, AnnotationOwnership):
            raise ValueError("Invalid annotation classification")
        target_page(self.target)
        if self.text is not None:
            plain_text(self.text, allow_empty=True)
        if self.style is not None and not isinstance(self.style, (InkStyle, HighlightStyle, FreeTextStyle)):
            raise ValueError("Invalid annotation style")
        if self.source is not None and not isinstance(self.source, CommandSource):
            raise ValueError("Invalid annotation source")
        if self.backing is not None and not isinstance(self.backing, PdfBackingReference):
            raise ValueError("Invalid PDF backing reference")
        if self.rich_text is not None:
            if not isinstance(self.rich_text, RichText) or self.text != self.rich_text.plain:
                raise ValueError("Invalid annotation rich text")
        if self.ownership == AnnotationOwnership.CREATED_BY_APP and self.source is None:
            raise ValueError("App annotation requires provenance")

    @property
    def page_index(self) -> int:
        return target_page(self.target)

    @property
    def editable(self) -> bool:
        return self.ownership != AnnotationOwnership.EXTERNAL_READ_ONLY
