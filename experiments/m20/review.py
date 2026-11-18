"""Review a single PDF page interval with the OpenAI Responses API."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import hashlib
from http.client import HTTPException, HTTPSConnection
import json
import math
import os
from pathlib import Path
import re
from tempfile import mkdtemp
from time import perf_counter
from typing import Any

import pymupdf

from pdf_annotator.services.review_contract.responses import (
    PageInterval, RunFailure, compose_prompt, read_editorial_context,
    request_body as _request_body, response_text,
)

from .corrections import InvalidCorrections, parse_corrections
from .metrics import DEFAULT_MODEL, SOL_RATES, Rates, estimated_cost, get_usage, object_at
from .prompt_profiles import DEFAULT_PROMPT_PROFILE, PROMPT_PROFILES


OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "output" / "m20"
PDF_LIMIT_BYTES = 50_000_000


@dataclass(frozen=True)
class PdfSubset:
    original_bytes: int
    original_sha256: str
    pdf: bytes


def prepare_pdf(path: Path, pages: PageInterval, max_pages: int) -> PdfSubset:
    if pages.count > max_pages:
        raise RunFailure("page_limit_exceeded")
    tools = pymupdf.TOOLS
    show_errors = tools.mupdf_display_errors()
    show_warnings = tools.mupdf_display_warnings()
    tools.mupdf_display_errors(False)
    tools.mupdf_display_warnings(False)
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            with pymupdf.open(path) as source:
                if not source.is_pdf or source.is_repaired:
                    raise RunFailure("invalid_pdf")
                if source.needs_pass or source.is_encrypted or (source.metadata or {}).get("encryption"):
                    raise RunFailure("protected_pdf")
                if not 0 <= pages.first <= pages.last < source.page_count:
                    raise RunFailure("page_interval_out_of_bounds")
                with pymupdf.open() as subset:
                    subset.insert_pdf(source, from_page=pages.first, to_page=pages.last, links=False)
                    pdf = subset.tobytes(garbage=3, deflate=True)
            fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            if (not os.path.samestat(before, after) or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise RunFailure("source_pdf_changed")
    except OSError:
        raise RunFailure("pdf_read_error") from None
    except (RuntimeError, ValueError):
        raise RunFailure("invalid_pdf") from None
    finally:
        tools.reset_mupdf_warnings()
        tools.mupdf_display_errors(show_errors)
        tools.mupdf_display_warnings(show_warnings)
    return PdfSubset(before.st_size, fingerprint, pdf)


def request_body(pdf: bytes, pages: PageInterval, model: str, reasoning: str,
                 max_output_tokens: int, prompt: dict[str, Any]) -> bytes:
    return _request_body(pdf, pages, model, reasoning, max_output_tokens, prompt,
                         pdf_limit=PDF_LIMIT_BYTES)


def create_response(body: bytes, api_key: str, timeout: float) -> tuple[int, dict[str, Any]]:
    # The fixed endpoint and a single POST prevent redirects or automatic replay of the PDF.
    connection = HTTPSConnection("api.openai.com", timeout=timeout)
    http_status = None
    try:
        connection.request("POST", "/v1/responses", body=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        response = connection.getresponse()
        http_status = response.status
        if http_status == 413:
            raise RunFailure("pdf_upload_rejected", http_status)
        if not 200 <= http_status < 300:
            code = "api_timeout" if http_status in (408, 504) else "api_http_error"
            raise RunFailure(code, http_status)
        try:
            result = json.loads(response.read())
        except (ValueError, RecursionError):
            raise RunFailure("invalid_api_json", http_status) from None
        if not isinstance(result, dict):
            raise RunFailure("invalid_api_response", http_status)
        return http_status, result
    except TimeoutError:
        raise RunFailure("api_timeout", http_status) from None
    except (OSError, HTTPException):
        raise RunFailure("api_transport_error", http_status) from None
    finally:
        connection.close()


def page_interval(value: str) -> PageInterval:
    if not re.fullmatch(r"[0-9]+-[0-9]+", value):
        raise argparse.ArgumentTypeError("usa INIZIO-FINE con indici fisici da zero")
    first, last = map(int, value.split("-"))
    if last < first:
        raise argparse.ArgumentTypeError("l'indice finale precede quello iniziale")
    return PageInterval(first, last)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("specifica un intero positivo")
    return number


def positive_timeout(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("specifica un timeout positivo e finito")
    return seconds


def rate(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except DecimalException:
        raise argparse.ArgumentTypeError("tariffa non valida") from None
    if not number.is_finite() or number < 0:
        raise argparse.ArgumentTypeError("specifica una tariffa finita e non negativa")
    return number


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pages", required=True, type=page_interval, metavar="INIZIO-FINE",
                        help="intervallo fisico originale inclusivo, da zero")
    parser.add_argument("--max-pages", type=positive_int, default=4)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt-profile", choices=tuple(PROMPT_PROFILES), default=DEFAULT_PROMPT_PROFILE,
                        help=f"profilo editoriale (predefinito: {DEFAULT_PROMPT_PROFILE})")
    parser.add_argument("--context-file", type=Path, help="file JSON UTF-8 con il contesto editoriale facoltativo")
    parser.add_argument("--reasoning", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
                        default="medium")
    parser.add_argument("--timeout", type=positive_timeout, default=180.0, help="secondi di timeout socket")
    parser.add_argument("--max-output-tokens", type=positive_int, default=8192)
    parser.add_argument("--rates", type=rate, nargs=4, metavar=("INPUT", "CACHED", "WRITE", "OUTPUT"),
                        help="USD per milione di token, nell'ordine indicato")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="prepara la richiesta in locale (predefinito)")
    mode.add_argument("--execute", action="store_true", help="autorizza un invio a OpenAI, a pagamento")
    args = parser.parse_args(argv)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key and api_key in args.model:
        parser.error("--model contiene la chiave API. Specifica l'ID del modello.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]*", args.model):
        parser.error("ID modello non valido")
    if args.max_output_tokens < 16:
        parser.error("--max-output-tokens deve essere almeno 16")
    if args.model == DEFAULT_MODEL and args.max_output_tokens > 128_000:
        parser.error("gpt-6.1-sol supporta al massimo 128000 token di output")
    if args.model == DEFAULT_MODEL and args.reasoning in ("none", "minimal"):
        parser.error("gpt-6.1-sol richiede un reasoning tra low, medium, high, xhigh e max")
    return args


def display(value: object) -> str:
    return "n/d" if value is None else str(value)


def write_results(directory: Path, metrics: dict[str, Any], corrections: dict[str, Any] | None) -> None:
    def write_json(name: str, value: dict[str, Any]) -> None:
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if corrections is not None:
        write_json("corrections.json", corrections)
    report = [
        "# fuPDF M20.1 — Revisione PDF",
        "",
        f"- Esito `{metrics['status']}`. Diagnostica `{display(metrics['error'])}`, "
        f"HTTP {display(metrics['http_status'])}, motivo incompleto `{display(metrics['incomplete_reason'])}`.",
        f"- Modello richiesto `{metrics['model_requested']}`, modello riportato "
        f"`{display(metrics['model_returned'])}`, reasoning `{metrics['reasoning']}`.",
        f"- Profilo editoriale `{metrics['prompt_profile']}`.",
        f"- Intervallo originale `{metrics['pages']['first']}-{metrics['pages']['last']}` (da zero), "
        f"{metrics['pages_selected']} pagine selezionate, {display(metrics['pages_analyzed'])} in un esito completato.",
        f"- PDF originale {display(metrics['original_bytes'])} byte, estratto {display(metrics['subset_bytes'])} byte.",
        f"- Tentativi API {metrics['api_requests']}, retry {metrics['retries']}, "
        f"durata totale {metrics['elapsed_seconds']} s, durata API {display(metrics['api_seconds'])} s.",
        "- Token API " + ", ".join(f"{key} {display(value)}" for key, value in metrics["usage"].items()) + ".",
        f"- Correzioni {display(metrics['correction_count'])}.",
        f"- Stima Standard USD {display(metrics['estimated_cost_usd'])}. La fatturazione effettiva può differire.",
        "",
        "I dettagli tecnici e le tariffe applicate sono in `run_metrics.json`.",
    ]
    (directory / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    # Readers check the metrics last, after the other artifacts have been written.
    write_json("run_metrics.json", metrics)


def run(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    started = perf_counter()
    rates = SOL_RATES if args.model == DEFAULT_MODEL else None
    if args.rates:
        rates = Rates(*args.rates)
    pages = args.pages
    metrics: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "mode": "execute" if args.execute else "dry_run",
        "model_requested": args.model,
        "model_returned": None,
        "reasoning": args.reasoning,
        "max_output_tokens": args.max_output_tokens,
        "request_timeout_seconds": args.timeout,
        "page_limit": args.max_pages,
        "pages": asdict(pages),
        "pages_selected": pages.count,
        "pages_analyzed": None,
        "original_sha256": None,
        "original_bytes": None,
        "subset_bytes": None,
        "prompt_profile": args.prompt_profile,
        "prompt_sha256": None,
        "editorial_context": None,
        "effective_prompt_sha256": None,
        "api_requests": 0,
        "retries": 0,
        "api_seconds": None,
        "http_status": None,
        "api_status": None,
        "incomplete_reason": None,
        "error": None,
        "usage": get_usage({}),
        "pricing_tier": "standard",
        "rates_usd_per_million": {key: str(value) for key, value in asdict(rates).items()} if rates else None,
        "estimated_cost_usd": None,
        "correction_count": None,
    }
    corrections = None
    try:
        context = read_editorial_context(args.context_file)
        metrics["editorial_context"] = context
        subset = prepare_pdf(args.pdf, pages, args.max_pages)
        metrics.update(original_sha256=subset.original_sha256, original_bytes=subset.original_bytes,
                       subset_bytes=len(subset.pdf))
        try:
            prompt = PROMPT_PROFILES[args.prompt_profile].path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise RunFailure("prompt_read_error") from None
        metrics["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        effective_prompt = compose_prompt(prompt, pages, context)
        body = request_body(subset.pdf, pages, args.model, args.reasoning, args.max_output_tokens, effective_prompt)
        prompt_json = json.dumps(effective_prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        metrics["effective_prompt_sha256"] = hashlib.sha256(prompt_json.encode("utf-8")).hexdigest()
        if args.execute:
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RunFailure("missing_openai_api_key")
            if not re.fullmatch(r"[!-~]+", api_key):
                raise RunFailure("invalid_openai_api_key")
            api_started = perf_counter()
            metrics["api_requests"] = 1
            try:
                metrics["http_status"], response = create_response(body, api_key, args.timeout)
            finally:
                metrics["api_seconds"] = round(perf_counter() - api_started, 3)
            model = response.get("model")
            if isinstance(model, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]*", model) and api_key not in model:
                metrics["model_returned"] = model
            status = response.get("status")
            if status in ("completed", "failed", "incomplete", "cancelled", "queued", "in_progress"):
                metrics["api_status"] = status
            reason = object_at(response, "incomplete_details").get("reason")
            if reason in ("max_output_tokens", "content_filter"):
                metrics["incomplete_reason"] = reason
            metrics["usage"] = get_usage(response)
            try:
                cost = estimated_cost(metrics["usage"], rates, args.model)
            except DecimalException:
                cost = None
            metrics["estimated_cost_usd"] = str(cost) if cost is not None else None
            corrections = parse_corrections(response_text(response), pages.first, pages.last)
            metrics.update(status="completed", pages_analyzed=pages.count,
                           correction_count=len(corrections["corrections"]))
        else:
            metrics["status"] = "dry_run"
    except RunFailure as error:
        metrics["error"] = str(error)
        if error.http_status is not None:
            metrics["http_status"] = error.http_status
    except InvalidCorrections as error:
        metrics["error"] = str(error)
    except KeyboardInterrupt:
        metrics["error"] = "interrupted"
    metrics["elapsed_seconds"] = round(perf_counter() - started, 3)
    write_results(directory, metrics, corrections)
    return metrics


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        directory = Path(mkdtemp(prefix="run-", dir=OUTPUT_ROOT))
        metrics = run(args, directory)
    except OSError:
        print("Impossibile salvare gli output dell'esecuzione.")
        return 1
    error = metrics["error"]
    summary = f"{metrics['status']} ({error})" if error else metrics["status"]
    print(f"{summary} — {directory}")
    return int(error is not None)


if __name__ == "__main__":
    raise SystemExit(main())
