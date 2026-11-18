"""Disposable full-save experiment, deliberately not production Save/Save As.

The live owner remains open during read-only validation. On replace failure the
validated candidate becomes recoverable backing state; it is never deleted.
"""

from dataclasses import dataclass
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
from typing import Any

from ..worker.protocol import (ErrorInfo, MAX_FRAME_BYTES, Operation, Response,
                               decode, encode)

MAX_CANDIDATE_BYTES = 16 * 1024 * 1024  # disposable M0 fixtures only


def validator_main(conn, candidate: str, expected_pages: int,
                   expected_annotations: int, crash: bool) -> None:
    """One-shot child; filesystem access to candidate is exclusively rb."""
    import pymupdf

    if crash:
        os._exit(73)  # explicit fault injection, not normal shutdown
    try:
        with open(candidate, "rb") as source:
            content = source.read(MAX_CANDIDATE_BYTES + 1)
        if len(content) > MAX_CANDIDATE_BYTES:
            raise ValueError("Candidate exceeds spike limit")
        with pymupdf.open(stream=content, filetype="pdf") as checked:
            if checked.is_repaired or len(checked) != expected_pages:
                raise ValueError("Candidate validation failed")
            count = sum(1 for page in checked for _ in page.annots() or ())
            if count != expected_annotations:
                raise ValueError("Annotation inventory mismatch")
        reply = Response("validator", 1, 1, Operation.SAVE_SPIKE, True,
                         {"pid": os.getpid(), "page_count": expected_pages, "annotations": count})
    except Exception as exc:
        reply = Response("validator", 1, 1, Operation.SAVE_SPIKE, False,
                         {"pid": os.getpid()}, ErrorInfo(type(exc).__name__, "Candidate validation failed"))
    try:
        conn.send_bytes(encode(reply))
    finally:
        conn.close()


def validate(candidate: Path, page_count: int, annot_count: int, crash: bool) -> dict:
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=validator_main,
                          args=(child, str(candidate), page_count, annot_count, crash))
    try:
        process.start()
        child.close()
        if not parent.poll(10):
            raise TimeoutError("Validator timeout")
        reply = decode(parent.recv_bytes(MAX_FRAME_BYTES))
        process.join(timeout=5)
        if process.is_alive() or process.exitcode != 0:
            raise ChildProcessError("Validator failed to exit")
        if not isinstance(reply, Response) or not reply.success:
            raise ValueError("Candidate rejected")
        return reply.payload
    finally:
        child.close()
        parent.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if not process.is_alive():
                process.close()


@dataclass
class SaveOutcome:
    document: Any
    success: bool
    payload: dict
    error: ErrorInfo | None = None


def full_save_probe(doc: Any, target: Path, failure: str = "none") -> SaveOutcome:
    import pymupdf

    if failure not in ("none", "candidate", "validator", "replace", "write"):
        raise ValueError("Unknown save fault injection")
    fd, name = tempfile.mkstemp(prefix=".m0-candidate-", suffix=".pdf", dir=target.parent)
    os.close(fd)
    candidate = Path(name)
    phases = ["candidate_created"]
    try:
        if failure == "write":
            raise OSError("Injected candidate write failure")
        doc.save(candidate, incremental=False, garbage=0, clean=False)
        phases.append("candidate_written")
        if failure == "candidate":
            candidate.write_bytes(b"not a PDF candidate")
        count = sum(1 for page in doc for _ in page.annots() or ())
        validation = validate(candidate, len(doc), count, failure == "validator")
        phases.append("validator_exited")
    except Exception as exc:
        candidate.unlink(missing_ok=True)  # live owner still holds all changes
        return SaveOutcome(doc, False, {"phase": "before_close", "phases": phases,
                                       "live_usable": not doc.is_closed, "dirty": True},
                           ErrorInfo(type(exc).__name__, "Temporary save validation failed"))
    doc.close()
    phases.append("live_closed")
    try:
        if failure == "replace":
            raise PermissionError("Injected replacement failure")
        os.replace(candidate, target)
        phases.append("replaced")
    except OSError as exc:
        recovered = pymupdf.open(candidate)
        return SaveOutcome(recovered, False,
                           {"phase": "replace", "phases": phases, "dirty": True,
                            "recovery_path": str(candidate), "reopened": True,
                            "validator_pid": validation["pid"]},
                           ErrorInfo(type(exc).__name__, "Replacement failed; recovery candidate retained"))
    reopened = pymupdf.open(target)
    phases.append("reopened")
    return SaveOutcome(reopened, True, {"phase": "complete", "phases": phases,
                                       "validator_pid": validation["pid"],
                                       "reopened": True, "dirty": False})
