"""Read-only safe-save checks. Imported by the owner and one-shot validator only.

Graph traversal labels references by encounter order, never by PDF object number.
Decoded streams include page content, fonts, images and annotation appearances.
"""

from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import json
import multiprocessing as mp
import os
import re
from time import perf_counter
from typing import Any


@dataclass(frozen=True)
class Ref:
    number: int


def _tokens(source: str) -> list[str]:
    tokens = []
    pos = 0
    while pos < len(source):
        if source[pos].isspace():
            pos += 1
            continue
        start = pos
        if source[pos] == "(":
            depth = 1
            pos += 1
            while pos < len(source) and depth:
                char = source[pos]
                pos += 1
                if char == "\\":
                    pos += 1
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
            if depth:
                raise ValueError("Unterminated PDF string")
        else:
            match = re.match(r"<<|>>|<[^>]*>|\[|\]|/[^\s<>\[\]()/]*|[^\s<>\[\]()/]+", source[pos:])
            if match is None:
                raise ValueError("Invalid PDF token")
            pos += len(match[0])
        tokens.append(source[start:pos])
    return tokens


def _parse(source: str) -> Any:
    tokens = _tokens(source)
    pos = 0

    def value():
        nonlocal pos
        token = tokens[pos]
        pos += 1
        if token == "<<":
            result = {}
            while tokens[pos] != ">>":
                key = tokens[pos]
                pos += 1
                if not key.startswith("/") or key in result:
                    raise ValueError("Invalid PDF dictionary")
                result[key] = value()
            pos += 1
            return result
        if token == "[":
            result = []
            while tokens[pos] != "]":
                result.append(value())
            pos += 1
            return result
        if token.isdigit() and pos + 1 < len(tokens) and tokens[pos].isdigit() and tokens[pos + 1] == "R":
            pos += 2
            return Ref(int(token))
        return token

    result = value()
    if pos != len(tokens):
        raise ValueError("Trailing PDF tokens")
    return result


