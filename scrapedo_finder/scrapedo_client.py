from __future__ import annotations

import time
from typing import Any

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

    def search_google_ai_mode(self, query: str) -> dict[str, Any]:
        params = self.build_params(query)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.get("https://api.scrape.do/plugin/google/search/ai-mode", params=params)
                    response.raise_for_status()
                    payload = response.json()
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                return payload
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"Scrape.do request failed: {last_error}") from last_error

    def build_params(self, query: str) -> dict[str, str]:
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
        return params
