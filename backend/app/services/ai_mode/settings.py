from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.core.config import PROJECT_ROOT


@dataclass
class Settings:
    scrapedo_token: str
    batch_size: int = 5
    scrapedo_timeout_seconds: float = 90.0
    scrapedo_max_retries: int = 2
    scrapedo_max_query_chars: int = 6000
    scrapedo_device: str = ""
    scrapedo_hl: str = ""
    scrapedo_gl: str = ""
    scrapedo_google_domain: str = ""
    scrapedo_safe: str = ""
    scrapedo_include_html: bool = False

    def validate(self) -> None:
        if not self.scrapedo_token:
            raise ValueError("Required: SCRAPEDO_TOKEN")
        if self.batch_size < 1:
            raise ValueError("Batch size must be >= 1")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    return float(value)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env_file: Path | None = None, batch_size: int | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    settings = Settings(
        scrapedo_token=os.getenv("SCRAPEDO_TOKEN", "").strip(),
        batch_size=batch_size or 5,
        scrapedo_timeout_seconds=_float_env("SCRAPEDO_TIMEOUT_SECONDS", 90.0),
        scrapedo_max_retries=_int_env("SCRAPEDO_MAX_RETRIES", 2),
        scrapedo_max_query_chars=_int_env("SCRAPEDO_MAX_QUERY_CHARS", 6000),
        scrapedo_device=os.getenv("SCRAPEDO_DEVICE", "").strip(),
        scrapedo_hl=os.getenv("SCRAPEDO_HL", "").strip(),
        scrapedo_gl=os.getenv("SCRAPEDO_GL", "").strip(),
        scrapedo_google_domain=os.getenv("SCRAPEDO_GOOGLE_DOMAIN", "").strip(),
        scrapedo_safe=os.getenv("SCRAPEDO_SAFE", "").strip(),
        scrapedo_include_html=_bool_env("SCRAPEDO_INCLUDE_HTML"),
    )
    settings.validate()
    return settings


# Gemini is the only LLM provider (2026-08-20). The OpenAI-compatible transport and its
# AI_MODE_LLM_PROVIDER switch are deleted: nothing used it, batch cleanup was Gemini-only
# anyway (so LLM_BATCH already forced this branch), and a second provider with untuned
# prompts and no pricing defaults was a config path nobody would have noticed breaking.
#
# There is no `provider` field either. With one provider it could only ever hold one value,
# and a field that cannot vary is not configuration -- it is a constant pretending to be a
# choice, which is what the next person reads as "so I can switch it".
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class LLMConfig:
    """Which Gemini model to call, and how patiently. The only thing chosen from env is
    the MODEL -- the endpoint is fixed and the key has one name."""

    api_key: str
    model: str
    base_url: str = GEMINI_BASE_URL
    max_retries: int = 2
    timeout_seconds: float = 120.0

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("GEMINI_API_KEY", self.api_key),
                ("GEMINI_MODEL", self.model),
                ("base_url", self.base_url),
            )
            if not value
        ]
        if missing:
            raise ValueError("Required: " + ", ".join(missing))
