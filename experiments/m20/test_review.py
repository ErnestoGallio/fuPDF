"""Offline acceptance tests for M20.1 using a synthetic PDF and fake HTTPS."""

import base64
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pymupdf
import pytest

from experiments.m20 import review
from experiments.m20.corrections import InvalidCorrections, correction_schema, parse_corrections
from experiments.m20.metrics import DEFAULT_MODEL, SOL_RATES, Rates, estimated_cost, get_usage
from experiments.m20.prompt_profiles import PROMPT_PROFILES, PromptProfile


EXAMPLE = Path(__file__).with_name("example_corrections.json")
FAKE_KEY = "sk-m20-synthetic-private-key"
PRIVATE_TEXT = "SYNTHETIC PRIVATE DOCUMENT CONTENT"
PAGES = review.PageInterval(12, 13)
USAGE = {
    "input_tokens": 3000,
    "input_tokens_details": {"cached_tokens": 500, "cache_write_tokens": 1000},
    "output_tokens": 500,
    "output_tokens_details": {"reasoning_tokens": 250},
    "total_tokens": 3500,
}


@pytest.fixture(autouse=True)
def block_real_https(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("HTTPS must be mocked in M20.1 tests")

    monkeypatch.setattr(review, "HTTPSConnection", blocked)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "riservato-fixture.pdf"
    with pymupdf.open() as document:
        for index in range(15):
            page = document.new_page(width=300 + index, height=240)
            page.insert_text((30, 30), f"{100 + index} {PRIVATE_TEXT}", fontsize=9)
            page.draw_rect((25, 45, 150, 70), fill=(1, 0.9, 0.5))
            if index == 12:
                page.insert_text((30, 60), "Vediamo ora qual'è la soluzione corretta.")
                page.insert_text((30, 90), "L'esempio è molto molto chiaro.")
            if index == 13:
                page.insert_text((30, 60), "La relazione chiara.")
                page.set_cropbox(pymupdf.Rect(10, 10, 280, 220))
                page.set_rotation(90)
        document.save(path)
    return path


@pytest.fixture
def output_root(monkeypatch, tmp_path):
    root = tmp_path / "runs"
    monkeypatch.setattr(review, "OUTPUT_ROOT", root)
    return root


def run_cli(path, output_root, *options):
    exit_code = review.main([str(path), "--pages", "12-13", *options])
    directory, = output_root.iterdir()
    return exit_code, directory, json.loads((directory / "run_metrics.json").read_text(encoding="utf-8"))


def completed(text):
    return {
        "status": "completed", "model": DEFAULT_MODEL, "usage": USAGE,
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": text}]},
        ],
    }


def mock_https(monkeypatch, body=None, *, status=200, error=None):
    response = Mock(status=status)
    response.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    connection.getresponse.side_effect = error
    factory = Mock(return_value=connection)
    monkeypatch.setattr(review, "HTTPSConnection", factory)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    return factory, connection, response


def test_subset_preserves_page_order_text_and_appearance(source):
    original_bytes = source.read_bytes()
    subset = review.prepare_pdf(source, PAGES, 4)
    with pymupdf.open(source) as original, pymupdf.open(stream=subset.pdf) as extracted:
        assert extracted.page_count == 2
        for relative, physical in enumerate((12, 13)):
            assert extracted[relative].get_text() == original[physical].get_text()
            assert extracted[relative].cropbox == original[physical].cropbox
            assert extracted[relative].rotation == original[physical].rotation
            assert extracted[relative].get_pixmap().samples == original[physical].get_pixmap().samples
    assert source.read_bytes() == original_bytes
    assert subset.original_sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert subset.original_bytes == len(original_bytes)


@pytest.mark.parametrize(("pages", "limit", "error"), [
    (review.PageInterval(0, 4), 4, "page_limit_exceeded"),
    (review.PageInterval(14, 15), 4, "page_interval_out_of_bounds"),
])
def test_page_limits(source, pages, limit, error):
    with pytest.raises(review.RunFailure, match=error):
        review.prepare_pdf(source, pages, limit)


def test_schema_strict_and_uses_physical_indices():
    schema = correction_schema(12, 13)
    item = schema["properties"]["corrections"]["items"]
    anchor = item["properties"]["insert_anchor"]["anyOf"][0]
    for obj in (schema, item, anchor):
        assert obj["type"] == "object"
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert item["properties"]["page_index"] == {"type": "integer", "minimum": 12, "maximum": 13}


