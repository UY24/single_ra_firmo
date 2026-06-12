# backend/app/services/ai_mode/cost.py
"""Per-run cost: LLM + scrape.do (spec §4 add-on #2).

scrape.do reports per-request credit cost in a response header. Header name must be
verified against a real response on first live run (candidates below) — until then the
env-rate estimate covers it.
"""
from __future__ import annotations

import os

_HEADER_CANDIDATES = ("Scrape.do-Request-Cost", "Scrapedo-Request-Cost",
                      "X-Scrapedo-Request-Cost", "sd-request-cost")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def extract_scrapedo_request_cost(headers) -> float | None:
    for name in _HEADER_CANDIDATES:
        value = headers.get(name) if hasattr(headers, "get") else None
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def calculate_llm_cost_usd(*, provider: str, prompt_tokens: int, completion_tokens: int,
                           batch_mode: bool = False) -> float:
    """USD cost of the cleanup LLM from aggregated token counts.

    Mirrors the legacy serpwow pricing-env convention (same env names/defaults for
    Gemini; batch jobs fall back to sync rates when batch rates are unset). OpenAI
    rates default to 0 — set OPENAI_*_USD_PER_1M_TOKENS to price the openai provider.
    """
    if provider == "openai":
        input_rate = _float_env("OPENAI_INPUT_USD_PER_1M_TOKENS", 0.0)
        output_rate = _float_env("OPENAI_OUTPUT_USD_PER_1M_TOKENS", 0.0)
    else:  # gemini (default provider)
        input_rate = _float_env("GEMINI_INPUT_USD_PER_1M_TOKENS", 0.10)
        output_rate = _float_env("GEMINI_OUTPUT_USD_PER_1M_TOKENS", 0.40)
        if batch_mode:
            input_rate = _float_env("GEMINI_BATCH_INPUT_USD_PER_1M_TOKENS", input_rate)
            output_rate = _float_env("GEMINI_BATCH_OUTPUT_USD_PER_1M_TOKENS", output_rate)
    cost = ((prompt_tokens / 1_000_000) * input_rate) + (
        (completion_tokens / 1_000_000) * output_rate
    )
    return round(cost, 8)


def build_cost_summary(*, llm_usd: float, request_costs: list[float | None],
                       request_count: int) -> dict:
    per_request_usd = float(os.getenv("SCRAPEDO_COST_PER_REQUEST_USD", "0") or 0)
    known = [c for c in request_costs if c is not None]
    if known and len(known) == len(request_costs):
        credits = sum(known)
        # credits→USD conversion depends on the scrape.do plan; expose credits raw and
        # use the env rate per request for the USD figure until a credit rate is known.
        scrapedo_usd = request_count * per_request_usd
        estimated = False
    else:
        credits = sum(known) if known else None
        scrapedo_usd = request_count * per_request_usd
        estimated = True
    return {"llm_usd": round(llm_usd, 6), "scrapedo_usd": round(scrapedo_usd, 6),
            "scrapedo_credits": credits, "scrapedo_requests": request_count,
            "scrapedo_cost_estimated": estimated,
            "total_usd": round(llm_usd + scrapedo_usd, 6)}
