from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class EntityInput:
    entity_name: str
    country: str = ""
    address: str = ""
    firm_id: int | None = None
    row_number: int = 0


@dataclass
class CompanyEntityInput:
    company_name_eng: str
    company_name_local: str = ""
    country_code: str = ""
    isic: int | None = None
    row_number: int = 0

    @property
    def entity_name(self) -> str:
        """English name is the canonical name used by the scrape runner."""
        return self.company_name_eng


@dataclass
class CompanyFlag:
    flag: str = ""
    why: str = ""


@dataclass
class CompanyCleanResult:
    company_name_eng: str
    company_name_local: str = ""
    country_code: str = ""
    thinking: str = ""
    confidence: int = 0
    flags: list["CompanyFlag"] = field(default_factory=list)
    website_url: str | None = None
    error: str | None = None
    sno: int = 0


@dataclass
class ScrapeDoRequestRecord:
    request_index: int
    entity_names: list[str]
    query: str
    status: str | None = None
    request_id: str | None = None
    json_endpoint: str | None = None
    raw_json_file: str | None = None
    time_taken_seconds: float = 0.0
    error: str | None = None


@dataclass
class BatchRunResult:
    batch_id: str
    csv_file: str
    total_entities: int
    started_at: str
    completed_at: str
    batch_duration_seconds: float
    scrapedo_request_count: int
    request_records: list[ScrapeDoRequestRecord] = field(default_factory=list)
    input_type: str = "address"


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class AttemptLogEntry:
    query: str = ""
    result: str = ""
    url: str | None = None


@dataclass
class EntityCleanResult:
    entity_name: str
    short_details: str = ""
    official_website: str | None = None
    found_at_attempt: int | None = None
    attempt_log: list[AttemptLogEntry] = field(default_factory=list)
    error: str | None = None
    country: str = ""
    location: str = ""
    sno: int = 0


JsonDict = dict[str, Any]
