from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx


class ScrapeDoClient:
    def __init__(
        self,
        token: str,
        timeout_seconds: float = 90.0,
        max_retries: int = 2,
        device: str = "",
        hl: str = "",
        gl: str = "",
        google_domain: str = "",
        safe: str = "",
        include_html: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.device = device
        self.hl = hl
        self.gl = gl
        self.google_domain = google_domain
        self.safe = safe
        self.include_html = include_html
        self.log = log

    def search_google_ai_mode(
        self, query: str, extra_params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Run one AI-Mode search; returns the parsed JSON payload."""
        params = self.build_params(query, extra_params=extra_params)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            attempt_started = time.perf_counter()
            try:
                self._log(
                    "Scrape.do attempt "
                    f"{attempt + 1}/{self.max_retries + 1} started "
                    f"q_chars={len(query)} estimated_url_chars={self.estimated_url_chars(query, extra_params=extra_params)} "
                    f"timeout={self.timeout_seconds}s"
                )
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.get("https://api.scrape.do/plugin/google/search/ai-mode", params=params)
                    elapsed = time.perf_counter() - attempt_started
                    self._log(
                        "Scrape.do attempt "
                        f"{attempt + 1}/{self.max_retries + 1} response "
                        f"http_status={response.status_code} elapsed={elapsed:.2f}s "
                        f"body_preview={self._body_preview(response)}"
                    )
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"HTTP {response.status_code} {response.reason_phrase}: "
                            f"{self._body_preview(response)}"
                        )
                    payload = response.json()
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                self._log(
                    "Scrape.do payload accepted "
                    f"keys={sorted(payload.keys())} "
                    f"text_blocks={len(payload.get('text_blocks') or [])} "
                    f"references={len(payload.get('references') or [])}"
                )
                return payload
            except Exception as exc:
                last_error = exc
                self._log(
                    "Scrape.do attempt "
                    f"{attempt + 1}/{self.max_retries + 1} failed "
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt >= self.max_retries:
                    break
                sleep_seconds = min(2**attempt, 8)
                self._log(f"Scrape.do retrying after {sleep_seconds}s")
                time.sleep(sleep_seconds)
        raise RuntimeError(f"Scrape.do request failed: {last_error}") from last_error

    def build_params(self, query: str, extra_params: dict[str, str] | None = None) -> dict[str, str]:
        params = {
            "token": self.token,
            "q": query,
        }
        if self.device:
            params["device"] = self.device
        if self.hl:
            params["hl"] = self.hl
        if self.gl:
            params["gl"] = self.gl
        if self.google_domain:
            params["google_domain"] = self.google_domain
        if self.include_html:
            params["include_html"] = "true"
        if self.safe:
            params["safe"] = self.safe
        for key, value in (extra_params or {}).items():
            value = str(value or "").strip()
            if value:
                params[key] = value
        return params

    def estimated_url_chars(self, query: str, extra_params: dict[str, str] | None = None) -> int:
        base_url = "https://api.scrape.do/plugin/google/search/ai-mode"
        return len(base_url) + 1 + len(urlencode(self.build_params(query, extra_params=extra_params)))

    def _log(self, message: str) -> None:
        if self.log:
            self.log(message)

    def _body_preview(self, response: httpx.Response, limit: int = 500) -> str:
        text = response.text.replace("\n", "\\n").replace("\r", "\\r").strip()
        if not text:
            return "<empty>"
        if len(text) > limit:
            return text[:limit] + "...<truncated>"
        return text