def semantic_digest(doc: Any) -> str:
    """Conservative structural equivalence, with bounded per-object work.

    Dictionary order, indirect numbers and stream compression are immaterial.
    Unknown keys and subtypes remain part of the comparison. No Annot.update.
    """
    digest = sha256()
    queue = deque()
    labels: dict[int, int] = {}

    def normalize(value):
        if isinstance(value, Ref):
            if not 0 < value.number < doc.xref_length():
                raise ValueError("Dangling PDF reference")
            if value.number not in labels:
                labels[value.number] = len(labels)
                queue.append(value.number)
            return {"ref": labels[value.number]}
        if isinstance(value, dict):
            return {key: normalize(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    def feed(value):
        digest.update(json.dumps(normalize(value), ensure_ascii=True, separators=(",", ":")).encode())
        digest.update(b"\n")

    trailer = _parse(doc.pdf_trailer())
    # Container offsets and file identifiers may change on a full save.
    feed({key: value for key, value in trailer.items()
          if key not in ("/Size", "/ID", "/Prev", "/XRefStm", "/Type", "/W", "/Index", "/Length", "/Filter", "/DecodeParms")})
    while queue:
        xref = queue.popleft()
        obj = _parse(doc.xref_object(xref))
        if doc.xref_is_stream(xref):
            obj = {key: value for key, value in obj.items()
                   if key not in ("/Length", "/Filter", "/DecodeParms")}
            feed(obj)
            stream = doc.xref_stream(xref)
            digest.update(sha256(stream).digest())
        else:
            feed(obj)
    return digest.hexdigest()


def locator_digest(doc: Any) -> str:
    """Additional M14 snapshot guard, separate from semantic validation.

    garbage=0/use_objstms=0 must retain even unreachable history resources and
    their locators. Refuse a candidate that needs a history remap we cannot prove.
    """
    digest = sha256()

    def normalize(value):
        if isinstance(value, Ref):
            return {"xref": value.number}
        if isinstance(value, dict):
            return {key: normalize(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    for xref in range(1, doc.xref_length()):
        try:
            obj = _parse(doc.xref_object(xref))
        except RuntimeError as exc:
            # M14 undo frees an app-created object's slot. The pinned engine
            # raises for such a free slot instead of returning PDF null. This
            # allowance is ONLY for the history locator scan: a reachable bad
            # reference is still rejected by semantic_digest above.
            if "cannot find object in xref" not in str(exc):
                raise
            digest.update(b'"null"\n')
            continue
        # Full save expands ObjStm containers and discards old xref tables.
        # Their individual objects retain their locators; history never owns
        # these transport containers. Treat their slots like the resulting null.
        container = isinstance(obj, dict) and obj.get("/Type") in ("/ObjStm", "/XRef")
        stream = doc.xref_is_stream(xref) and not container
        if container:
            obj = "null"
        elif stream:
            obj = {key: value for key, value in obj.items()
                   if key not in ("/Length", "/Filter", "/DecodeParms")}
        digest.update(json.dumps(normalize(obj), ensure_ascii=True, separators=(",", ":")).encode())
        if stream:
            digest.update(sha256(doc.xref_stream(xref)).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def base_page_text(page: Any) -> str:
    import pymupdf

    display = page.get_displaylist(annots=False)
    # The pinned rebased API returns FzStextPage here; wrap it in the public
    # TextPage value before using the documented extraction methods.
    return pymupdf.TextPage(display.get_textpage()).extractText()


def inspect_document(doc: Any) -> dict:
    if (not doc.is_pdf or doc.is_repaired or doc.needs_pass or doc.is_encrypted
            or (doc.metadata or {}).get("encryption") or doc.page_count < 1):
        raise ValueError("Invalid candidate PDF")
    pages = sha256()
    for page in doc:
        pages.update(repr((tuple(page.rect), tuple(page.cropbox), tuple(page.mediabox), page.rotation)).encode())
        # FreeText contributes text to the default page extraction in 1.28.
        # Base-content equivalence must deliberately exclude annotations.
        pages.update(base_page_text(page).encode("utf-8"))
        roots = page.annot_xrefs()
        for xref, _kind, _name in roots:
            subtype = doc.xref_get_key(xref, "Subtype")[1]
            intent = doc.xref_get_key(xref, "IT")[1]
            # Native M10 Replace is one Caret root plus one grouped StrikeOut.
            if subtype == "/Caret" and intent == "/Replace":
                members = [other for other, _, _ in roots
                           if doc.xref_get_key(other, "IRT") == ("xref", f"{xref} 0 R")
                           and doc.xref_get_key(other, "RT")[1] == "/Group"
                           and doc.xref_get_key(other, "IT")[1] == "/StrikeOutTextEdit"]
                if len(members) != 1 or doc.xref_get_key(members[0], "Subtype")[1] != "/StrikeOut":
                    raise ValueError("Incomplete replacement correction")
            if subtype in ("/Highlight", "/StrikeOut"):
                quads = _parse(doc.xref_get_key(xref, "QuadPoints")[1])
                if not isinstance(quads, list) or not quads or len(quads) % 8:
                    raise ValueError("Invalid text markup geometry")
    result = {"pages": doc.page_count, "page_digest": pages.hexdigest(),
              "semantic_digest": semantic_digest(doc), "locator_digest": locator_digest(doc)}
    if doc.is_repaired:
        raise ValueError("Candidate required repair")
    return result


def validator_main(conn: Any, candidate: str, expected: dict, started_at: float) -> None:
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    process_started = perf_counter()
    try:
        # File-backed read-only inspection; no save, mutation or source access.
        scan_started = perf_counter()
        with pymupdf.open(candidate) as doc:
            valid = inspect_document(doc) == expected
        conn.send_bytes(json.dumps({
            "valid": valid,
            "pid": os.getpid(),
            "timing_ms": {
                "validator_startup": (process_started - started_at) * 1000,
                "validator_semantic_scan": (perf_counter() - scan_started) * 1000,
            },
        }).encode())
    except Exception:
        conn.send_bytes(json.dumps({"valid": False, "pid": os.getpid()}).encode())
    finally:
        conn.close()


def validate_candidate(candidate: str, expected: dict, *, timeout: float = 120,
                       timing_ms: dict[str, float] | None = None) -> int:
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    started_at = perf_counter()
    process = ctx.Process(target=validator_main, args=(child, candidate, expected, started_at),
                         name="pdf-save-validator")
    try:
        spawn_started = perf_counter()
        process.start()
        if timing_ms is not None:
            timing_ms["validator_spawn"] = (perf_counter() - spawn_started) * 1000
        child.close()
        if not parent.poll(timeout):
            raise TimeoutError("Candidate validator timed out")
        reply = json.loads(parent.recv_bytes(1024))
        if timing_ms is not None:
            timing_ms.update(reply.get("timing_ms", {}))
            timing_ms["validator_total"] = (perf_counter() - started_at) * 1000
        process.join(5)
        if process.is_alive() or process.exitcode != 0 or not reply["valid"]:
            raise ValueError("Candidate validation failed")
        return reply["pid"]
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            process.close()