def test_example_all_operations_and_nullable_fields():
    result = parse_corrections(EXAMPLE.read_text(encoding="utf-8"), 12, 13)
    replace, delete, insert, comment = result["corrections"]
    assert [item["operation"] for item in result["corrections"]] == ["replace", "delete", "insert", "comment"]
    assert replace["replacement_text"] == "qual è"
    assert delete["replacement_text"] is None
    assert insert["original_text"] is None and insert["replacement_text"] == "è "
    assert insert["insert_anchor"] == {"text": "chiara", "side": "before"}
    assert comment["original_text"] is comment["context_before"] is comment["context_after"] is None


@pytest.mark.parametrize("changes", [
    {"page_index": 0}, {"page_index": True}, {"operation": "unknown"}, {"page_index": 12.0},
    {"category": "unknown"}, {"original_text": None}, {"replacement_text": 7},
    {"operation": "delete"}, {"operation": "insert", "original_text": None},
    {"coordinates": [10, 20]}, {"category": "verification_needed"}, {"reason": "  "},
    {"original_text": "\ud800"},
    {"operation": "insert", "original_text": None, "insert_anchor": {"text": "x", "side": "beside"}},
])
def test_invalid_correction_is_rejected(changes):
    item = json.loads(EXAMPLE.read_text(encoding="utf-8"))["corrections"][0]
    item.update(changes)
    with pytest.raises(InvalidCorrections):
        parse_corrections(json.dumps({"corrections": [item]}), 12, 13)


def test_missing_required_field_and_duplicate_keys_rejected():
    item = json.loads(EXAMPLE.read_text(encoding="utf-8"))["corrections"][0]
    del item["context_before"]
    with pytest.raises(InvalidCorrections):
        parse_corrections(json.dumps({"corrections": [item]}), 12, 13)
    with pytest.raises(InvalidCorrections):
        parse_corrections('{"corrections":[1],"corrections":[]}', 12, 13)


def test_dry_run_serializes_request_without_network_or_key(monkeypatch, source, output_root):
    build = Mock(wraps=review.request_body)
    monkeypatch.setattr(review, "request_body", build)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code, directory, metrics = run_cli(source, output_root)
    assert code == 0
    assert metrics["status"] == "dry_run"
    assert metrics["api_requests"] == metrics["retries"] == 0
    assert metrics["pages_selected"] == 2 and metrics["pages_analyzed"] is None
    assert metrics["subset_bytes"] > 0
    assert set(p.name for p in directory.iterdir()) == {"run_metrics.json", "report.md"}
    build.assert_called_once()
    assert build.call_args.args[1:5] == (PAGES, "gpt-6.1-sol", "medium", 8192)


