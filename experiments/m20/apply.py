"""Apply a saved M20.2 plan through the production fuPDF worker and Save As."""

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import multiprocessing as mp
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Iterator

from pdf_annotator.domain.identifiers import new_command_id
from pdf_annotator.domain.serialization import result_from_payload, target_to_payload
from pdf_annotator.util.paths import identify_file, identify_save_target
from pdf_annotator.worker.document_service import DocumentSource
from pdf_annotator.worker.protocol import Operation, Request, Response, decode, encode, frame_limit

from .application_verify import VerificationFailure, verify_saved
from .application_worker import ApplicationPlan, application_worker, source_matches
from .corrections import unique_keys
from .localize import OUTPUT_ROOT, STATUSES, read_corrections


class BatchFailure(RuntimeError):
    pass


def read_plan(proposal_file: Path, localization_file: Path) -> ApplicationPlan:
    data = proposal_file.read_bytes()
    proposals = read_corrections(data)
    proposal_hash = sha256(data).hexdigest()
    localization_data = localization_file.read_bytes()
    manifest = json.loads(localization_data, object_pairs_hook=unique_keys)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("coordinate_system") != "pymupdf_unrotated_crop_local_points"
            or manifest.get("corrections_sha256") != proposal_hash
            or not isinstance(manifest.get("document_sha256"), str)
            or not isinstance(manifest.get("results"), list) or len(manifest["results"]) != len(proposals)
            or not all(isinstance(row, dict) for row in manifest["results"])):
        raise BatchFailure("localization_manifest_mismatch")
    return ApplicationPlan(manifest["document_sha256"], proposal_hash, sha256(localization_data).hexdigest(),
                           proposals, manifest["results"])


class WorkerClient:
    """Synchronous CLI transport. Only the PDF owner uses PyMuPDF during application."""

    def __init__(self, conn, document_id: str, source: DocumentSource) -> None:
        self.conn = conn
        self.document_id = document_id
        self.source = source
        self.generation = 1
        self.next_request = 0

    def request(self, operation: Operation, payload: dict[str, Any] | None = None) -> Response:
        self.next_request += 1
        request = Request(self.document_id, self.generation, self.next_request, operation, payload or {})
        limit = self.source.raster_limit
        try:
            self.conn.send_bytes(encode(request, limit))
            if not self.conn.poll(150):
                raise BatchFailure("worker_timeout")
            response = decode(self.conn.recv_bytes(frame_limit(limit)), limit)
        except (EOFError, OSError, ValueError) as exc:
            raise BatchFailure("worker_transport_failure") from exc
        if (not isinstance(response, Response) or response.document_id != request.document_id
                or response.document_generation != request.document_generation
                or response.request_id != request.request_id or response.operation != operation):
            raise BatchFailure("worker_response_identity_mismatch")
        if operation == Operation.SAVE and response.payload.get("reopened"):
            if response.payload.get("new_generation") != self.generation + 1:
                raise BatchFailure("save_generation_mismatch")
            self.generation += 1
        return response


@contextmanager
def worker_session(source: DocumentSource, plan: ApplicationPlan) -> Iterator[WorkerClient]:
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    document_id = new_command_id()
    process = ctx.Process(target=application_worker, args=(child, document_id, source, plan),
                          name="fupdf-m20-document-worker")
    try:
        process.start()
        child.close()
        client = WorkerClient(parent, document_id, source)
        response = client.request(Operation.OPEN_DOCUMENT)
        if not response.success:
            raise BatchFailure(response.error.code)
        yield client
        closed = client.request(Operation.CLOSE)
        if not closed.success:
            raise BatchFailure(closed.error.code)
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(3)
            process.close()


NOT_APPLICABLE = {"stale_text_target", "invalid_geometry", "page_out_of_range",
                  "text_page_too_complex", "invalid_command", "duplicate_command"}
FATAL = {"stale_generation", "SOURCE_CHANGED", "WORKER_FAILED"}


def proposal_row(index: int, plan: ApplicationPlan) -> dict[str, Any]:
    proposal = plan.proposals[index]
    status = plan.localizations[index].get("status")
    return {"proposal_index": index, "operation": proposal["operation"], "page_index": proposal["page_index"],
            "localization": status if status in STATUSES else None}


