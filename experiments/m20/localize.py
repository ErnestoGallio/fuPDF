"""Localizzazione offline delle correzioni M20.1 in un PDF originale."""

import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import pymupdf

from pdf_annotator.worker.review_localization import (
    STATUSES, Localization, anchor_for, localize_document, localize_item, page_comment_positions,
)

from .corrections import InvalidCorrections, validate_item, unique_keys


OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "output" / "m20"

def read_corrections(data: bytes) -> list[dict[str, Any]]:
    try:
        root = json.loads(data, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        raise InvalidCorrections("JSON delle correzioni non valido") from None
    if not isinstance(root, dict) or root.keys() != {"corrections"} or not isinstance(root["corrections"], list):
        raise InvalidCorrections("Struttura delle correzioni non valida")
    for index, item in enumerate(root["corrections"]):
        page_index = item.get("page_index") if isinstance(item, dict) else None
        if type(page_index) is not int:
            raise InvalidCorrections(f"Correzione {index}: invalid_page_index")
        try:
            # Validate the proposal independently of whether the requested page exists.
            validate_item(item, page_index, page_index)
        except InvalidCorrections as exc:
            raise InvalidCorrections(f"Correzione {index}: {exc}") from None
    return root["corrections"]


def localize_files(pdf: Path, proposals: Path) -> dict[str, Any]:
    data = proposals.read_bytes()
    corrections = read_corrections(data)
    with pdf.open("rb") as stream:
        before = os.fstat(stream.fileno())
        fingerprint = file_digest(stream, "sha256").hexdigest()
        with pymupdf.open(pdf) as document:
            if not document.is_pdf or document.is_repaired or document.page_count <= 0:
                raise ValueError("PDF non valido")
            if document.needs_pass or document.is_encrypted or (document.metadata or {}).get("encryption"):
                raise ValueError("PDF protetto")
            results = localize_document(document, corrections)
        after = pdf.stat()
        if (not os.path.samestat(before, after) or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            raise ValueError("Il PDF è cambiato durante la localizzazione")
    counts = Counter(result.status for result in results)
    applicable = sum(result.applicable for result in results)
    return {
        "schema_version": 1,
        "document_sha256": fingerprint,
        "corrections_sha256": sha256(data).hexdigest(),
        "coordinate_system": "pymupdf_unrotated_crop_local_points",
        "summary": {"total": len(results), **{key: counts[key] for key in STATUSES},
                    "applicable": applicable, "not_applicable": len(results) - applicable},
        "results": [asdict(result) for result in results],
    }


def write_localization(directory: Path, localization: dict[str, Any]) -> None:
    report = ["# fuPDF M20.2 — Localizzazione", "",
              "Applicabilità indica che la destinazione è utilizzabile. La valutazione editoriale è indipendente.", ""]
    report.extend(f"- {key}: {value}" for key, value in localization["summary"].items())
    report.extend(["", "| Indice | Pagina fisica | Operazione | Esito | Applicabile | Candidati | Diagnostica |",
                   "| ---: | ---: | --- | --- | --- | ---: | --- |"])
    for result in localization["results"]:
        diagnostic = result["reason"] or f"{len(result['quads'])} segmenti"
        if result["context_match_count"] is not None:
            diagnostic += f", {result['context_match_count']} coerenti con il contesto"
        report.append(f"| {result['correction_index']} | {result['page_index']} | {result['operation']} | "
                      f"{result['status']} | {'sì' if result['applicable'] else 'no'} | "
                      f"{result['candidate_count']} | {diagnostic} |")
    (directory / "localization_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (directory / "localization.json").write_text(
        json.dumps(localization, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF originale")
    parser.add_argument("corrections", type=Path, help="corrections.json prodotto da M20.1")
    args = parser.parse_args(argv)
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    try:
        localization = localize_files(args.pdf, args.corrections)
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        directory = Path(mkdtemp(prefix="localization-", dir=OUTPUT_ROOT))
        write_localization(directory, localization)
    except (OSError, RuntimeError):
        print("Impossibile leggere gli input o scrivere i risultati.")
        return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    print(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
