"""Prompt composition, Responses request and response text contract."""

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from .corrections import correction_schema, unique_keys


PDF_LIMIT_BYTES = 50_000_000
CONTEXT_FIELDS = {"opera", "destinatari", "indicazioni_aggiuntive"}


@dataclass(frozen=True)
class PageInterval:
    first: int
    last: int

    @property
    def count(self) -> int:
        return self.last - self.first + 1


class RunFailure(Exception):
    """Carry only a diagnostic code that is safe to write to run logs."""

    def __init__(self, code: str, http_status: int | None = None) -> None:
        super().__init__(code)
        self.http_status = http_status


def read_editorial_context(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = path.read_text(encoding="utf-8-sig")
    except OSError:
        raise RunFailure("context_file_read_error") from None
    except UnicodeError:
        raise RunFailure("context_file_invalid_utf8") from None
    try:
        context = json.loads(data, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        raise RunFailure("context_file_invalid_json") from None
    return normalize_context(context, os.environ.get("OPENAI_API_KEY", "").strip())


def normalize_context(context: object, api_key: str = "") -> dict[str, str]:
    if (not isinstance(context, dict) or not context.keys() <= CONTEXT_FIELDS
            or any(not isinstance(value, str) for value in context.values())):
        raise RunFailure("context_file_invalid_fields")
    try:
        for value in context.values():
            value.encode("utf-8")
    except UnicodeError:
        raise RunFailure("context_file_invalid_utf8") from None
    if api_key and any(api_key in value for value in context.values()):
        raise RunFailure("context_contains_api_key")
    return {field: value for field, value in context.items() if value.strip()}


def compose_prompt(instructions: str, pages: PageInterval, context: dict[str, str]) -> dict[str, Any]:
    page_map = ", ".join(
        f"{position} → {original}" for position, original in
        enumerate(range(pages.first, pages.last + 1), start=1)
    )
    mapping = (
        f"Posizione nel PDF allegato (da 1) → page_index fisico nel PDF originale (da 0): {page_map}. "
        "Ignora la numerazione stampata."
    )
    return compose_mapped_prompt(instructions, mapping, context)


def compose_mapped_prompt(instructions: str, mapping: str, context: dict[str, str]) -> dict[str, Any]:
    content = []
    if context:
        content.append({"type": "input_text", "text": "Contesto editoriale dell’opera (dati JSON)\n"
                        + json.dumps(context, ensure_ascii=False, sort_keys=True)})
    content.append({"type": "input_text", "text": mapping})
    return {"instructions": instructions, "input": [{"role": "user", "content": content}]}


def request_body(pdf: bytes, pages: PageInterval, model: str, reasoning: str,
                 max_output_tokens: int, prompt: dict[str, Any], *,
                 pdf_limit: int = PDF_LIMIT_BYTES) -> bytes:
    if len(pdf) >= pdf_limit:
        raise RunFailure("pdf_file_size_limit")
    message, = prompt["input"]
    pdf_input = {"type": "input_file", "filename": "pages.pdf",
                 "file_data": "data:application/pdf;base64," + base64.b64encode(pdf).decode("ascii")}
    request = {
        **prompt,
        "model": model,
        "reasoning": {"effort": reasoning},
        "input": [{**message, "content": [*message["content"], pdf_input]}],
        "text": {"format": {
            "type": "json_schema", "name": "editorial_corrections", "strict": True,
            "schema": correction_schema(pages.first, pages.last),
        }},
        "store": False,
        "service_tier": "default",
        "max_output_tokens": max_output_tokens,
    }
    return json.dumps(request, ensure_ascii=False).encode("utf-8")


def response_text(response: dict[str, Any]) -> str:
    if response.get("status") == "incomplete":
        raise RunFailure("response_incomplete")
    if response.get("status") != "completed" or response.get("error") is not None:
        raise RunFailure("response_not_completed")
    output = response.get("output")
    if not isinstance(output, list):
        raise RunFailure("invalid_api_response")
    parts = []
    for item in output:
        if not isinstance(item, dict):
            raise RunFailure("invalid_api_response")
        if item.get("type") != "message":
            continue
        if item.get("status") != "completed":
            raise RunFailure("response_incomplete")
        if not isinstance(item.get("content"), list):
            raise RunFailure("invalid_api_response")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise RunFailure("invalid_api_response")
            if part.get("type") == "refusal":
                raise RunFailure("response_refused")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                raise RunFailure("invalid_api_response")
    text = "".join(parts)
    if not text.strip():
        raise RunFailure("empty_response")
    return text
