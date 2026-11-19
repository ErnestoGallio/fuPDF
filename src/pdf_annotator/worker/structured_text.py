"""Native page text extraction on the existing PDF owner's main thread."""

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..domain.structured_text import (ExtractionIdentity, PageText, MAX_PAGE_JSON_BYTES,
                                      canonical_json, integer, normalize_page_text)
from .review_localization_text import EXTRACTION_FLAGS, base_textpage
from .review_snapshot import ReviewError
from .text_service import MAX_PATHOLOGICAL_PAGE_CHARACTERS, WorkerTextService


def extract_page(document, page_index: int) -> PageText:
    integer(page_index)
    if page_index >= document.page_count:
        raise ValueError("Page outside document")
    page = document.load_page(page_index)
    textpage = base_textpage(page, EXTRACTION_FLAGS)
    # base_textpage is a content-only device result; bind it for Page.get_text.
    textpage.parent = page
    text = page.get_text("text", textpage=textpage, sort=False)
    if len(text) > MAX_PATHOLOGICAL_PAGE_CHARACTERS:
        raise ReviewError("TEXT_PAGE_TOO_LARGE", "Pagina troppo grande per l’esportazione testuale.")
    return PageText(page_index + 1, normalize_page_text(text))


def export_page(service: WorkerTextService, revision: int, payload: dict) -> dict:
    if set(payload) != {"workspace", "page_index", "revision"} or type(payload["revision"]) is not int:
        raise ReviewError("TEXT_INVALID_REQUEST", "Richiesta di estrazione non valida.")
    if payload["revision"] != revision:
        raise ReviewError("TEXT_STALE", "Documento modificato durante l’estrazione. Ripeti l’esportazione.")
    try:
        identity = ExtractionIdentity(service.document_id, service.generation, revision)
        page = extract_page(service.document, payload["page_index"])
        data = canonical_json(page.to_payload()).encode("utf-8")
        if len(data) > MAX_PAGE_JSON_BYTES:
            raise ReviewError("TEXT_PAGE_TOO_LARGE", "Pagina troppo grande per l’esportazione testuale.")
        path = Path(payload["workspace"]) / f"structured-page-{uuid4().hex}.json"
        path.write_bytes(data)
        return {"path": str(path), "size_bytes": len(data), "sha256": sha256(data).hexdigest(),
                "page_number": page.page, "identity": asdict(identity)}
    except (ValueError, OSError, RuntimeError) as exc:
        raise ReviewError("TEXT_EXTRACTION_FAILED", "Estrazione del testo non riuscita.") from exc
