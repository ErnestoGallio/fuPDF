"""Per-document state only. Future viewer/editor values have no behavior in M1."""

from dataclasses import dataclass, field
from enum import Enum

from ..domain.annotation_models import AnnotationModel
from ..domain.annotation_inventory import AnnotationDetail, AnnotationSummary
from ..domain.search import SearchMatch
from ..util.paths import FileIdentity
from ..viewer.coordinate_mapper import PageGeometry
from ..worker.client import WorkerClient
from ..worker.protocol import ErrorInfo, Operation


class Lifecycle(str, Enum):
    OPENING = "opening"
    READY = "ready"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class ReadingAnchor:
    page_index: int = 0
    offset_pt: float = 0.0


@dataclass
class SearchState:
    query: str = ""
    current_result: int | None = None
    results: list[SearchMatch] = field(default_factory=list)
    generation: int = 0
    next_page: int = 0
    normalized_offset: int = 0
    complete: bool = True


@dataclass
class DocumentUIState:
    reading_anchor: ReadingAnchor = field(default_factory=ReadingAnchor)
    zoom: float = 1.0
    layout: str = "single"
    search: SearchState = field(default_factory=SearchState)
    sidebar_expanded: bool = True
    pen_enabled: bool = False
    pen_color: str = "red"
    text_box_enabled: bool = False
    callout_enabled: bool = False
    delete_text_enabled: bool = False
    insert_text_enabled: bool = False
    replace_text_enabled: bool = False
    text_box_fill: str = "pale-yellow"
    text_box_border: str = "red"
    text_box_text_color: str = "black"
    text_box_font_size: float = 10.0
    text_box_font_family: str = "Helvetica"
    text_box_bold: bool = False
    text_box_italic: bool = False


@dataclass
class DocumentSession:
    document_id: str
    identity: FileIdentity
    worker: WorkerClient | None
    document_generation: int = 1
    lifecycle: Lifecycle = Lifecycle.OPENING
    page_count: int = 0
    pages: list[PageGeometry] = field(default_factory=list)
    format: str = ""
    ui: DocumentUIState = field(default_factory=DocumentUIState)
    document_revision: int = 0
    saved_revision: int = 0
    history_position: int = 0
    history_length: int = 0
    history_state_id: int = 0
    saved_history_state_id: int = 0
    pending_mutations: int = 0
    saving: bool = False
    save_request_id: int | None = None
    last_save_timing_ms: dict[str, float] = field(default_factory=dict)
    annotations: dict[str, AnnotationModel] = field(default_factory=dict)
    annotation_inventory: dict[str, AnnotationSummary] = field(default_factory=dict)
    annotation_details: dict[str, AnnotationDetail] = field(default_factory=dict)
    inventory_cursor: int = 0
    inventory_complete: bool = False
    inventory_request_id: int | None = None
    inventory_operation: Operation | None = None
    inventory_reload_pending: bool = False
    inventory_detail_id: str | None = None
    inventory_detail_offset: int = 0
    inventory_detail_buffer: str = ""
    inventory_detail_pending_id: str | None = None
    inventory_detail_subject: str | None = None
    inventory_detail_retries: int = 0
    annotation_detail_errors: set[str] = field(default_factory=set)
    inventory_changed_during_request: set[str] = field(default_factory=set)
    selected_annotation_id: str | None = None
    external_delete_ids: dict[str, str] = field(default_factory=dict)
    error: ErrorInfo | None = None
    expected_request_id: int | None = None
    expected_operation: Operation | None = None

    @property
    def canonical_path(self) -> str:
        return self.identity.canonical_path

    @property
    def dirty(self) -> bool:
        # Revisions sequence native writes (including undo/redo); semantic
        # history state is the save checkpoint identity.  This lets undo back
        # to the opened/saved document become clean without weakening the
        # monotonic revision contract required by a future save barrier.
        return (self.pending_mutations > 0 or
                (self.history_state_id != self.saved_history_state_id
                 and self.document_revision != self.saved_revision))

    @property
    def can_undo(self) -> bool:
        return not self.saving and self.pending_mutations == 0 and self.history_position > 0

    @property
    def can_redo(self) -> bool:
        return not self.saving and self.pending_mutations == 0 and self.history_position < self.history_length

    # Compatibility name used by the completed lifecycle milestones. New
    # mutation code uses the normative document_revision terminology.
    @property
    def confirmed_revision(self) -> int:
        return self.document_revision

    @confirmed_revision.setter
    def confirmed_revision(self, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("Invalid document revision")
        self.document_revision = value

    @property
    def can_close(self) -> bool:
        # Future Save/Discard/Cancel must resolve this guard; M1 never fakes saving.
        return not self.saving and not self.dirty
