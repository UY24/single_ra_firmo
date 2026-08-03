"""Best-effort Slack notifications on run completion/failure.

Mirrors the never-fail-the-run design of ``services/ai_mode/s3_sync.py``: no-ops
silently when ``SLACK_WEBHOOK_URL`` is unset, and swallows + logs every error so a
Slack outage or a bad webhook can never affect a pipeline run. Nothing here raises.

Messages use Slack Block Kit: a plain status header, short summary, and grouped
field grid so the important run details are easy to scan. A concise ``text``
fallback is always included for push notifications / accessibility. Used by both
pipelines (AI Mode in the FastAPI process, SerpWow in the worker); both load
``.env``, so one env var reaches both.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Human labels for the raw pipeline keys (mirrors static/js/runs.js PIPELINE_LABELS).
# AI Mode passes its already-human ``mode_label`` directly, so it isn't in this map.
_PIPELINE_LABELS = {
    "ai_bulk": "Google AI (Bulk)",
    "ai_deep": "Google AI (Deep)",
    "gmaps": "Google Maps",
    "gsearch": "Google Search",
    "firmographics": "Firmographics",
}


def is_configured() -> bool:
    return bool((os.getenv("SLACK_WEBHOOK_URL") or "").strip())


def pipeline_label(pipeline: str | None) -> str:
    """Map a raw pipeline key to its UI label; pass through anything unknown."""
    key = (pipeline or "").strip()
    return _PIPELINE_LABELS.get(key, key or "—")


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _fmt_dur(seconds: float | int | None) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _fmt_tokens(n: int | None) -> str | None:
    if not isinstance(n, int):
        return None
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _fmt_usd(value: int | float) -> str:
    if value == 0:
        return "$0.00"
    if 0 < value < 0.000001:
        return "<$0.000001"
    if -0.000001 < value < 0:
        return ">-$0.000001"
    if abs(value) < 0.01:
        return f"${value:,.6f}".rstrip("0").rstrip(".")
    return f"${value:,.2f}"


def _field(label: str, value: str) -> dict:
    """One cell in the two-column Block Kit field grid."""
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _fields_block(fields: list[dict]) -> dict:
    return {"type": "section", "fields": fields}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _divider() -> dict:
    return {"type": "divider"}


def _post(text: str, blocks: list[dict] | None = None) -> bool:
    """POST to the Slack webhook. ``text`` is the fallback; ``blocks`` the rich UI.

    Best-effort; never raises.
    """
    url = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()
    if not url:
        return False
    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        import httpx

        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning("slack: webhook returned %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:  # never let a notification failure touch the run
        logger.warning("slack: notification failed: %s: %s", type(exc).__name__, exc)
        return False


def notify_run_complete(*, pipeline: str, company: str | None, run_ref: str, status: str,
                        found: int | None = None, not_found: int | None = None,
                        errored: int | None = None, error_sources: dict | None = None,
                        success: int | None = None, failed: int | None = None,
                        total_rows: int | None = None, searches: int | None = None,
                        search_label: str = "Searches", credits: int | None = None,
                        tokens: int | None = None, input_tokens: int | None = None,
                        output_tokens: int | None = None, cost_usd: float | None = None,
                        llm_cost_usd: float | None = None, serpwow_cost_usd: float | None = None,
                        duration_seconds: float | None = None,
                        llm_errors: int | None = None) -> bool:
    """Notify that a run reached a terminal completed / completed_with_errors state.

    ``search_label`` names the search unit for the source engine, e.g.
    ``"Scrape.do searches"`` (AI Mode) or ``"SerpWow searches"``.
    """
    if status == "completed_with_errors":
        headline = "⚠️ Run completed with errors"
        status_text = "`completed_with_errors`"
        summary_prefix = "Completed with errors"
        flavor = "🤕 Made it to the finish line, but a few rows put up a fight."
    else:
        headline = "✅ Run completed"
        status_text = "`completed`"
        summary_prefix = "Completed"
        flavor = "🎉 Smooth sailing — all wrapped up!"

    fields: list[dict] = []
    if errored is not None:
        fields.append(_field("🎯 Outcome",
            f"*{found or 0:,}* found\n*{not_found or 0:,}* not found\n*{errored:,}* errored"))
    elif found is not None or not_found is not None:
        fields.append(_field("🎯 Outcome", f"*{found or 0:,}* found\n*{not_found or 0:,}* not found"))
    else:
        fields.append(_field("🎯 Outcome", f"*{success or 0:,}* succeeded\n*{failed or 0:,}* failed"))
    if isinstance(total_rows, int):
        fields.append(_field("📋 Rows", f"{total_rows:,}"))
    # Provider cell: searches THEN cost when a per-search USD is given; credits when the
    # provider bills in credits (scrape.do Google Maps); otherwise the bare count
    # (e.g. AI Mode's scrape.do flat-fee searches).
    if isinstance(searches, int):
        if isinstance(serpwow_cost_usd, (int, float)):
            fields.append(_field(f"🔎 {search_label}",
                                 f"{searches:,} searches · {_fmt_usd(serpwow_cost_usd)}"))
        elif isinstance(credits, int):
            fields.append(_field(f"🔎 {search_label}",
                                 f"{searches:,} requests · {credits:,} credits"))
        else:
            fields.append(_field(f"🔎 {search_label}", f"{searches:,}"))
    if isinstance(tokens, int):
        val = f"{tokens:,}"
        tin, tout = _fmt_tokens(input_tokens), _fmt_tokens(output_tokens)
        if tin and tout:
            val += f"\n{tin} input / {tout} output"
        fields.append(_field("🪙 Tokens", val))
    # Cost cell: when the LLM/SerpWow split is provided, show LLM + Total (SerpWow
    # is already in the 🔎 cell above); otherwise a single total.
    if isinstance(llm_cost_usd, (int, float)) and isinstance(cost_usd, (int, float)):
        fields.append(_field("💰 Cost", f"LLM {_fmt_usd(llm_cost_usd)}\nTotal {_fmt_usd(cost_usd)}"))
    elif isinstance(cost_usd, (int, float)) and cost_usd > 0:
        fields.append(_field("💰 Cost", _fmt_usd(cost_usd)))
    dur = _fmt_dur(duration_seconds)
    if dur:
        fields.append(_field("⏱️ Duration", dur))
    if isinstance(llm_errors, int) and llm_errors > 0:
        fields.append(_field("🚑 LLM errors", f"{llm_errors:,}"))
    if error_sources:
        top = ", ".join(f"{v} {k}" for k, v in sorted(error_sources.items(), key=lambda kv: -kv[1]))
        fields.append(_field("⚠️ Errors", top))

    blocks = [
        _header(headline),
        _section(f"*{pipeline}*\n{flavor}\n🏢 Company: {company or '—'}\n🚦 Status: {status_text}"),
        _divider(),
        _fields_block(fields),
        _context(f"🆔 Run ref: `{run_ref}`"),
    ]
    if errored is not None:
        summary = f"{found or 0:,} found / {not_found or 0:,} not found / {errored:,} errored"
    elif found is not None or not_found is not None:
        summary = f"{found or 0:,} found / {not_found or 0:,} not found"
    else:
        summary = f"{success or 0:,} succeeded / {failed or 0:,} failed"
    fallback = f"{summary_prefix}: {pipeline} · {company or '—'} · {summary}"
    return _post(fallback, blocks)


def notify_run_failed(*, pipeline: str, company: str | None, run_ref: str,
                      error: str | None, total_rows: int | None = None,
                      duration_seconds: float | None = None) -> bool:
    """Notify that a run failed."""
    err = error or "unknown error"
    fields: list[dict] = []
    if isinstance(total_rows, int):
        fields.append(_field("📋 Rows", f"{total_rows:,}"))
    dur = _fmt_dur(duration_seconds)
    if dur:
        fields.append(_field("⏱️ Ran for", dur))

    blocks = [
        _header("❌ Run failed"),
        _section(f"*{pipeline}*\n💥 The run hit a wall and bailed out.\n"
                 f"🏢 Company: {company or '—'}\n🚦 Status: `failed`"),
        _divider(),
        _section(f"*🧨 What broke*\n```{err}```"),
    ]
    if fields:
        blocks.append(_fields_block(fields))
    blocks.append(_context(f"🆔 Run ref: `{run_ref}`"))

    fallback = f"Run failed: {pipeline} · {company or '—'} · {err}"
    return _post(fallback, blocks)
