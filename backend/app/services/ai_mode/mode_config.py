# backend/app/services/ai_mode/mode_config.py
"""One engine, two configs (spec §5)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.core.config import PROMPTS_DIR


@dataclass(frozen=True)
class ModeConfig:
    key: str                 # 'ai_bulk' | 'ai_deep'
    label: str
    prompt_file: str
    batch_size_env: str
    default_batch_size: int

    def batch_size(self) -> int:
        value = os.getenv(self.batch_size_env)
        if value and value.strip():
            try:
                return max(1, int(value.strip()))
            except ValueError:
                pass
        return self.default_batch_size

    def search_prompt(self) -> str:
        return (PROMPTS_DIR / self.prompt_file).read_text(encoding="utf-8")


MODES: dict[str, ModeConfig] = {
    "ai_bulk": ModeConfig("ai_bulk", "AI Mode 1 — Bulk", "ai_bulk_search.txt",
                          "AI_BULK_BATCH_SIZE", 10),
    "ai_deep": ModeConfig("ai_deep", "AI Mode 2 — Deep Search", "ai_deep_search.txt",
                          "AI_DEEP_BATCH_SIZE", 3),
}


def get_mode(key: str) -> ModeConfig:
    return MODES[key]
