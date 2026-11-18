"""Persistent user-document owner: metadata and bounded rendering, no mutations."""

from dataclasses import asdict, dataclass, replace
import logging
import os
from time import perf_counter
from typing import Any

from ..domain.errors import (DomainValidationError, MutationError,
                             MutationErrorCode)
from ..domain.geometry import PageBounds
from ..domain.serialization import (result_to_payload, target_to_payload,
                                    text_chunk_to_payload,
                                    text_page_index_to_payload)
from ..util.errors import OpenError, open_error
from ..util.paths import FileIdentity, identify_file
from ..viewer.coordinate_mapper import PageGeometry
from .protocol import (ErrorInfo, MAX_SINGLE_RASTER_BYTES, MAX_GEOMETRY_PAGES, Operation,
                       Request, Response, decode, encode)


@dataclass(frozen=True)
class DocumentSource:
    identity: FileIdentity
    raster_limit: int = MAX_SINGLE_RASTER_BYTES


@dataclass(frozen=True)
class OpenDocumentResult:
    page_count: int
    format: str

    def __post_init__(self) -> None:
        if type(self.page_count) is not int or self.page_count <= 0:
            raise ValueError("Invalid page count")
        if not isinstance(self.format, str) or len(self.format) > 128:
            raise ValueError("Invalid document format")


@dataclass(frozen=True)
class GeometryBatch:
    start: int
    pages: tuple[PageGeometry, ...]

    @classmethod
    def from_payload(cls, payload: dict) -> "GeometryBatch":
        start, records = payload["start"], payload["pages"]
        if type(start) is not int or start < 0 or not isinstance(records, list):
            raise ValueError("Invalid geometry batch")
        if not 1 <= len(records) <= MAX_GEOMETRY_PAGES:
            raise ValueError("Invalid geometry batch size")
        pages = []
        for index, record in enumerate(records, start):
            page = PageGeometry(**{**record, "cropbox": tuple(record["cropbox"]),
                                   "mediabox": tuple(record["mediabox"])})
            if page.page_index != index:
                raise ValueError("Non-contiguous geometry")
            pages.append(page)
        return cls(start, tuple(pages))


def open_document(source: DocumentSource) -> Any:
    import pymupdf

    expected = source.identity
    if identify_file(expected.canonical_path) != expected:
        raise OpenError("SOURCE_CHANGED")
    # Preserve native permission/sharing errors (PyMuPDF can wrap them as a
    # generic FileDataError). On Windows this read handle also guards against
    # path replacement between fstat and the engine's own open.
    with open(expected.canonical_path, "rb") as guard:
        if FileIdentity.from_stat(expected.canonical_path, os.fstat(guard.fileno())) != expected:
            raise OpenError("SOURCE_CHANGED")
        doc = pymupdf.open(expected.canonical_path)
    try:
        if not doc.is_pdf:
            raise OpenError("NOT_PDF")
        # needs_pass alone misses owner-only protection with an empty user password.
        if doc.needs_pass or doc.is_encrypted or (doc.metadata or {}).get("encryption"):
            raise OpenError("ENCRYPTED")
        if doc.is_repaired or doc.page_count <= 0:
            raise OpenError("INVALID_PDF")
        if identify_file(expected.canonical_path) != expected:
            raise OpenError("SOURCE_CHANGED")
        return doc
    except Exception:
        doc.close()
        raise


def page_geometry(doc: Any, payload: dict) -> GeometryBatch:
    start = payload.get("start")
    if type(start) is not int or not 0 <= start < doc.page_count:
        raise ValueError("Invalid geometry offset")
    pages = []
    for index in range(start, min(start + MAX_GEOMETRY_PAGES, doc.page_count)):
        page = doc.load_page(index)
        # The same unrotated coordinate contract proven in M0.
        width, height = page.rect.width, page.rect.height
        if page.rotation in (90, 270):
            width, height = height, width
        pages.append(PageGeometry(index, width, height, page.rotation,
                                  tuple(page.cropbox), tuple(page.mediabox)))
    if doc.is_repaired:
        raise OpenError("INVALID_PDF")
    return GeometryBatch(start, tuple(pages))


def document_worker_main(conn: Any, document_id: str, source: DocumentSource) -> None:
    run_document_worker(conn, document_id, source)


