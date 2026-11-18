"""Usage and estimated Standard token charges for a single Responses call."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


SOL_MODEL = "gpt-6.1-sol"
TERRA_MODEL = "gpt-6.1-terra"
DEFAULT_MODEL = TERRA_MODEL


@dataclass(frozen=True)
class Rates:
    input: Decimal
    cached_input: Decimal
    cache_write: Decimal
    output: Decimal


SOL_RATES = Rates(Decimal("2"), Decimal("0.10"), Decimal("2.50"), Decimal("10"))
STANDARD_RATES = {
    SOL_MODEL: SOL_RATES,
    TERRA_MODEL: Rates(Decimal("1"), Decimal("0.05"), Decimal("1.25"), Decimal("5")),
}


def object_at(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


def tokens_at(parent: dict[str, Any], key: str) -> int | None:
    value = parent.get(key)
    return value if type(value) is int and value >= 0 else None


def get_usage(response: dict[str, Any]) -> dict[str, int | None]:
    usage = object_at(response, "usage")
    input_details = object_at(usage, "input_tokens_details")
    output_details = object_at(usage, "output_tokens_details")
    return {
        "input_tokens": tokens_at(usage, "input_tokens"),
        "output_tokens": tokens_at(usage, "output_tokens"),
        "reasoning_tokens": tokens_at(output_details, "reasoning_tokens"),
        "cached_input_tokens": tokens_at(input_details, "cached_tokens"),
        "cache_write_tokens": tokens_at(input_details, "cache_write_tokens"),
        "total_tokens": tokens_at(usage, "total_tokens"),
    }


def estimated_cost(usage: dict[str, int | None], rates: Rates | None, model: str) -> Decimal | None:
    if rates is None:
        return None
    inputs = usage["input_tokens"]
    outputs = usage["output_tokens"]
    cached = usage["cached_input_tokens"]
    writes = usage["cache_write_tokens"]
    if inputs is None or outputs is None:
        return None
    if cached is None and rates.cached_input == rates.input:
        cached = 0
    if writes is None and rates.cache_write == rates.input:
        writes = 0
    if cached is None or writes is None or cached + writes > inputs:
        return None
    input_cost = (inputs - cached - writes) * rates.input
    input_cost += cached * rates.cached_input + writes * rates.cache_write
    output_cost = outputs * rates.output
    if model in STANDARD_RATES and inputs > 272_000:
        input_cost *= 2
        output_cost *= Decimal("1.5")
    return (input_cost + output_cost) / 1_000_000
