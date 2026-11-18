"""One Responses POST per attempt, with classified diagnostics and rate feedback."""

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException, HTTPSConnection
import json
import math
import re
from socket import SHUT_RDWR
from threading import Timer
from time import monotonic, time
from typing import Protocol


RATE_RESOURCES = ("requests", "tokens", "project-requests", "project-tokens")
RATE_HEADERS = frozenset(f"x-ratelimit-{part}-{resource}"
                         for part in ("limit", "remaining", "reset") for resource in RATE_RESOURCES)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RATE_WAIT = 900.0


@dataclass(frozen=True)
class TransportResult:
    status: int | None = None
    body: dict = field(default_factory=dict, repr=False)
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    retryable: bool = False
    systemic: bool = False
    uncertain: bool = False
    sent: bool = True


class ReviewTransport(Protocol):
    def post(self, body: bytes) -> TransportResult: ...


def duration(value: str) -> float | None:
    if len(value) > 64:
        return None
    factors = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|[dhms])", value)
    if parts and "".join(number + unit for number, unit in parts) == value:
        return sum(float(number) * factors[unit] for number, unit in parts)
    return None


def retry_after(value: str, now: float) -> float | None:
    if len(value) > 64:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is not None:
            return max(0.0, date.timestamp() - now)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return None


def classify_response(status: int, body: dict, headers: dict[str, str]) -> TransportResult:
    result = {"status": status, "body": body, "headers": headers}
    if 200 <= status < 300:
        return TransportResult(**result)
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    quota_errors = (
        "insufficient_quota", "billing_hard_limit_reached", "billing_not_active",
        "credit_balance_exhausted", "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
    )
    if code in quota_errors or kind in quota_errors:
        return TransportResult(**result, error="quota_exhausted", systemic=True)
    if status == 401:
        return TransportResult(**result, error="authentication_failed", systemic=True)
    if status == 403:
        return TransportResult(**result, error="permission_denied", systemic=True)
    if status == 404 or code == "model_not_found":
        return TransportResult(**result, error="model_unavailable", systemic=True)
    if status == 400 and code == "context_length_exceeded":
        return TransportResult(**result, error="model_context_exceeded")
    if status == 413:
        return TransportResult(**result, error="pdf_upload_rejected")
    if status in (429, 503):
        error = "rate_limited" if status == 429 else "service_unavailable"
        message = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else None
        if code == "slow_down" or kind == "slow_down" or (
                status == 503 and isinstance(message, str) and "slow down" in message.lower()):
            error = "slow_down"
        return TransportResult(**result, error=error, retryable=True)
    if status in (408, 500, 502, 504):
        return TransportResult(**result, error="remote_outcome_unknown", uncertain=True)
    return TransportResult(**result, error="request_rejected", systemic=True)


class OpenAIReviewTransport:
    """The caller supplies a credential for an explicitly started job."""

    def __init__(self, api_key: str, *, timeout: float = 180.0) -> None:
        if not re.fullmatch(r"[!-~]+", api_key):
            raise ValueError("Invalid API credential")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Expected a finite positive socket timeout")
        self._api_key = api_key
        self.timeout = timeout

    def contains_credential(self, *values: object) -> bool:
        return any(isinstance(value, str) and self._api_key in value for value in values)

    def check_local_metadata(self, *values: str) -> None:
        if self.contains_credential(*values):
            raise ValueError("Review configuration contains the API credential")

    def post(self, body: bytes) -> TransportResult:
        # http.client neither follows redirects nor retries a POST.
        connection = HTTPSConnection("api.openai.com", timeout=self.timeout)
        deadline = monotonic() + self.timeout
        status = None
        response = None
        headers: dict[str, str] = {}
        try:
            connection.connect()
        except (OSError, HTTPException):
            connection.close()
            return TransportResult(error="connection_failed", sent=False)
        remaining = deadline - monotonic()
        if remaining <= 0:
            connection.close()
            return TransportResult(error="connection_timeout", sent=False)

        sock = connection.sock

        def abort() -> None:
            # Keep the original socket, including when http.client transfers
            # ownership to a response with Connection: close.
            if sock is not None:
                try:
                    sock.shutdown(SHUT_RDWR)
                except OSError:
                    sock.close()

        timer = Timer(remaining, abort)
        timer.daemon = True
        timer.start()
        try:
            connection.request("POST", "/v1/responses", body=body, headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            status = response.status
            for name, value in response.getheaders():
                name, value = name.lower(), value.strip()
                if self.contains_credential(value):
                    continue
                if ((name.startswith(("x-ratelimit-limit-", "x-ratelimit-remaining-"))
                        and name in RATE_HEADERS and re.fullmatch(r"\d{1,15}", value))
                        or (name.startswith("x-ratelimit-reset-") and name in RATE_HEADERS
                            and duration(value) is not None)
                        or (name == "retry-after" and retry_after(value, time()) is not None)
                        or (name == "x-request-id" and re.fullmatch(r"[\w.-]{1,128}", value, re.ASCII))):
                    headers[name] = value
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            try:
                payload = json.loads(raw) if len(raw) <= MAX_RESPONSE_BYTES else None
            except (ValueError, RecursionError):
                payload = None
            if 200 <= status < 300 and not isinstance(payload, dict):
                return TransportResult(status, headers=headers, error="invalid_api_json")
            return classify_response(status, payload if isinstance(payload, dict) else {}, headers)
        except (OSError, HTTPException):
            # Error responses already received still retain their HTTP classification.
            if status is not None and status not in (429, 503) and not 200 <= status < 300:
                return classify_response(status, {}, headers)
            return TransportResult(status, headers=headers, error="remote_outcome_unknown", uncertain=True)
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
