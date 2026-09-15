from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .models import TokenUsage
from .settings import LLMConfig


def parse_gemini_usage(usage: dict[str, Any] | None) -> TokenUsage:
    """Parse a native Gemini usageMetadata object into the shared TokenUsage shape."""
    usage = usage or {}
    return TokenUsage(
        prompt_tokens=int(usage.get("promptTokenCount", 0) or 0),
        completion_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
        total_tokens=int(usage.get("totalTokenCount", 0) or 0),
    )


def _post_json_with_retries(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    max_retries: int,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=timeout_seconds, transport=transport) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()
        except Exception as exc:  # noqa: BLE001 - normalized into RuntimeError below
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"LLM request failed: {last_error}") from last_error


class GeminiClient:
    """Native Google Gemini API client (generativelanguage.googleapis.com).

    Takes a chat-style ``messages`` list (``{"role", "content"}``, the shape the prompt
    builders emit) and translates it into Gemini's contents / systemInstruction format.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        max_retries: int = 2,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def complete_json(self, messages: list[dict[str, str]]) -> tuple[dict[str, Any], TokenUsage]:
        system_texts: list[str] = []
        contents: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            text = message.get("content", "")
            if role == "system":
                system_texts.append(text)
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": text}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
        if system_texts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}

        data = _post_json_with_retries(
            url=f"{self.base_url}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key},
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            transport=self._transport,
        )
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts)
        return json.loads(text), parse_gemini_usage(data.get("usageMetadata"))


def make_llm_client(config: LLMConfig) -> GeminiClient:
    """Gemini is the only provider (2026-08-20). Kept as a function because it adapts
    LLMConfig to the client's kwargs and is the seam every offline test patches."""
    return GeminiClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        max_retries=config.max_retries,
        timeout_seconds=config.timeout_seconds,
    )
