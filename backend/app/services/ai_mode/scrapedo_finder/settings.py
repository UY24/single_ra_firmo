from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.core.config import PROJECT_ROOT

# Package directory; used by prompting modules to locate bundled prompt templates.
BASE_DIR = Path(__file__).resolve().parent


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
        batch_size=batch_size or _int_env("SCRAPEDO_BATCH_SIZE", 5),
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


DEFAULT_LLM_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    provider: str = "openai"
    max_retries: int = 2
    timeout_seconds: float = 120.0

    def validate(self) -> None:
        if self.provider not in DEFAULT_LLM_BASE_URLS:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(DEFAULT_LLM_BASE_URLS)}; got '{self.provider}'"
            )
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self.api_key),
                ("LLM_BASE_URL", self.base_url),
                ("LLM_MODEL", self.model),
            )
            if not value
        ]
        if missing:
            raise ValueError("Required: " + ", ".join(missing))


def load_llm_config(env_file: Path | None = None) -> LLMConfig:
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    provider = (os.getenv("LLM_PROVIDER", "openai").strip().lower()) or "openai"
    base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        base_url = DEFAULT_LLM_BASE_URLS.get(provider, "")
    config = LLMConfig(
        api_key=os.getenv("LLM_API_KEY", "").strip(),
        base_url=base_url,
        model=os.getenv("LLM_MODEL", "").strip(),
        provider=provider,
        max_retries=_int_env("LLM_MAX_RETRIES", 2),
        timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", 120.0),
    )
    config.validate()
    return config
