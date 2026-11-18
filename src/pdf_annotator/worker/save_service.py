"""Production atomic full-save transaction, executed only by the PDF owner."""

from dataclasses import asdict, dataclass
import logging
import os
from pathlib import Path
import shutil
import tempfile
from time import perf_counter
from typing import Any

from ..util.paths import FileIdentity, SaveTarget, identify_file, identify_save_target
from .protocol import ErrorInfo
from .save_validation import inspect_document, validate_candidate


@dataclass(frozen=True)
class SaveRequest:
    target: SaveTarget
    revision: int
    overwrite: bool = False

    @classmethod
    def from_payload(cls, value: dict) -> "SaveRequest":
        if set(value) != {"target", "revision", "overwrite"}:
            raise ValueError("Invalid save request")
        target = value["target"]
        if set(target) != {"canonical_path", "existing"}:
            raise ValueError("Invalid save target")
        existing = FileIdentity(**target["existing"]) if target["existing"] is not None else None
        if (not isinstance(target["canonical_path"], str) or type(value["revision"]) is not int
                or value["revision"] < 0 or type(value["overwrite"]) is not bool):
            raise ValueError("Invalid save precondition")
        return cls(SaveTarget(target["canonical_path"], existing), value["revision"], value["overwrite"])


@dataclass
class SaveOutcome:
    document: Any
    success: bool
    reopened: bool
    payload: dict
    error: ErrorInfo | None = None


def _temporary(target: Path, label: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".pdf-annotator-{label}-", suffix=".pdf", dir=target.parent)
    os.close(fd)
    return Path(name)


def _sync(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def safe_save(doc: Any, source: FileIdentity, request: SaveRequest, *,
              validate=validate_candidate, replace=os.replace, reopen=None,
              write=None) -> SaveOutcome:
    """Fault seams are Python-only test hooks, never exposed in production IPC.

    A rollback copy protects an existing target if final reopen fails. Recover
    the validated bytes into the sole owner before deleting the temporary file.
    If that recovery itself fails, retain and report all recovery paths.
    """
    import pymupdf

    reopen = reopen or pymupdf.open
    target = Path(request.target.canonical_path)
    candidate = backup = None
    closed = replaced = False
    phase = "preflight"
    started_at = perf_counter()
    timing_ms: dict[str, float] = {}
    payload: dict = {"reopened": False, "timing_ms": timing_ms}
    try:
        phase_started = perf_counter()
        if identify_file(source.canonical_path) != source:
            raise FileExistsError("Source changed externally")
        if identify_save_target(str(target)) != request.target:
            raise FileExistsError("Destination changed externally")
        same_path = os.path.normcase(str(target)) == source.path_key
        if request.target.existing is not None and not same_path and not request.overwrite:
            raise FileExistsError("Overwrite confirmation required")
        timing_ms["preflight"] = (perf_counter() - phase_started) * 1000
        phase = "write"
        candidate = _temporary(target, "candidate")
        scan_started = perf_counter()
        expected = inspect_document(doc)
        timing_ms["owner_semantic_scan"] = (perf_counter() - scan_started) * 1000
        write_started = perf_counter()
        if write is not None:
            write(doc, candidate)
        else:
            doc.save(candidate, incremental=False, garbage=0, clean=False,
                     deflate=False, use_objstms=0, no_new_id=True, raise_on_repair=True)
        timing_ms["candidate_write"] = (perf_counter() - write_started) * 1000
        sync_started = perf_counter()
        _sync(candidate)
        timing_ms["candidate_fsync"] = (perf_counter() - sync_started) * 1000
        phase = "validation"
        validation_timing: dict[str, float] = {}
        if validate is validate_candidate:
            payload["validator_pid"] = validate(str(candidate), expected, timing_ms=validation_timing)
        else:
            payload["validator_pid"] = validate(str(candidate), expected)
        timing_ms.update(validation_timing)
        # Recheck after potentially slow validation and before acquiring backup.
        if identify_file(source.canonical_path) != source or identify_save_target(str(target)) != request.target:
            raise FileExistsError("File changed during save")
        if request.target.existing is not None:
            backup_started = perf_counter()
            backup = _temporary(target, "rollback")
            shutil.copy2(target, backup)
            _sync(backup)
            timing_ms["rollback_copy_fsync"] = (perf_counter() - backup_started) * 1000
        if identify_file(source.canonical_path) != source or identify_save_target(str(target)) != request.target:
            raise FileExistsError("File changed during save")
        phase = "replace"
        close_started = perf_counter()
        doc.close()
        timing_ms["owner_close"] = (perf_counter() - close_started) * 1000
        closed = True
        replace_started = perf_counter()
        replace(candidate, target)
        timing_ms["os_replace"] = (perf_counter() - replace_started) * 1000
        replaced = True
        phase = "reopen"
        reopen_started = perf_counter()
        doc = reopen(str(target))
        timing_ms["owner_reopen"] = (perf_counter() - reopen_started) * 1000
        if doc.is_repaired or doc.page_count != expected["pages"]:
            doc.close()
            raise ValueError("Final reopen failed")
        payload.update(identity=asdict(identify_file(str(target))), saved_revision=request.revision,
                       reopened=True)
        return SaveOutcome(doc, True, True, payload)
    except Exception as exc:
        logging.getLogger("pdf_annotator").warning("save phase=%s error=%s", phase, type(exc).__name__)
        recovery_paths = []
        if closed:
            try:
                if doc is not None and not doc.is_closed:
                    doc.close()
                if replaced:
                    # Retain confirmed edits before rolling the destination back.
                    shutil.copyfile(target, candidate)
                    if backup is not None:
                        os.replace(backup, target)
                    else:
                        target.unlink()
                # One live writable document, backed by memory after failure;
                # source signature/path continue to describe the unchanged file.
                doc = pymupdf.open(stream=candidate.read_bytes(), filetype="pdf")
                payload["reopened"] = True
                payload["identity"] = asdict(identify_file(source.canonical_path))
            except Exception as recovery_error:
                logging.getLogger("pdf_annotator").error("save recovery error=%s", type(recovery_error).__name__)
                recovery_paths = [str(path) for path in (candidate, backup) if path and path.exists()]
                payload["recovery_paths"] = recovery_paths
                payload["unusable"] = True
        code = "SAVE_CONFLICT" if isinstance(exc, FileExistsError) else "SAVE_FAILED"
        message = ("Il file è cambiato o richiede conferma di sovrascrittura. Salvataggio annullato."
                   if code == "SAVE_CONFLICT" else "Salvataggio non riuscito. Le modifiche restano da salvare.")
        if recovery_paths:
            message += " Copia di recupero: " + "; ".join(recovery_paths)
        return SaveOutcome(doc, False, bool(payload["reopened"]), payload, ErrorInfo(code, message))
    finally:
        cleanup_started = perf_counter()
        for path in (candidate, backup):
            if path is not None and str(path) not in payload.get("recovery_paths", ()):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logging.getLogger("pdf_annotator").warning("save cleanup error=%s", type(exc).__name__)
                    payload.setdefault("cleanup_paths", []).append(str(path))
        timing_ms["cleanup"] = (perf_counter() - cleanup_started) * 1000
        timing_ms["worker_total"] = (perf_counter() - started_at) * 1000
