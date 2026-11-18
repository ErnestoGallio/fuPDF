import json
from unittest.mock import Mock

import pytest

from pdf_annotator.services import review_transport
from pdf_annotator.services.review_limits import RateController, RateLimits
from pdf_annotator.services.review_transport import OpenAIReviewTransport, TransportResult, classify_response


@pytest.mark.parametrize("status,error,retryable,systemic,uncertain", [
    (429, "rate_limited", True, False, False),
    (503, "service_unavailable", True, False, False),
    (401, "authentication_failed", False, True, False),
    (403, "permission_denied", False, True, False),
    (404, "model_unavailable", False, True, False),
    (413, "pdf_upload_rejected", False, False, False),
    (400, "request_rejected", False, True, False),
    (504, "remote_outcome_unknown", False, False, True),
])
def test_http_classification(status, error, retryable, systemic, uncertain):
    reply = classify_response(status, {}, {})
    assert (reply.error, reply.retryable, reply.systemic, reply.uncertain) == (
        error, retryable, systemic, uncertain)


def test_fixed_endpoint_one_post_and_allowlisted_headers(monkeypatch):
    connection = Mock()
    factory = Mock(return_value=connection)
    monkeypatch.setattr(review_transport, "HTTPSConnection", factory)
    response = connection.getresponse.return_value
    response.status = 429
    response.getheaders.return_value = [
        ("Set-Cookie", "private"), ("X-Request-ID", "req_123"),
        ("X-RateLimit-Limit-Project-Tokens", "50000"), ("X-RateLimit-Reset-Tokens", "1m2.5s"),
        ("X-RateLimit-Remaining-Requests", "unrecognized"), ("Retry-After", "4"),
    ]
    response.read.return_value = json.dumps({"error": {"code": "insufficient_quota"}}).encode()
    transport = OpenAIReviewTransport("credential-test", timeout=10)
    reply = transport.post(b'{"store":false}')
    factory.assert_called_once_with("api.openai.com", timeout=10)
    connection.request.assert_called_once_with("POST", "/v1/responses", body=b'{"store":false}', headers={
        "Authorization": "Bearer credential-test", "Content-Type": "application/json"})
    response.read.assert_called_once_with(review_transport.MAX_RESPONSE_BYTES + 1)
    connection.close.assert_called_once()
    assert reply.systemic and not reply.retryable and reply.error == "quota_exhausted"
    assert reply.headers == {"x-request-id": "req_123", "x-ratelimit-limit-project-tokens": "50000",
                             "x-ratelimit-reset-tokens": "1m2.5s", "retry-after": "4"}


@pytest.mark.parametrize("during_connect", [True, False])
def test_network_failure_has_no_replay(monkeypatch, during_connect):
    connection = Mock()
    monkeypatch.setattr(review_transport, "HTTPSConnection", lambda *a, **kw: connection)
    failed = connection.connect if during_connect else connection.getresponse
    failed.side_effect = TimeoutError
    result = OpenAIReviewTransport("credential-test").post(b"data")
    assert not result.retryable and result.uncertain is not during_connect
    assert result.sent is not during_connect
    assert connection.request.call_count == (0 if during_connect else 1)
    connection.close.assert_called_once()


def test_project_and_model_rate_limits_and_usage_adjustment():
    rates = RateController()
    rates.configure("project", "family", RateLimits(3, 130), RateLimits(2, 100))
    rates.configure("project", "other_family", RateLimits(), RateLimits(5, 200))
    first = rates.reserve("project", "family", 40, 0)
    second = rates.reserve("project", "family", 40, 1)
    assert rates.availability("independent", "family", 1, 1) == (0.05, None)
    rates.finish(first, TransportResult(), {"total_tokens": 70}, 2, 2, 0)
    assert rates.estimate("project", "family", 2, 20, 1) == 90
    assert rates.availability("project", "other_family", 25, 2) == (58, None)
    assert rates.availability("project", "family", 101, 2) == (0, "batch_exceeds_rate_budget")
    assert rates.availability("independent", "family", 1, 2) == (0, None)
    rates.finish(second, TransportResult(), {}, 1, 3, 0)
    assert rates.availability("project", "family", 10, 3) == (57, None)
    assert rates.availability("project", "family", 10, 60) == (0, None)


def test_unknown_budget_and_out_of_order_headers_are_conservative():
    rates = RateController()
    first = rates.reserve("p", "m", 30, 0)
    assert rates.availability("p", "m", 30, 0) == (0.05, None)
    reply = TransportResult(headers={
        "x-ratelimit-limit-requests": "5", "x-ratelimit-limit-tokens": "150",
        "x-ratelimit-remaining-tokens": "120", "x-ratelimit-reset-tokens": "10s"})
    rates.finish(first, reply, {}, 1, 1, 0)
    older = rates.reserve("p", "m", 20, 1)
    newer = rates.reserve("p", "m", 20, 2)
    rates.finish(newer, TransportResult(headers={
        "x-ratelimit-remaining-tokens": "5", "x-ratelimit-reset-tokens": "8s",
    }), {}, 1, 3, 0)
    rates.finish(older, TransportResult(headers={
        "x-ratelimit-remaining-tokens": "95", "x-ratelimit-reset-tokens": "7s",
    }), {}, 1, 4, 0)
    assert rates.availability("p", "m", 10, 4) == (7, None)
    later = rates.reserve("p", "m", 10, 11)
    rates.finish(later, TransportResult(), {}, 1, 12, 0)
    assert rates.availability("p", "m", 151, 12) == (0, "batch_exceeds_rate_budget")


def test_retry_after_and_slowdown_apply_to_later_admissions():
    rate = RateController()
    first = rate.reserve("p", "m", 10, 0)
    reply = classify_response(503, {"error": {"code": "slow_down"}}, {"retry-after": "5"})
    assert rate.finish(first, reply, {}, 1, 1, 2) == 6
    assert rate.availability("p", "m", 10, 2) == (4, None)
    assert rate.availability("p", "other", 10, 2) == (0, None)
    second = rate.reserve("p", "m", 10, 6)
    rate.finish(second, TransportResult(), {}, 1, 6.2, 0)
    assert rate.availability("p", "m", 10, 6.2)[0] == pytest.approx(0.8)


def test_429_rpm_exhaustion_does_not_wait_for_a_full_token_reset():
    rate = RateController()
    first = rate.reserve("p", "m", 10, 0)
    headers = {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "3s",
               "x-ratelimit-remaining-tokens": "10000", "x-ratelimit-reset-tokens": "6m"}
    reply = classify_response(429, {}, headers)
    assert rate.finish(first, reply, {}, 1, 1, 1) == 4


@pytest.mark.parametrize("code", ["credit_balance_exhausted", "organization_spend_limit_exceeded",
                                  "project_spend_limit_exceeded", "organization_usage_limit_exceeded"])
def test_quota_429_ignores_retry_indication(code):
    result = classify_response(429, {"error": {"code": code}}, {"retry-after": "3"})
    assert result.systemic and result.error == "quota_exhausted" and not result.retryable
