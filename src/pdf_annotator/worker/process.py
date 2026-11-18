"""M0 worker entry: all engine calls run on this spawned process's main thread."""

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import time

from ..spikes.fixtures import FixtureSpec
from ..viewer.coordinate_mapper import PageGeometry
from .protocol import (ErrorInfo, MAX_FRAME_BYTES, Operation, Request, Response,
                       decode, encode)


def worker_main(conn, document_id: str, fixture: FixtureSpec) -> None:
    import pymupdf

    from ..spikes.annotations import add_probe, inspect_all
    from ..spikes.fixtures import create_fixture
    from ..spikes.save import full_save_probe
    from .render import render_page

    doc = None
    generation = 1
    revision = saved_revision = 0
    try:
        create_fixture(fixture)
        doc = pymupdf.open(fixture.path)
        while True:
            request = decode(conn.recv_bytes(MAX_FRAME_BYTES))
            if not isinstance(request, Request):
                raise ValueError("Expected request")
            payload, pixels = {}, b""
            error = None
            close = False
            try:
                if request.document_id != document_id:
                    raise ValueError("Wrong document identity")
                if request.operation == Operation.CLOSE:
                    close = True
                elif request.document_generation != generation:
                    raise ValueError("Stale document generation")
                elif request.operation == Operation.INFO:
                    pages = []
                    for page in doc:
                        # Page.rect is rotated; canonical dimensions must not be.
                        w, h = page.rect.width, page.rect.height
                        if page.rotation in (90, 270):
                            w, h = h, w
                        geometry = PageGeometry(page.number, w, h, page.rotation,
                                                tuple(page.cropbox), tuple(page.mediabox))
                        landmark = page.get_drawings()[0]["rect"]
                        pages.append({**asdict(geometry), "landmark": list(landmark)})
                    payload = {"pid": os.getpid(), "path": doc.name, "page_count": len(doc),
                               "marker": doc[0].get_text(clip=pymupdf.Rect(0, 0, 200, 150)).strip(),
                               "pages": pages,
                               "annotations": inspect_all(doc), "revision": revision,
                               "base_content_hash": hashlib.sha256(b"".join(
                                   doc.xref_stream(xref) for page in doc for xref in page.get_contents()
                               )).hexdigest(),
                               "saved_revision": saved_revision,
                               "pymupdf_version": pymupdf.VersionBind}
                elif request.operation == Operation.RENDER:
                    payload, pixels = render_page(doc, request.payload)
                elif request.operation == Operation.ANNOTATION_SPIKE:
                    callout = request.payload.get("callout", False)
                    if type(callout) is not bool:
                        raise ValueError("callout must be boolean")
                    payload = add_probe(doc, callout)
                    revision += 1
                    payload["confirmed_revision"] = revision
                elif request.operation == Operation.SAVE_SPIKE:
                    outcome = full_save_probe(doc, Path(fixture.path), request.payload.get("failure", "none"))
                    doc, payload, error = outcome.document, outcome.payload, outcome.error
                    if payload.get("reopened"):
                        generation += 1
                        payload["new_generation"] = generation
                    if outcome.success:
                        saved_revision = revision
                    payload["saved_revision"] = saved_revision
                    payload["confirmed_revision"] = revision
                elif request.operation == Operation.HOLD:
                    seconds = request.payload.get("seconds", 0.1)
                    if type(seconds) not in (int, float) or not 0 <= seconds <= 10:
                        raise ValueError("Invalid hold duration")
                    time.sleep(seconds)  # deterministic busy-worker fault injection only
                    payload = {"pid": os.getpid()}
                elif request.operation == Operation.CRASH:
                    os._exit(71)  # fixture-only crash containment probe
                else:
                    raise ValueError("Unsupported probe operation")
            except Exception as exc:
                error = ErrorInfo(type(exc).__name__, "PDF probe operation failed")
            conn.send_bytes(encode(Response(document_id, request.document_generation,
                                             request.request_id, request.operation,
                                             error is None, payload, error, pixels)))
            # Do not retain the last raster while waiting for the next request:
            # its GUI-side reservation may already have been released.
            del pixels, payload
            if close:
                break
    except (EOFError, BrokenPipeError):
        # Parent closed the dedicated transport; never restart or mutate another PDF.
        pass
    finally:
        if doc is not None and not doc.is_closed:
            doc.close()
        conn.close()
