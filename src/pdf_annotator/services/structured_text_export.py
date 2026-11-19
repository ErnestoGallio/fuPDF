"""Background JSON export using the existing nonblocking worker transport.

No PDF engine imports here. Only one bounded local page is read at a time;
page text travels through bounded private files.
"""

import asyncio
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable

from ..domain.structured_text import (ExtractionIdentity, MAX_PAGE_JSON_BYTES,
                                      PageText, canonical_json, integer)
from ..worker.client import WorkerClient
from ..worker.protocol import Operation
from .review_worker import ReviewWorker, ReviewWorkerError


class StructuredTextWorker(ReviewWorker):
    async def page(self, identity: ExtractionIdentity, page_index: int,
                   stopping: Callable[[], bool] = lambda: False) -> PageText:
        if self.client.generation != identity.document_generation or self.client.document_id != identity.document_id:
            raise ReviewWorkerError("TEXT_STALE")
        response = await self._request(Operation.STRUCTURED_TEXT_PAGE,
                                       {"page_index": page_index, "revision": identity.document_revision}, stopping)
        info = response.payload
        try:
            size = info["size_bytes"]
            if type(size) is not int or not 0 < size <= MAX_PAGE_JSON_BYTES:
                raise ValueError("Invalid text page size")
            with self.client.review_reader():
                path = Path(info["path"])
                if (not path.is_absolute() or not path.name.startswith("structured-page-")
                        or path.suffix != ".json"):
                    raise ValueError("Invalid text page path")
                try:
                    with path.open("rb") as stream:
                        data = stream.read(size + 1)
                    if len(data) != size or sha256(data).hexdigest() != info["sha256"]:
                        raise ValueError("Invalid text page file")
                    page = PageText.from_payload(json.loads(data))
                finally:
                    path.unlink(missing_ok=True)
            if (ExtractionIdentity(**info["identity"]) != identity or page.page != page_index + 1
                    or info["page_number"] != page.page):
                raise ValueError("Text page identity differs")
            return page
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ReviewWorkerError("TEXT_INVALID_PAGE") from exc


def export_document(client: WorkerClient, revision: int, page_count: int, destination: str,
                    *, stopping: Callable[[], bool] = lambda: False,
                    protected_paths: tuple[str, ...] = ()) -> str:
    """Stream an atomic pages-only JSON export; future consumers can read pages directly.

    Run outside Qt. Temporary output is a sibling; an interrupted/failed export
    leaves an existing destination intact. The PDF worker never writes here.
    """
    integer(page_count, 1)
    identity = ExtractionIdentity(client.document_id, client.generation, revision)
    path = Path(destination).resolve()
    if path.suffix.lower() != ".json":
        raise ValueError("TEXT_JSON_REQUIRED")
    for source in protected_paths:
        source_path = Path(source).resolve()
        if path == source_path or (path.exists() and source_path.exists() and path.samefile(source_path)):
            raise ValueError("TEXT_SOURCE_PROTECTED")
    worker = StructuredTextWorker(client)
    temporary = None
    try:
        with ExitStack() as stack:
            runner = stack.enter_context(asyncio.Runner())
            output = stack.enter_context(NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                prefix=".fupdf-text-", suffix=".tmp", dir=path.parent, delete=False))
            temporary = Path(output.name)
            output.write('{"pages":[\n')
            for index in range(page_count):
                if stopping():
                    raise ReviewWorkerError("cancelled")
                page = runner.run(worker.page(identity, index, stopping))
                if stopping():
                    raise ReviewWorkerError("cancelled")
                if index:
                    output.write(",\n")
                output.write("  " + canonical_json(page.to_payload()))
            output.write("\n]}\n")
            output.flush()
            os.fsync(output.fileno())
        if stopping() or client.generation != identity.document_generation:
            raise ReviewWorkerError("TEXT_STALE")
        os.replace(temporary, path)
        temporary = None
        return str(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