def run_document_worker(conn: Any, document_id: str, source: DocumentSource, *,
                        mutation_adapter=None, mutation_adapter_factory=None,
                        prepare_annotation_command=None) -> None:
    """One live Document on the spawned process's main thread, until CLOSE/EOF."""
    import pymupdf
    from pathlib import Path
    from ..services.review_batches import REVIEW_PDF_LIMIT
    from .annotation_dispatch import AnnotationCommandDispatcher
    from .protocol import frame_limit
    from .render import render_page
    from .annotation_appearance import render_annotation_appearance
    from .review_snapshot import ReviewError, ReviewSnapshotStore

    # Parser diagnostics can include fragments from an untrusted PDF. Report
    # only classified errors to the UI and log codes/types, never raw messages.
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    doc = None
    dispatcher = None
    text_service = None
    annotation_inventory = None
    review_store = None
    review_application = None
    generation = 1
    current_identity = source.identity

    def bounds_for_page(index: int) -> PageBounds:
        if doc is None or type(index) is not int or not 0 <= index < doc.page_count:
            raise IndexError(index)
        page = doc.load_page(index)
        width, height = page.rect.width, page.rect.height
        if page.rotation in (90, 270):
            width, height = height, width
        return PageBounds(width, height)

    try:
        while True:
            request = decode(conn.recv_bytes(frame_limit(source.raster_limit)), source.raster_limit)
            if not isinstance(request, Request):
                raise ValueError("Expected request")
            payload, error, pixels = {}, None, b""
            prepared_frame = None
            closing = request.operation == Operation.CLOSE
            try:
                if request.document_id != document_id or request.document_generation != generation:
                    raise ValueError("Stale/mismatched document identity")
                if closing:
                    if doc is not None:
                        doc.close()
                        doc = None
                elif request.operation == Operation.OPEN_DOCUMENT:
                    if doc is not None:
                        raise ValueError("Document already open")
                    doc = open_document(source)
                    from .annotation_adapter import ProductAnnotationAdapter
                    from .text_service import WorkerTextService
                    text_service = WorkerTextService(document_id, 1, doc)
                    from .annotation_inventory import NativeAnnotationInventory
                    annotation_inventory = NativeAnnotationInventory(doc)
                    if mutation_adapter is not None and mutation_adapter_factory is not None:
                        raise ValueError("Conflicting mutation adapter injection")
                    adapter = (mutation_adapter_factory(text_service)
                               if mutation_adapter_factory is not None else mutation_adapter)
                    if adapter is None:
                        adapter = ProductAnnotationAdapter(text_service)
                    dispatcher = AnnotationCommandDispatcher(document_id, 1, bounds_for_page,
                                                             adapter)
                    payload = asdict(OpenDocumentResult(doc.page_count,
                                                       (doc.metadata or {}).get("format", "PDF")))
                elif request.operation == Operation.PAGE_GEOMETRY and doc is not None:
                    payload = asdict(page_geometry(doc, request.payload))
                elif request.operation == Operation.REVIEW_SNAPSHOT and doc is not None:
                    data = request.payload
                    if set(data) != {"workspace"} or not isinstance(data["workspace"], str):
                        raise ReviewError("REVIEW_INVALID_REQUEST", "Richiesta snapshot non valida.")
                    workspace = Path(data["workspace"])
                    if not workspace.is_absolute() or not workspace.is_dir():
                        raise ReviewError("REVIEW_INVALID_REQUEST", "Directory snapshot non disponibile.")
                    if review_store is None:
                        review_store = ReviewSnapshotStore(workspace)
                        from .review_application import ReviewApplicationWorker
                        review_application = ReviewApplicationWorker(review_store)
                    elif workspace != review_store.workspace:
                        raise ReviewError("REVIEW_INVALID_REQUEST", "Directory snapshot non riconosciuta.")
                    payload = review_store.capture(doc, document_id, generation,
                                                   dispatcher.document_revision, current_identity)
                elif request.operation in (Operation.REVIEW_EXTRACT, Operation.REVIEW_RELEASE_EXTRACT,
                                           Operation.REVIEW_RELEASE) and doc is not None:
                    data = request.payload
                    if review_store is None:
                        raise ReviewError("REVIEW_SNAPSHOT_UNKNOWN", "Snapshot non disponibile per questa scheda.")
                    if request.operation == Operation.REVIEW_RELEASE and set(data) == {"snapshot_id"}:
                        review_store.release(data["snapshot_id"])
                        review_application.clear()
                    elif request.operation == Operation.REVIEW_RELEASE_EXTRACT and set(data) == {"snapshot_id", "extract_id"}:
                        payload = {"released": review_store.release_extract(data["snapshot_id"], data["extract_id"])}
                    elif (request.operation == Operation.REVIEW_EXTRACT
                          and {"snapshot_id", "first_page", "last_page"} <= data.keys()
                          <= {"snapshot_id", "first_page", "last_page", "max_bytes", "split", "max_context_pages"}
                          and type(data.get("split", True)) is bool
                          and type(data.get("max_context_pages", 2)) is int
                          and 0 <= data.get("max_context_pages", 2) <= 2):
                        payload = review_store.extract(data["snapshot_id"], data["first_page"],
                                                       data["last_page"], data.get("max_bytes", REVIEW_PDF_LIMIT),
                                                       split=data.get("split", True),
                                                       max_context_pages=data.get("max_context_pages", 2))
                    else:
                        raise ReviewError("REVIEW_INVALID_REQUEST", "Richiesta estratto non valida.")
                elif request.operation in (Operation.REVIEW_LOCALIZE, Operation.REVIEW_CONFIRM,
                                           Operation.REVIEW_LOOKUP) and doc is not None:
                    if review_application is None:
                        raise ReviewError("REVIEW_SNAPSHOT_UNKNOWN", "Snapshot non disponibile per questa scheda.")
                    if request.operation == Operation.REVIEW_LOCALIZE:
                        result = review_application.localize(request.payload)
                    elif request.operation == Operation.REVIEW_CONFIRM:
                        result = review_application.confirm(request.payload, text_service, current_identity)
                    else:
                        result = review_application.lookup(request.payload, text_service, adapter, current_identity)
                    try:
                        prepared_frame = encode(Response(document_id, request.document_generation,
                            request.request_id, request.operation, True, result), source.raster_limit)
                    except ValueError:
                        raise ReviewError("REVIEW_ITEM_TOO_LARGE", "Proposta di revisione troppo grande.") from None
                elif request.operation == Operation.SAVE and doc is not None:
                    from .save_service import SaveRequest, safe_save
                    save_request = SaveRequest.from_payload(request.payload)
                    if dispatcher is None or save_request.revision != dispatcher.document_revision:
                        raise ValueError("Save revision barrier mismatch")
                    outcome = safe_save(doc, current_identity, save_request)
                    doc, payload, error = outcome.document, outcome.payload, outcome.error
                    if outcome.reopened:
                        rebind_started = perf_counter()
                        generation += 1
                        payload["new_generation"] = generation
                        current_identity = FileIdentity(**payload["identity"])
                        text_service.trim()
                        text_service = WorkerTextService(document_id, generation, doc)
                        adapter.rebind(text_service, generation)
                        dispatcher.rebind_generation(generation)
                        if review_application is not None:
                            review_application.clear()
                        annotation_inventory = NativeAnnotationInventory(doc)
                        payload.setdefault("timing_ms", {})["post_reopen_rebind_invalidation"] = (
                            perf_counter() - rebind_started) * 1000
                elif request.operation == Operation.RENDER and doc is not None:
                    payload, pixels = render_page(doc, request.payload, raster_limit=source.raster_limit)
                elif request.operation == Operation.ANNOTATION_APPEARANCE and doc is not None:
                    payload, pixels = render_annotation_appearance(
                        doc, request.payload, raster_limit=source.raster_limit)
                elif request.operation == Operation.ANNOTATION_COMMAND and doc is not None:
                    if dispatcher is None:
                        raise RuntimeError("Mutation dispatcher unavailable")
                    next_inventory = None

                    def finalize_mutation(result):
                        nonlocal payload, prepared_frame, next_inventory
                        delta = result.history_delta
                        if delta is not None and (delta.inventory_added or delta.inventory_updated
                                                  or delta.inventory_removed_ids):
                            next_inventory, refresh = annotation_inventory.prepare_delta(delta)
                            if refresh:
                                result = replace(result, refresh_inventory=True)
                        else:
                            next_inventory = NativeAnnotationInventory(doc)
                        payload = result_to_payload(result)
                        prepared_frame = encode(Response(document_id, request.document_generation,
                            request.request_id, request.operation, True, payload), source.raster_limit)
                        return result

                    # Python-only injection for the offline experiment. The normal worker
                    # accepts only the existing serialized annotation commands.
                    command = (prepare_annotation_command(text_service, request.payload)
                               if prepare_annotation_command is not None else request.payload)
                    authorize = (None if review_application is None else
                                 lambda item: review_application.authorize(item, adapter, current_identity))
                    mutation_result = dispatcher.dispatch(doc, command, finalize=finalize_mutation,
                                                          authorize_review=authorize)
                    if mutation_result.success:
                        annotation_inventory = next_inventory
                    else:
                        prepared_frame = None
                        payload = result_to_payload(mutation_result)
                elif request.operation == Operation.ANNOTATION_SUMMARIES and annotation_inventory is not None:
                    payload = annotation_inventory.summaries(request.payload.get("cursor", 0))
                elif request.operation == Operation.ANNOTATION_DETAIL and annotation_inventory is not None:
                    payload = annotation_inventory.detail(request.payload.get("logical_id"),
                                                          request.payload.get("offset", 0))
                elif request.operation == Operation.TEXT_PAGE_INDEX and text_service is not None:
                    index = text_service.page_index(request.payload.get("page_index"))
                    payload = {"page_text": text_page_index_to_payload(index),
                               "metrics": text_service.metrics()}
                elif request.operation == Operation.TEXT_CHUNK and text_service is not None:
                    chunk = text_service.chunk(
                        request.payload.get("page_index"),
                        request.payload.get("page_text_id"),
                        request.payload.get("chunk_index"))
                    payload = {"chunk": text_chunk_to_payload(chunk),
                               "metrics": text_service.metrics()}
                elif request.operation == Operation.TEXT_SELECTION and text_service is not None:
                    status, target = text_service.resolve(request.payload)
                    payload = {"status": status,
                               "target": target_to_payload(target) if target is not None else None}
                elif request.operation == Operation.TEXT_INSERTION and text_service is not None:
                    status, target = text_service.resolve_insertion(request.payload)
                    payload = {"status": status,
                               "target": target_to_payload(target) if target is not None else None}
                elif request.operation == Operation.TEXT_TRIM and text_service is not None:
                    if request.payload:
                        raise DomainValidationError(MutationErrorCode.INVALID_COMMAND)
                    text_service.trim()
                    payload = {"cache_bytes": 0}
                elif request.operation == Operation.SEARCH_SLICE and text_service is not None:
                    payload = text_service.search_slice(request.payload)
                else:
                    # Disposable mutation/save probes must never operate on user PDFs.
                    raise ValueError("Operation unavailable for user documents")
            except ReviewError as exc:
                error = ErrorInfo(exc.code, str(exc))
            except Exception as exc:
                if isinstance(exc, DomainValidationError):
                    page = exc.page_index
                    if page is None and type(request.payload.get("page_index")) is int:
                        page = request.payload["page_index"]
                    logging.getLogger("pdf_annotator").warning(
                        "document=%s operation=%s page=%s error=%s validator=%s "
                        "field=%s reason=%s value_category=%s",
                        document_id, request.operation.value,
                        page if page is not None else "-", exc.code.value,
                        exc.validator or "unspecified", exc.field or "unspecified",
                        exc.reason or "unspecified",
                        exc.value_category or "unspecified")
                    domain_error = MutationError.create(exc.code)
                    error = ErrorInfo(domain_error.code.value, domain_error.message)
                else:
                    logging.getLogger("pdf_annotator").warning(
                        "document=%s operation=%s page=%s error=%s validator=worker "
                        "field=operation reason=unexpected_exception value_category=%s",
                        document_id, request.operation.value,
                        request.payload.get("page_index", "-"), type(exc).__name__,
                        type(exc).__name__)
                    # Keep the UI message classified, but retain the traceback
                    # locally while diagnosing an M15.1 worker-only failure.
                    logging.getLogger("pdf_annotator").warning(
                        "worker operation traceback", exc_info=True)
                    error = open_error(OpenError("WORKER_FAILED") if doc is not None and doc.is_closed else exc)
            conn.send_bytes(prepared_frame if prepared_frame is not None and error is None else encode(Response(document_id, request.document_generation,
                                             request.request_id, request.operation,
                                             error is None, payload, error, pixels), source.raster_limit))
            del pixels, payload  # do not retain the last raster while idle
            pymupdf.TOOLS.reset_mupdf_warnings()
            if closing or (doc is not None and doc.is_closed):
                break
    except (EOFError, BrokenPipeError):
        pass  # owner transport was closed; always release the live document below
    finally:
        if doc is not None and not doc.is_closed:
            doc.close()
        conn.close()
