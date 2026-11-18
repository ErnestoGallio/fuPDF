"""Small, content-free user errors shared by file resolution and PDF opening."""

from ..worker.protocol import ErrorInfo

MESSAGES = {
    "NOT_FOUND": "Il file non esiste più.",
    "UNREADABLE": "Impossibile leggere il file. Controlla i permessi.",
    "NOT_PDF": "Seleziona un file PDF valido.",
    "INVALID_PDF": "PDF danneggiato o non supportato.",
    "ENCRYPTED": "I PDF cifrati o protetti non sono supportati.",
    "SOURCE_CHANGED": "Il file è cambiato durante l'apertura. Riprova.",
    "WORKER_FAILED": "Il processo PDF si è interrotto. Chiudi e riapri il documento.",
    "OPEN_BUSY": "Troppe aperture in corso. Attendi e riprova.",
    "CLOSING": "Il documento è in chiusura. Attendi prima di riaprirlo.",
    "DIRTY_CLOSE_BLOCKED": "Il documento ha modifiche non salvate. Chiusura sospesa.",
    "PROTOCOL_ERROR": "Impossibile completare l'apertura del PDF.",
}


class OpenError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def user_error(code: str) -> ErrorInfo:
    return ErrorInfo(code, MESSAGES[code])


def open_error(exc: Exception) -> ErrorInfo:
    if isinstance(exc, OpenError):
        return user_error(exc.code)
    if isinstance(exc, FileNotFoundError):
        return user_error("NOT_FOUND")
    if isinstance(exc, OSError):
        return user_error("UNREADABLE")
    return user_error("INVALID_PDF")
