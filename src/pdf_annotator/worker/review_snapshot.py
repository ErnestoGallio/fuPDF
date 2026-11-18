"""Offline snapshots and bounded extract leases owned by one PDF worker."""

from dataclasses import asdict, replace
from hashlib import file_digest
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from ..services.review_batches import (
    REVIEW_BATCH_PAGES, REVIEW_PDF_LIMIT, ReviewBatch, ReviewPlan, ReviewSnapshot,
    check_interval, context_variants, make_batch,
)
from ..util.paths import FileIdentity, identify_file

if TYPE_CHECKING:
    import pymupdf


class ReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def fingerprint(path: Path) -> str:
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def write_snapshot(doc: "pymupdf.Document", path: Path) -> None:
    import pymupdf

    # Pinned PyMuPDF 1.28.2 Document.save() calls JM_embedded_clean on the
    # live catalog, changing PageMode for PDFs with embedded files.
    pdf = pymupdf.mupdf.pdf_document_from_fz_document(doc.this)
    options = pymupdf.mupdf.PdfWriteOptions()
    options.dont_regenerate_id = 1
    options.do_preserve_metadata = 1
    options.do_encrypt = pymupdf.PDF_ENCRYPT_KEEP
    pymupdf.mupdf.pdf_save_document(pdf, str(path), options)
    if doc.is_repaired:
        raise ValueError("Snapshot serialization required repair")


def write_extract(snapshot: ReviewSnapshot, first: int, last: int, destination: Path) -> None:
    import pymupdf

    # insert_pdf() drops replies, popups and grouped StrikeOuts. Select pages
    # on a disposable instance instead, and write only to the separate output.
    with pymupdf.open(snapshot.path) as subset:
        subset.select(range(first, last + 1))
        annotations = {xref for page in subset for xref, _kind, _name in page.annot_xrefs()}
        for xref in annotations:
            kind, parent = subset.xref_get_key(xref, "IRT")
            if kind == "xref" and int(parent.split()[0]) not in annotations:
                # Keep the note but sever replies to a page outside this extract.
                subset.xref_set_key(xref, "IRT", "null")
                subset.xref_set_key(xref, "RT", "null")
        subset.save(destination, garbage=3, deflate=True, no_new_id=True, raise_on_repair=True)


class ReviewSnapshotStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.snapshot: ReviewSnapshot | None = None
        self.snapshot_stamp: tuple[int, int, int, int] | None = None
        self._extracts: dict[str, Path] = {}

    @staticmethod
    def stamp(path: Path) -> tuple[int, int, int, int]:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def capture(self, doc: "pymupdf.Document", document_id: str, generation: int,
                revision: int, source: FileIdentity) -> dict:
        if self.snapshot is not None:
            raise ReviewError("REVIEW_SNAPSHOT_ACTIVE", "È già disponibile uno snapshot della scheda.")
        snapshot_id = uuid4().hex
        path = self.workspace / f"snapshot-{snapshot_id}.pdf"
        started = perf_counter()
        reserved = False
        try:
            if identify_file(source.canonical_path) != source:
                raise ReviewError("REVIEW_SOURCE_CHANGED", "Il file di origine è cambiato.")
            with path.open("xb"):
                reserved = True
            write_snapshot(doc, path)
            serialized = perf_counter()
            stamp = self.stamp(path)
            digest = fingerprint(path)
            if identify_file(source.canonical_path) != source:
                raise ReviewError("REVIEW_SOURCE_CHANGED", "Il file di origine è cambiato durante la cattura.")
            if self.stamp(path) != stamp:
                raise ReviewError("REVIEW_SNAPSHOT_CHANGED", "Il file dello snapshot è cambiato.")
        except (OSError, RuntimeError, ValueError) as exc:
            if reserved:
                path.unlink(missing_ok=True)
            if isinstance(exc, ReviewError):
                raise
            raise ReviewError("REVIEW_SNAPSHOT_FAILED", "Impossibile acquisire lo snapshot del PDF.") from exc
        self.snapshot = ReviewSnapshot(snapshot_id, document_id, generation, revision,
                                       doc.page_count, digest, stamp[2], str(path))
        self.snapshot_stamp = stamp
        return {"snapshot": asdict(self.snapshot), "timing_ms": {
            "serialize": (serialized - started) * 1000,
            "hash": (perf_counter() - serialized) * 1000,
        }}

    def _require_snapshot(self, snapshot_id: str) -> ReviewSnapshot:
        if self.snapshot is None or self.snapshot.snapshot_id != snapshot_id:
            raise ReviewError("REVIEW_SNAPSHOT_UNKNOWN", "Snapshot non disponibile per questa scheda.")
        return self.snapshot

    def checked_snapshot(self, snapshot_id: str) -> ReviewSnapshot:
        snapshot = self._require_snapshot(snapshot_id)
        try:
            unchanged = self.stamp(Path(snapshot.path)) == self.snapshot_stamp
        except OSError:
            unchanged = False
        if not unchanged:
            raise ReviewError("REVIEW_SNAPSHOT_CHANGED", "Il file dello snapshot è cambiato.")
        return snapshot

    @staticmethod
    def _clean_extracts(extracts: dict[str, Path]) -> None:
        failure = None
        for extract_id, path in tuple(extracts.items()):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                failure = exc
            else:
                del extracts[extract_id]
        if failure is not None:
            raise ReviewError("REVIEW_CLEANUP_FAILED", "Impossibile rilasciare i file di revisione.") from failure

    def release_extract(self, snapshot_id: str, extract_id: str) -> bool:
        self._require_snapshot(snapshot_id)
        if not isinstance(extract_id, str) or not extract_id:
            raise ReviewError("REVIEW_INVALID_REQUEST", "Identità dell'estratto non valida.")
        path = self._extracts.get(extract_id)
        if path is None:
            return False
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ReviewError("REVIEW_CLEANUP_FAILED", "Impossibile rilasciare l'estratto PDF.") from exc
        del self._extracts[extract_id]
        return True

    def release(self, snapshot_id: str) -> None:
        snapshot = self._require_snapshot(snapshot_id)
        self._clean_extracts(self._extracts)
        try:
            Path(snapshot.path).unlink(missing_ok=True)
        except OSError as exc:
            raise ReviewError("REVIEW_CLEANUP_FAILED", "Impossibile rilasciare i file di revisione.") from exc
        self.snapshot = None
        self.snapshot_stamp = None

    def extract(self, snapshot_id: str, first: int, last: int, max_bytes: int,
                *, split: bool = True, max_context_pages: int = 2) -> dict:
        snapshot = self._require_snapshot(snapshot_id)
        try:
            check_interval(snapshot, first, last)
            if last - first + 1 > REVIEW_BATCH_PAGES or type(max_bytes) is not int or not 0 < max_bytes <= REVIEW_PDF_LIMIT:
                raise ValueError("Invalid extract limits")
        except ValueError as exc:
            raise ReviewError("REVIEW_INVALID_BATCH", "Intervallo o limite dell'estratto non valido.") from exc

        path = Path(snapshot.path)
        started = perf_counter()
        pending: dict[str, Path] = {}
        try:
            if self.stamp(path) != self.snapshot_stamp:
                raise ReviewError("REVIEW_SNAPSHOT_CHANGED", "Il file dello snapshot è cambiato.")
            batches = self._build(snapshot, first, last, max_bytes, pending, split=split,
                                  max_context_pages=max_context_pages)
            if self.stamp(path) != self.snapshot_stamp:
                raise ReviewError("REVIEW_SNAPSHOT_CHANGED", "Il file dello snapshot è cambiato.")
            plan = ReviewPlan(snapshot, first, last, batches).to_payload()
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                self._clean_extracts(pending)
            finally:
                # Retain ownership of files the OS refused to remove for cleanup on release.
                self._extracts.update(pending)
            if isinstance(exc, ReviewError):
                raise
            raise ReviewError("REVIEW_EXTRACT_FAILED", "Impossibile preparare l'estratto PDF.") from exc
        self._extracts.update(pending)
        return {"plan": plan,
                "timing_ms": {"extract": (perf_counter() - started) * 1000}}

    def _build(self, snapshot: ReviewSnapshot, first: int, last: int, max_bytes: int,
               pending: dict[str, Path], *, split: bool = True,
               max_context_pages: int = 2) -> tuple[ReviewBatch, ...]:
        extract_id = uuid4().hex
        path = self.workspace / f"extract-{extract_id}.pdf"
        pending[extract_id] = path
        for context in context_variants(snapshot, first, last):
            if len(context) > max_context_pages:
                continue
            batch = make_batch(snapshot, first, last, context)
            write_extract(snapshot, batch.page_map[0].page_index, batch.page_map[-1].page_index, path)
            size = path.stat().st_size
            if size <= max_bytes:
                return (replace(batch, status="ready", size_bytes=size, path=str(path),
                                sha256=fingerprint(path), extract_id=extract_id),)
            path.unlink()
        del pending[extract_id]
        if not split or first == last:
            return (replace(batch, status="too_large", size_bytes=size),)
        chunk = 2 if last - first + 1 > 2 else 1
        return tuple(batch for start in range(first, last + 1, chunk)
                     for batch in self._build(snapshot, start, min(start + chunk - 1, last), max_bytes, pending,
                                              max_context_pages=max_context_pages))
