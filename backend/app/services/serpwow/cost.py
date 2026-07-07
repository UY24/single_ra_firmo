# backend/app/services/serpwow/cost.py
"""Pricing math for SerpWow pipelines (Gemini per-1M-token + SerpWow per-search)."""
from __future__ import annotations

from typing import Any, Optional

from app.services.common.env import get_float_env


def calculate_gemini_cost_usd(usage: Optional[dict[str, Any]]) -> float:
    if not usage:
        return 0.0

    input_tokens = int(usage.get("promptTokenCount", 0) or 0)
    output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

    input_usd_per_1m = get_float_env("GEMINI_INPUT_USD_PER_1M_TOKENS", 0.10)
    output_usd_per_1m = get_float_env("GEMINI_OUTPUT_USD_PER_1M_TOKENS", 0.40)

    cost = ((input_tokens / 1_000_000) * input_usd_per_1m) + (
        (output_tokens / 1_000_000) * output_usd_per_1m
    )
    return round(cost, 8)


def calculate_gemini_batch_cost_usd(usage: Optional[dict[str, Any]]) -> float:
    if not usage:
        return 0.0

    input_tokens = int(usage.get("promptTokenCount", 0) or 0)
    output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)

    input_usd_per_1m = get_float_env(
        "GEMINI_BATCH_INPUT_USD_PER_1M_TOKENS",
        get_float_env("GEMINI_INPUT_USD_PER_1M_TOKENS", 0.10),
    )
    output_usd_per_1m = get_float_env(
        "GEMINI_BATCH_OUTPUT_USD_PER_1M_TOKENS",
        get_float_env("GEMINI_OUTPUT_USD_PER_1M_TOKENS", 0.40),
    )

    cost = ((input_tokens / 1_000_000) * input_usd_per_1m) + (
        (output_tokens / 1_000_000) * output_usd_per_1m
    )
    return round(cost, 8)


def calculate_serpwow_cost_usd(requests: int) -> float:
    if requests <= 0:
        return 0.0
    usd_per_request = get_float_env("SERPWOW_USD_PER_SEARCH",
                                    get_float_env("SERPWOW_USD_PER_REQUEST", 0.0))
    return round(requests * usd_per_request, 8)
