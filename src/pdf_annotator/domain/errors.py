"""Machine-readable annotation mutation failures."""

from dataclasses import dataclass
from enum import Enum


class MutationErrorCode(str, Enum):
    INVALID_COMMAND = "invalid_command"
    INVALID_GEOMETRY = "invalid_geometry"
    STALE_GENERATION = "stale_generation"
    DOCUMENT_NOT_READY = "document_not_ready"
    UNSUPPORTED_COMMAND = "unsupported_command"
    ANNOTATION_NOT_FOUND = "annotation_not_found"
    READ_ONLY_ANNOTATION = "read_only_annotation"
    WORKER_FAILURE = "worker_failure"
    INTERNAL_PDF_MUTATION = "internal_pdf_mutation"
    DUPLICATE_COMMAND = "duplicate_command"
    BACKPRESSURE = "backpressure"
    CANCELLED = "cancelled"
    PROTOCOL_ERROR = "protocol_error"
    PAGE_OUT_OF_RANGE = "page_out_of_range"
    STALE_TEXT_TARGET = "stale_text_target"
    TEXT_PAGE_TOO_COMPLEX = "text_page_too_complex"
    TEXT_OVERFLOW = "text_overflow"
    REVIEW_CONFLICT = "review_conflict"
    REVIEW_DOCUMENT_CHANGED = "review_document_changed"


DEFAULT_ERROR_MESSAGES = {
    MutationErrorCode.INVALID_COMMAND: "Comando di annotazione non valido.",
    MutationErrorCode.INVALID_GEOMETRY: "Geometria dell'annotazione non valida.",
    MutationErrorCode.STALE_GENERATION: "Il documento è cambiato; ripeti l'operazione.",
    MutationErrorCode.DOCUMENT_NOT_READY: "Il documento non è pronto.",
    MutationErrorCode.UNSUPPORTED_COMMAND: "Questo comando non è ancora disponibile.",
    MutationErrorCode.ANNOTATION_NOT_FOUND: "Annotazione non trovata.",
    MutationErrorCode.READ_ONLY_ANNOTATION: "L'annotazione è di sola lettura.",
    MutationErrorCode.WORKER_FAILURE: "Il processo PDF si è interrotto.",
    MutationErrorCode.INTERNAL_PDF_MUTATION: "La modifica PDF non è riuscita.",
    MutationErrorCode.DUPLICATE_COMMAND: "Comando duplicato rifiutato.",
    MutationErrorCode.BACKPRESSURE: "Troppe operazioni in corso.",
    MutationErrorCode.CANCELLED: "Operazione annullata.",
    MutationErrorCode.PROTOCOL_ERROR: "Risposta di modifica non valida.",
    MutationErrorCode.PAGE_OUT_OF_RANGE: "Pagina di destinazione non valida.",
    MutationErrorCode.STALE_TEXT_TARGET: "La selezione testuale non è più valida.",
    MutationErrorCode.TEXT_PAGE_TOO_COMPLEX: "La pagina contiene troppo testo selezionabile.",
    MutationErrorCode.TEXT_OVERFLOW: "Il testo non entra nel riquadro.",
    MutationErrorCode.REVIEW_CONFLICT: "Il bersaglio è già interessato da una correzione.",
    MutationErrorCode.REVIEW_DOCUMENT_CHANGED: "Il documento della revisione è cambiato.",
}


@dataclass(frozen=True, slots=True)
class MutationError:
    code: MutationErrorCode
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, MutationErrorCode):
            raise ValueError("Invalid mutation error code")
        if not isinstance(self.message, str) or not 0 < len(self.message) <= 512:
            raise ValueError("Invalid mutation error message")

    @classmethod
    def create(cls, code: MutationErrorCode) -> "MutationError":
        return cls(code, DEFAULT_ERROR_MESSAGES[code])


class DomainValidationError(ValueError):
    def __init__(self, code: MutationErrorCode, message: str = "", *,
                 validator: str | None = None, field: str | None = None,
                 reason: str | None = None, value_category: str | None = None,
                 page_index: int | None = None) -> None:
        self.code = code
        self.validator = validator
        self.field = field
        self.reason = reason
        self.value_category = value_category
        self.page_index = page_index
        super().__init__(message or DEFAULT_ERROR_MESSAGES[code])