def run_application(pdf: Path, plan: ApplicationPlan, directory: Path) -> dict[str, Any]:
    output = directory / "revision.pdf"
    report: dict[str, Any] = {"document_sha256": plan.document_sha256, "corrections_sha256": plan.corrections_sha256,
                              "localization_sha256": plan.localization_sha256, "output_pdf": None,
                              "error": None, "verification": None, "results": []}
    rows = report["results"]
    revision = 0
    try:
        identity = identify_file(str(pdf))
        if not source_matches(identity, plan.document_sha256):
            raise BatchFailure("source_hash_mismatch")
        destination = identify_save_target(str(output))
        if destination.existing is not None:
            raise BatchFailure("output_already_exists")
        with worker_session(DocumentSource(identity), plan) as worker:
            for index, proposal in enumerate(plan.proposals):
                supplied = plan.localizations[index]
                row = proposal_row(index, plan)
                rows.append(row)
                if supplied.get("applicable") is not True:
                    reason = supplied.get("reason")
                    if not isinstance(reason, str) or not reason.isascii() or not reason.isidentifier() or len(reason) > 64:
                        reason = "not_localized"
                    row.update(status="not_applied", reason=reason)
                    continue
                response = worker.request(Operation.ANNOTATION_COMMAND, {"proposal_index": index})
                result = result_from_payload(response.payload) if response.success else None
                if result is not None and (result.document_id != worker.document_id
                        or result.document_generation != worker.generation or result.revision_before != revision):
                    raise BatchFailure("mutation_result_identity_mismatch")
                if result is None or not result.success:
                    reason = result.error.code.value if result is not None else response.error.code
                    row.update(status="not_applied" if reason in NOT_APPLICABLE else "error", reason=reason)
                    if reason in FATAL:
                        raise BatchFailure(reason)
                    continue
                model = result.annotation
                if model is None or model.backing is None or model.page_index != proposal["page_index"]:
                    raise BatchFailure("missing_native_mutation_acknowledgement")
                revision = result.document_revision
                row.update(status="applied", annotation_id=model.annotation_id.value,
                           pdf_nm=model.backing.pdf_nm, root_xref=model.backing.xref,
                           associated_xrefs=list(model.backing.associated_xrefs),
                           revision=revision, target=target_to_payload(model.target))
            if revision:
                saved = worker.request(Operation.SAVE, {"target": asdict(destination),
                    "revision": revision, "overwrite": False})
                if not saved.success:
                    report["save_diagnostics"] = {key: saved.payload[key] for key in
                        ("timing_ms", "recovery_paths", "cleanup_paths") if key in saved.payload}
                    raise BatchFailure(saved.error.code)
                if saved.payload.get("saved_revision") != revision:
                    raise BatchFailure("save_revision_mismatch")
                report["output_pdf"] = str(output.resolve())
        if revision:
            if not source_matches(identity, plan.document_sha256):
                raise BatchFailure("source_changed_during_application")
            report["verification"] = verify_saved(pdf, output, rows, plan.proposals)
    except (BatchFailure, VerificationFailure) as exc:
        report["error"] = str(exc)
    except (RuntimeError, OSError, ValueError) as exc:
        report["error"] = f"verification_or_io_failed:{type(exc).__name__}"
    for row in rows:
        if "status" not in row:
            row.update(status="error", reason=report["error"])
    for index in range(len(rows), len(plan.proposals)):
        rows.append({**proposal_row(index, plan), "status": "not_applied", "reason": "batch_aborted"})
    report["summary"] = {status: sum(row["status"] == status for row in rows)
                         for status in ("applied", "not_applied", "error")}
    return report


def write_report(directory: Path, report: dict[str, Any]) -> None:
    (directory / "application.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# fuPDF M20.3 — Applicazione nativa", "",
             f"- PDF salvato: {Path(report['output_pdf']).name if report['output_pdf'] else 'assente'}.",
             f"- Verifica strutturale: {'PASS' if report['verification'] else 'non completata'}.",
             f"- Errore del lotto: {report['error'] or 'nessuno'}.",
             "- Adobe Acrobat e InDesign: NOT RUN.", "",
             "| Proposta | Pagina fisica | Operazione | Esito worker | Identità PDF `/NM` o motivo |",
             "| ---: | ---: | --- | --- | --- |"]
    for row in report["results"]:
        lines.append(f"| {row['proposal_index']} | {row['page_index']} | {row['operation']} | "
                     f"{row['status']} | `{row.get('pdf_nm') or row.get('reason')}` |")
    (directory / "application_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("corrections", type=Path)
    parser.add_argument("localization", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = read_plan(args.corrections, args.localization)
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        directory = Path(mkdtemp(prefix="application-", dir=OUTPUT_ROOT))
        report = run_application(args.pdf, plan, directory)
        write_report(directory, report)
    except (OSError, ValueError, BatchFailure):
        print("Input o output M20.3 non valido.")
        return 1
    print(directory)
    return int(report["error"] is not None or report["summary"]["error"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
