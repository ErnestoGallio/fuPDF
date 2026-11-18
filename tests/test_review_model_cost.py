from decimal import Decimal

import pytest

from pdf_annotator.services.review_contract.metrics import SOL_MODEL, TERRA_MODEL, estimated_cost, get_usage
from pdf_annotator.services.review_jobs import ReviewSettings
from pdf_annotator.services.review_limits import RateController, RateLimits


@pytest.mark.parametrize(("model", "cost", "long_cost"), [
    (SOL_MODEL, "0.544400", "1.038804"),
    (TERRA_MODEL, "0.272200", "0.519402"),
])
def test_standard_cost_and_long_context_from_response_usage(model, cost, long_cost):
    usage = get_usage({"usage": {
        "input_tokens": 272_000, "output_tokens": 10_000,
        "input_tokens_details": {"cached_tokens": 64_000, "cache_write_tokens": 44_000},
        "output_tokens_details": {"reasoning_tokens": 4_000},
    }})
    rates = ReviewSettings(model=model).pricing
    assert estimated_cost(usage, rates, model) == Decimal(cost)
    assert estimated_cost({**usage, "input_tokens": 272_001}, rates, model) == Decimal(long_cost)
    assert estimated_cost({**usage, "cache_write_tokens": None}, rates, model) is None
    assert estimated_cost({**usage, "cached_input_tokens": 272_000}, rates, model) is None


def test_model_limits_are_separate_under_one_project():
    controller = RateController()
    sol, terra = ReviewSettings(model=SOL_MODEL), ReviewSettings(model=TERRA_MODEL)
    controller.configure("project", sol.family, RateLimits(2, 200), RateLimits(1, 50))
    controller.configure("project", terra.family, RateLimits(2, 200), RateLimits(2, 150))
    controller.reserve("project", sol.family, 50, 1)
    assert controller.availability("project", sol.family, 1, 2)[0] > 0
    controller.reserve("project", terra.family, 70, 2)
    assert {reservation.family for reservation in controller.active} == {SOL_MODEL, TERRA_MODEL}
    assert controller.availability("another-project", terra.family, 1, 2)[0] > 0


def test_unknown_service_model_is_rejected():
    with pytest.raises(ValueError, match="Unknown review model"):
        ReviewSettings(model="unknown-model")