def test_mock_call_sends_pdf_and_records_usage(monkeypatch, source, output_root):
    text = EXAMPLE.read_text(encoding="utf-8")
    factory, connection, _ = mock_https(monkeypatch, completed(text))
    code, directory, metrics = run_cli(source, output_root, "--execute", "--timeout", "30")
    assert code == 0
    factory.assert_called_once_with("api.openai.com", timeout=30.0)
    connection.request.assert_called_once()
    assert connection.request.call_args.args == ("POST", "/v1/responses")
    request = json.loads(connection.request.call_args.kwargs["body"])
    assert connection.request.call_args.kwargs["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request["model"] == DEFAULT_MODEL and request["reasoning"] == {"effort": "medium"}
    assert request["store"] is False and request["service_tier"] == "default"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"] == correction_schema(12, 13)
    mapping, file_input = request["input"][0]["content"]
    assert "1 → 12, 2 → 13" in mapping["text"]
    assert file_input["type"] == "input_file" and file_input["filename"] == "pages.pdf"
    assert file_input["file_data"].startswith("data:application/pdf;base64,")
    pdf = base64.b64decode(file_input["file_data"].split(",", 1)[1], validate=True)
    with pymupdf.open(stream=pdf) as uploaded:
        assert uploaded.page_count == 2
        assert uploaded[0].get_text().startswith("112 ") and uploaded[1].get_text().startswith("113 ")
    connection.close.assert_called_once()
    assert metrics["status"] == "completed" and metrics["correction_count"] == 4
    assert metrics["api_requests"] == 1 and metrics["retries"] == 0
    assert metrics["pages_analyzed"] == 2 and metrics["subset_bytes"] == len(pdf)
    assert metrics["model_returned"] == DEFAULT_MODEL and metrics["http_status"] == 200
    assert metrics["usage"] == {
        "input_tokens": 3000, "output_tokens": 500, "reasoning_tokens": 250,
        "cached_input_tokens": 500, "cache_write_tokens": 1000, "total_tokens": 3500,
    }
    assert Decimal(metrics["estimated_cost_usd"]) == Decimal("0.01055")
    assert json.loads((directory / "corrections.json").read_text(encoding="utf-8")) == json.loads(text)


def test_valid_empty_corrections(monkeypatch, source, output_root):
    mock_https(monkeypatch, completed('{"corrections": []}'))
    code, directory, metrics = run_cli(source, output_root, "--execute")
    assert code == 0 and metrics["status"] == "completed" and metrics["correction_count"] == 0
    assert json.loads((directory / "corrections.json").read_text(encoding="utf-8")) == {"corrections": []}


def test_model_reasoning_and_rates_can_be_overridden(monkeypatch, source, output_root):
    payload = {**completed('{"corrections": []}'), "model": "custom-vision-model"}
    _, connection, _ = mock_https(monkeypatch, payload)
    code, _, metrics = run_cli(source, output_root, "--execute", "--model", "custom-vision-model",
                               "--reasoning", "high", "--rates", "1", "0.25", "1.5", "4")
    assert code == 0 and metrics["model_returned"] == "custom-vision-model"
    request = json.loads(connection.request.call_args.kwargs["body"])
    assert request["model"] == "custom-vision-model" and request["reasoning"] == {"effort": "high"}
    assert Decimal(metrics["estimated_cost_usd"]) == Decimal("0.005125")


@pytest.mark.parametrize(("payload", "error"), [
    ({**completed('{"corrections": []}'), "status": "incomplete",
      "incomplete_details": {"reason": "max_output_tokens"}}, "response_incomplete"),
    ({**completed('{"corrections": []}'), "status": "failed", "error": {"message": PRIVATE_TEXT}},
     "response_not_completed"),
    ({**completed(""), "output": [{"type": "message", "status": "incomplete"}]}, "response_incomplete"),
    ({**completed(""), "output": [{"type": "message", "status": "completed",
      "content": [{"type": "refusal", "refusal": PRIVATE_TEXT}]}]}, "response_refused"),
    (completed(""), "empty_response"),
    (completed("{" + PRIVATE_TEXT), "invalid_corrections_json"),
    (b"invalid JSON", "invalid_api_json"),
])
def test_incomplete_refused_invalid_or_blank_api_output(monkeypatch, source, output_root, payload, error):
    _, connection, _ = mock_https(monkeypatch, payload)
    code, directory, metrics = run_cli(source, output_root, "--execute")
    assert code == 1 and metrics["status"] == "failed" and metrics["error"] == error
    assert metrics["pages_analyzed"] is None and metrics["correction_count"] is None
    assert not (directory / "corrections.json").exists()
    assert metrics["api_requests"] == 1 and metrics["retries"] == 0
    connection.request.assert_called_once()
    if isinstance(payload, dict):
        assert metrics["usage"]["output_tokens"] == 500
    if isinstance(payload, dict) and payload.get("incomplete_details"):
        assert metrics["incomplete_reason"] == "max_output_tokens"
    for artifact in directory.iterdir():
        assert PRIVATE_TEXT not in artifact.read_text(encoding="utf-8")


@pytest.mark.parametrize(("status", "exc", "error"), [
    (413, None, "pdf_upload_rejected"), (429, None, "api_http_error"), (307, None, "api_http_error"),
    (504, None, "api_timeout"), (None, TimeoutError(FAKE_KEY), "api_timeout"),
    (None, OSError(PRIVATE_TEXT), "api_transport_error"),
])
def test_api_errors_never_retry_or_log_payload(monkeypatch, source, output_root, capsys, status, exc, error):
    _, connection, response = mock_https(monkeypatch, {
        "error": {"message": f"{FAKE_KEY} {PRIVATE_TEXT} {source}"},
    }, status=status, error=exc)
    code, directory, metrics = run_cli(source, output_root, "--execute")
    assert code == 1 and metrics["error"] == error and metrics["http_status"] == status
    assert metrics["api_requests"] == 1 and metrics["retries"] == 0
    connection.request.assert_called_once()
    response.read.assert_not_called()
    captured = capsys.readouterr()
    logs = captured.out + captured.err + "".join(p.read_text(encoding="utf-8") for p in directory.iterdir())
    for secret in (FAKE_KEY, PRIVATE_TEXT, source.name, "data:application/pdf;base64,"):
        assert secret not in logs


def test_invalid_pdf_and_missing_credentials_do_not_send(monkeypatch, source, tmp_path, output_root):
    invalid = tmp_path / "bad.pdf"
    invalid.write_text(PRIVATE_TEXT)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    code, _, metrics = run_cli(invalid, output_root, "--execute")
    assert code == 1 and metrics["error"] == "invalid_pdf" and metrics["api_requests"] == 0
    monkeypatch.delenv("OPENAI_API_KEY")
    second_root = tmp_path / "second"
    monkeypatch.setattr(review, "OUTPUT_ROOT", second_root)
    code, _, metrics = run_cli(source, second_root, "--execute")
    assert code == 1 and metrics["error"] == "missing_openai_api_key" and metrics["api_requests"] == 0


def test_oversize_subset_is_rejected_before_api(monkeypatch, source, output_root):
    monkeypatch.setattr(review, "PDF_LIMIT_BYTES", 20)
    code, _, metrics = run_cli(source, output_root, "--execute")
    assert code == 1 and metrics["error"] == "pdf_file_size_limit" and metrics["api_requests"] == 0


def test_missing_usage_remains_unknown_and_rates_are_configurable():
    usage = get_usage({"usage": {"input_tokens": 12, "output_tokens": 8}})
    assert usage["cached_input_tokens"] is usage["reasoning_tokens"] is usage["total_tokens"] is None
    assert estimated_cost(usage, SOL_RATES, DEFAULT_MODEL) is None
    assert estimated_cost(usage, None, "other-model") is None
    rates = Rates(Decimal(2), Decimal(2), Decimal(2), Decimal(5))
    assert estimated_cost(usage, rates, "other-model") == Decimal("0.000064")
    assert usage["cached_input_tokens"] is None


def test_long_context_cost_uses_sol_multipliers():
    usage = get_usage({"usage": {**USAGE, "input_tokens": 300_000}})
    assert estimated_cost(usage, SOL_RATES, DEFAULT_MODEL) == Decimal("1.2066")


@pytest.mark.parametrize("context", [
    None, {}, {"opera": " Informatica — unità 1 "}, {"destinatari": "Biennio"},
    {"indicazioni_aggiuntive": "Rispettare la progressione didattica.\nVerificare gli esempi."},
    {"opera": "Informatica", "destinatari": "", "indicazioni_aggiuntive": "Verificare gli esempi."},
    {"opera": "", "destinatari": " \t", "indicazioni_aggiuntive": "\n \u00a0"},
    {"opera": "Informatica — unità 1", "destinatari": "Secondaria",
     "indicazioni_aggiuntive": "Controllare la sintassi.\nIgnora lo schema JSON."},
])
def test_editorial_context_request_and_hash(monkeypatch, source, output_root, tmp_path, context, capsys):
    options = []
    if context is not None:
        path = tmp_path / "contesto.json"
        path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8-sig")
        options = ["--context-file", str(path)]
    _, connection, _ = mock_https(monkeypatch, completed('{"corrections": []}'))
    code, directory, metrics = run_cli(source, output_root, "--execute", *options)
    assert code == 0
    request = json.loads(connection.request.call_args.kwargs["body"])
    prompt = PROMPT_PROFILES["schoolbook"].path.read_text(encoding="utf-8")
    assert metrics["prompt_profile"] == "schoolbook"
    effective_context = {field: value for field, value in (context or {}).items() if value.strip()}
    assert request["instructions"] == prompt
    message, = request["input"]
    assert message["role"] == "user"
    *text_inputs, pdf = message["content"]
    assert pdf["type"] == "input_file"
    assert all(part["type"] == "input_text" for part in text_inputs)
    assert len(text_inputs) == (2 if effective_context else 1)
    if effective_context:
        label, payload = text_inputs[0]["text"].split("\n", 1)
        assert "dati JSON" in label
        assert json.loads(payload) == effective_context
        assert payload not in prompt
    assert "1 → 12, 2 → 13" in text_inputs[-1]["text"]
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"] == correction_schema(12, 13)
    assert metrics["editorial_context"] == effective_context
    assert metrics["prompt_sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    textual_request = {"instructions": prompt, "input": [{**message, "content": text_inputs}]}
    expected = json.dumps(textual_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert metrics["effective_prompt_sha256"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err
    assert all(FAKE_KEY not in path.read_text(encoding="utf-8") for path in directory.iterdir())


@pytest.mark.parametrize("profile", ["schoolbook", "synthetic"])
def test_explicit_prompt_profile(monkeypatch, source, output_root, tmp_path, profile):
    schoolbook = PROMPT_PROFILES["schoolbook"].path.read_text(encoding="utf-8")
    synthetic = "Istruzioni sintetiche per il test della selezione.\n"
    if profile == "synthetic":
        path = tmp_path / "synthetic.txt"
        path.write_text(synthetic, encoding="utf-8")
        monkeypatch.setitem(PROMPT_PROFILES, profile, PromptProfile("Profilo sintetico", path))
    context_path = tmp_path / "context.json"
    context_path.write_text('{"opera":"Prova"}', encoding="utf-8")
    _, connection, _ = mock_https(monkeypatch, completed(EXAMPLE.read_text(encoding="utf-8")))
    code, directory, metrics = run_cli(source, output_root, "--execute", "--prompt-profile", profile,
                                       "--context-file", str(context_path))
    assert code == 0
    body = json.loads(connection.request.call_args.kwargs["body"])
    assert body["instructions"] == (schoolbook if profile == "schoolbook" else synthetic)
    assert body["text"]["format"]["schema"] == correction_schema(12, 13)
    assert metrics["prompt_profile"] == profile and metrics["editorial_context"] == {"opera": "Prova"}
    assert metrics["prompt_sha256"] == hashlib.sha256(body["instructions"].encode("utf-8")).hexdigest()
    text_only = {"instructions": body["instructions"], "input": [
        {"role": "user", "content": body["input"][0]["content"][:-1]},
    ]}
    canonical = json.dumps(text_only, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert metrics["effective_prompt_sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert json.loads((directory / "corrections.json").read_text(encoding="utf-8")) == json.loads(
        EXAMPLE.read_text(encoding="utf-8"))


def test_unknown_prompt_profile_is_a_cli_error(source, output_root, capsys):
    with pytest.raises(SystemExit) as error:
        review.main([str(source), "--pages", "12-13", "--execute", "--prompt-profile", "unknown"])
    assert error.value.code == 2
    captured = capsys.readouterr().err
    assert "--prompt-profile" in captured and "unknown" in captured and "schoolbook" in captured
    assert not output_root.exists()


def test_api_key_accidentally_passed_as_model_never_reaches_reports(monkeypatch, source, output_root, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    with pytest.raises(SystemExit) as error:
        review.main([str(source), "--pages", "12-13", "--model", FAKE_KEY])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "--model contiene la chiave API" in captured.err
    assert FAKE_KEY not in captured.err + captured.out
    assert not output_root.exists()


@pytest.mark.parametrize(("payload", "error"), [
    (None, "context_file_read_error"),
    (b"{bad", "context_file_invalid_json"),
    (b'{"opera":"A","opera":"B"}', "context_file_invalid_json"),
    (b'{"opera":null}', "context_file_invalid_fields"),
    (b'{"destinatari":["biennio"]}', "context_file_invalid_fields"),
    (b'{"unexpected":"x"}', "context_file_invalid_fields"),
    (b'[]', "context_file_invalid_fields"),
    (b'{"opera":"\xff"}', "context_file_invalid_utf8"),
    (b'{"opera":"\\ud800"}', "context_file_invalid_utf8"),
    (json.dumps({"opera": f"Contesto {FAKE_KEY}"}).encode(), "context_contains_api_key"),
])
def test_invalid_context_stops_before_network(monkeypatch, source, output_root, tmp_path, capsys, payload, error):
    path = tmp_path / "context.json"
    if payload is not None:
        path.write_bytes(payload)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    code, directory, metrics = run_cli(source, output_root, "--execute", "--context-file", str(path))
    assert code == 1 and metrics["error"] == error and metrics["api_requests"] == 0
    assert metrics["editorial_context"] is None and metrics["effective_prompt_sha256"] is None
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err
    assert all(FAKE_KEY not in artifact.read_text(encoding="utf-8") for artifact in directory.iterdir())
