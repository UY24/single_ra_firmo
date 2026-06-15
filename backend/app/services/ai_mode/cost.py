# backend/app/services/ai_mode/cost.py
"""Per-run cost: LLM only.

scrape.do is billed as a flat fee, not per-request, so we do NOT compute a scrape.do
dollar cost — we just count searches done (one scrape.do request = one search, which in
``ai_bulk`` covers a batch of entities). That count is surfaced per run and per company.
"""
from __future__ import annotations

import os


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


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


def build_cost_summary(*, llm_usd: float, request_count: int) -> dict:
    """Per-run cost summary. scrape.do is a flat fee, so the only dollar figure is the
    LLM cleanup cost; ``scrapedo_searches`` is just the count of scrape.do requests."""
    return {"llm_usd": round(llm_usd, 6),
            "scrapedo_searches": request_count,
            "total_usd": round(llm_usd, 6)}
