"""Typed mutation outcomes; Python exceptions never cross the wire."""

from dataclasses import dataclass
from enum import Enum

from .errors import MutationError
from .identifiers import AnnotationId, validate_command_id
from .annotation_models import AnnotationModel
from .annotation_inventory import AnnotationSummary


class MutationOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class HistoryDelta:
    """The exact logical state change made by one history transition.

    App-created objects carry their rich editable model. Native external
    objects carry only their already-safe M12 summary. A missing delta is an
    explicit signal that the GUI must use its defensive inventory fallback.
    """
    added: tuple[AnnotationModel, ...] = ()
    removed_ids: tuple[str, ...] = ()
    updated: tuple[AnnotationModel, ...] = ()
    inventory_added: tuple[AnnotationSummary, ...] = ()
    inventory_removed_ids: tuple[str, ...] = ()
    inventory_updated: tuple[AnnotationSummary, ...] = ()
    appearance_invalidated_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (any(not isinstance(item, AnnotationModel) for item in self.added + self.updated)
                or any(not isinstance(item, AnnotationSummary)
                       for item in self.inventory_added + self.inventory_updated)
                or any(not isinstance(item, str) or not item
                       for item in self.removed_ids + self.inventory_removed_ids
                       + self.appearance_invalidated_ids)):
            raise ValueError("Invalid history delta")

    @property
    def changed_ids(self) -> tuple[str, ...]:
        values = ((item.annotation_id.value for item in self.added), self.removed_ids,
                  (item.annotation_id.value for item in self.updated),
                  (item.logical_id for item in self.inventory_added),
                  self.inventory_removed_ids,
                  (item.logical_id for item in self.inventory_updated))
        return tuple(dict.fromkeys(value for group in values for value in group))


@dataclass(frozen=True, slots=True)
class MutationResult:
    command_id: str
    document_id: str
    document_generation: int
    outcome: MutationOutcome
    revision_before: int
    document_revision: int
    annotation_id: AnnotationId | None = None
    annotation: AnnotationModel | None = None
    error: MutationError | None = None
    history_position: int = 0
    history_length: int = 0
    history_state_id: int = 0
    refresh_inventory: bool = False
    history_delta: HistoryDelta | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", validate_command_id(self.command_id))
        if not isinstance(self.document_id, str) or not 0 < len(self.document_id) <= 128:
            raise ValueError("Invalid result document identity")
        if type(self.document_generation) is not int or self.document_generation < 1:
            raise ValueError("Invalid result generation")
        if not isinstance(self.outcome, MutationOutcome):
            raise ValueError("Invalid mutation outcome")
        if (type(self.revision_before) is not int or type(self.document_revision) is not int
                or self.revision_before < 0 or self.document_revision < 0):
            raise ValueError("Invalid mutation revision")
        if self.outcome == MutationOutcome.SUCCESS:
            if self.error is not None or self.document_revision != self.revision_before + 1:
                raise ValueError("Inconsistent successful mutation")
        elif self.error is None or self.document_revision != self.revision_before:
            raise ValueError("Inconsistent failed mutation")
        if self.annotation_id is not None and not isinstance(self.annotation_id, AnnotationId):
            raise ValueError("Invalid result annotation identity")
        if self.annotation is not None:
            if (not isinstance(self.annotation, AnnotationModel)
                    or self.outcome != MutationOutcome.SUCCESS
                    or self.annotation.annotation_id != self.annotation_id
                    or self.annotation.document_id != self.document_id
                    or self.annotation.document_generation != self.document_generation):
                raise ValueError("Invalid confirmed annotation")
        if (type(self.history_position) is not int or type(self.history_length) is not int
                or self.history_position < 0 or self.history_length < self.history_position):
            raise ValueError("Invalid history state")
        if type(self.history_state_id) is not int or self.history_state_id < 0:
            raise ValueError("Invalid history state identity")
        if type(self.refresh_inventory) is not bool:
            raise ValueError("Invalid inventory refresh flag")
        if self.history_delta is not None and not isinstance(self.history_delta, HistoryDelta):
            raise ValueError("Invalid history delta")

    @property
    def success(self) -> bool:
        return self.outcome == MutationOutcome.SUCCESS


class RequestState(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class CommandSubmission:
    command_id: str
    state: RequestState
    request_id: int | None = None
    result: MutationResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", validate_command_id(self.command_id))
        if self.state == RequestState.PENDING:
            if type(self.request_id) is not int or self.request_id < 1 or self.result is not None:
                raise ValueError("Invalid pending submission")
        elif self.request_id is not None or self.result is None:
            raise ValueError("Invalid completed submission")
