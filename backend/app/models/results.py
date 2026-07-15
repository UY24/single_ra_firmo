# backend/app/models/results.py
"""Unified AI-mode output schema for BOTH modes (spec §5)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Flag:
    flag: str
    why: str


@dataclass
class AttemptLogEntry:
    query: str
    result: str
    url: str | None = None


@dataclass
class EntityResult:
    company_name: str
    country: str
    sno: int
    company_local_name: str | None = None
    website_url: str | None = None
    confidence: int = 0
    flags: list[Flag] = field(default_factory=list)
    attempt_log: list[AttemptLogEntry] = field(default_factory=list)
    error: str | None = None
    # Shared with AI Mode; additive defaults so existing callers are unaffected.
    error_source: str | None = None
    error_category: str | None = None
    degraded_search: bool = False

    def flags_csv(self) -> str:
        return "\n".join(f"{f.flag}: {f.why}" for f in self.flags)

    def attempt_log_csv(self) -> str:
        lines = []
        for i, a in enumerate(self.attempt_log, start=1):
            line = f"{i}. {a.query} → {a.result}"
            if a.url:
                line += f" [{a.url}]"
            lines.append(line)
        return "\n".join(lines)

    def to_report_dict(self) -> dict:
        return {
            "sno": self.sno, "company_name": self.company_name,
            "company_local_name": self.company_local_name, "country": self.country,
            "website_url": self.website_url, "confidence": self.confidence,
            "flags": [{"flag": f.flag, "why": f.why} for f in self.flags],
            "attempt_log": [{"query": a.query, "result": a.result, "url": a.url}
                            for a in self.attempt_log],
            "error": self.error,
            "error_source": self.error_source,
            "error_category": self.error_category,
            "degraded_search": self.degraded_search,
        }

    @classmethod
    def from_llm_object(cls, obj: dict, fallback_country: str = "",
                        fallback_name: str = "", fallback_local: str | None = None,
                        fallback_sno: int = 0) -> "EntityResult":
        flags = [Flag(str(f.get("flag", "")), str(f.get("why", "")))
                 for f in obj.get("flags") or [] if isinstance(f, dict)]
        attempts = [AttemptLogEntry(str(a.get("query", "")), str(a.get("result", "")),
                                    a.get("url") or None)
                    for a in obj.get("attempt_log") or [] if isinstance(a, dict)]
        try:
            confidence = max(0, min(100, int(obj.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0
        return cls(
            company_name=str(obj.get("company_name") or fallback_name),
            country=str(obj.get("country") or fallback_country),
            sno=int(obj.get("sno") or fallback_sno),
            company_local_name=obj.get("company_local_name") or fallback_local,
            website_url=(obj.get("website_url") or None),
            confidence=confidence, flags=flags, attempt_log=attempts,
        )
