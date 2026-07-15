"""Data-driven search phases for relationship-mode lookups."""
from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Callable, Collection, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from app.services.serpwow.url_utils import (
    canonicalize_official_url,
    is_disallowed_official_url,
    url_matches_domain,
)


PhaseGoal = Literal["combined", "relationship_evidence", "official_url"]
SearchPolicy = Literal["adaptive", "sequential"]

_URL_TOKEN_RE = re.compile(
    r"https?://[^\s<>\"']+|"
    r"(?<![@\w])(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+[a-z]{2,63}",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
_BARE_FILE_SUFFIXES = frozenset({
    "7z", "css", "csv", "doc", "docx", "gz", "htm", "html", "js", "json",
    "jsx", "md", "pdf", "ppt", "pptx", "py", "rar", "rtf", "sh", "tar",
    "ts", "tsx", "txt", "xls", "xlsx", "xml", "yaml", "yml", "zip",
})
_NAME_STOPWORDS = frozenset({
    "capital", "co", "company", "corp", "corporation", "group", "holding",
    "holdings", "inc", "incorporated", "lab", "labs", "limited", "llc",
    "ltd", "partners", "plc", "technologies", "technology",
})
_FINANCIAL_MARKER_RE = re.compile(
    r"\binvest(?:s|ed|ing|ment(?:s)?|or(?:s)?)?\b|"
    r"\bfund(?:s|ed|ing)?\b|"
    r"\bfinanc(?:e|es|ed|ing)\b|"
    r"\b(?:financial\s+)?backing\b|\bfinancially\s+backs?\b|"
    r"\bportfolio\b|\bbacked\b|"
    r"\bacquisition(?:s)?\b|\bacquired\b|"
    r"\bparent(?:\s+company)?\b|\bsubsidiar(?:y|ies)\b|\bownership\b|"
    r"\b(?:led|participated\s+in)\b[^.;\n]{0,80}\bseries\s+[a-z0-9]+\b",
    re.IGNORECASE,
)
_POSITIVE_FINANCIAL_RE = re.compile(
    r"\binvest(?:s|ed|ing)?(?:\s+\w+){0,3}\s+(?:in|into)\b|"
    r"\binvestor(?:s)?\s+(?:in|of)\b|"
    r"\binvestment(?:s)?\s+(?:in|into|from|by)\b|"
    r"\bfund(?:s|ed)\s+\w+\b|\bfund\s+for\b|"
    r"\bfunding(?:\s+\w+){0,4}\s+(?:from|by|for)\b|"
    r"\bfinanc(?:e|es|ed)\s+\w+\b|"
    r"\bfinancing\s+(?:to|from|by|for)\b|"
    r"\b(?:financial\s+)?backing\s+(?:to|from|by|for)\b|"
    r"\bbacked\s+by\b|\bfinancially\s+backs?\s+\w+\b|"
    r"\bportfolio\s+compan(?:y|ies)\b|"
    r"\bin\b[^.;\n]{0,80}\bportfolio\b|"
    r"\bacquisition(?:s)?\s+(?:of|by)\b|"
    r"\bacquired\s+by\b|"
    r"\bparent\s+company\b|\bsubsidiar(?:y|ies)\b|"
    r"\bownership\s+(?:of|in)\b|"
    r"\b(?:led|participated\s+in)\b[^.;\n]{0,80}\bseries\s+[a-z0-9]+\b",
    re.IGNORECASE,
)
_GROUNDED_ACTIVE_FINANCIAL_RE = re.compile(
    r"\b(?:acquired|backed)\s+\w+\b",
    re.IGNORECASE,
)
_NEGATIVE_FINANCIAL_RE = re.compile(
    r"\b(?:no|not|without|never|"
    r"(?:did|was|is|has|have|were|are|do|does)n['’]t)\b"
    r"(?:\s+[\w'’]+){0,8}\s+"
    r"(?:financial\s+relationship|relationship|"
    r"invest(?:s|ed|ing|ment(?:s)?|or(?:s)?)?|"
    r"fund(?:s|ed|ing)?|financ(?:e|es|ed|ing)|backing|backed|"
    r"financially\s+backs?|portfolio|acquisition(?:s)?|acquired|"
    r"parent(?:\s+company)?|subsidiar(?:y|ies)|ownership)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RelationshipSearchInput:
    x_name: str
    y_name: str
    input_url: str
    x_domain: str
    city: str
    country: str


QueryBuilder = Callable[
    [RelationshipSearchInput, Sequence[Mapping[str, object]]],
    str,
]


@dataclass(frozen=True)
class RelationshipPhase:
    name: str
    goal: PhaseGoal
    build_query: QueryBuilder
    enabled: bool = True


def extract_candidate_records(
    raw_result: Mapping[str, Any],
    phase: str,
    x_domain: str,
) -> list[dict[str, str]]:
    """Extract canonical URL candidates with first-seen field provenance."""
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: object, source_field: str) -> None:
        if not isinstance(value, str):
            return
        original = value.strip().rstrip(_TRAILING_URL_PUNCTUATION)
        if (not original or "@" in original or re.search(r"\s", original)
                or ("://" in original
                    and not original.lower().startswith(("http://", "https://")))):
            return
        try:
            parsed_host = urlsplit(
                original if "://" in original else f"https://{original}"
            ).hostname
            if parsed_host:
                ip_address(parsed_host)
                return
        except ValueError:
            pass
        canonical = canonicalize_official_url(original)
        if (not canonical or canonical in seen
                or is_disallowed_official_url(canonical)
                or url_matches_domain(canonical, x_domain)):
            return
        seen.add(canonical)
        records.append({
            "url": canonical,
            "original_text": original,
            "phase": phase,
            "source_field": source_field,
            "evidence_id": f"{phase}.candidate.{len(records)}",
        })

    def add_text(value: object, source_field: str) -> None:
        if not isinstance(value, str):
            return
        masked = _EMAIL_RE.sub(lambda match: " " * len(match.group()), value)
        for match in _URL_TOKEN_RE.finditer(masked):
            token = match.group().rstrip(_TRAILING_URL_PUNCTUATION)
            if not token.lower().startswith(("http://", "https://")):
                host = token.split("/", 1)[0].lower()
                if host.rsplit(".", 1)[-1] in _BARE_FILE_SUFFIXES:
                    continue
            add(token, source_field)

    raw = raw_result.get("raw_response")
    if not isinstance(raw, Mapping):
        raw = {}

    knowledge_graph = raw.get("knowledge_graph")
    if isinstance(knowledge_graph, Mapping):
        add(knowledge_graph.get("website"), "knowledge_graph.website")

    answer_box = raw.get("answer_box")
    if isinstance(answer_box, Mapping):
        for key in ("link", "url"):
            add(answer_box.get(key), f"answer_box.{key}")

    ai_overview = raw.get("ai_overview")
    if isinstance(ai_overview, Mapping):
        sources = ai_overview.get("ai_overview_sources")
        if isinstance(sources, list):
            for index, source in enumerate(sources):
                if isinstance(source, Mapping):
                    add(
                        source.get("source_url"),
                        f"ai_overview.ai_overview_sources[{index}].source_url",
                    )

    organic_results = raw.get("organic_results")
    if isinstance(organic_results, list):
        for index, result in enumerate(organic_results):
            if not isinstance(result, Mapping):
                continue
            for key in ("link", "url"):
                add(result.get(key), f"organic_results[{index}].{key}")

    if isinstance(ai_overview, Mapping):
        contents = ai_overview.get("ai_overview_contents")
        if isinstance(contents, list):
            for index, content in enumerate(contents):
                if isinstance(content, Mapping):
                    add_text(
                        content.get("text"),
                        f"ai_overview.ai_overview_contents[{index}].text",
                    )

    if isinstance(organic_results, list):
        for index, result in enumerate(organic_results):
            if not isinstance(result, Mapping):
                continue
            for key in ("displayed_link", "snippet"):
                add_text(result.get(key), f"organic_results[{index}].{key}")

    candidates = raw_result.get("candidates")
    if isinstance(candidates, list):
        for index, candidate in enumerate(candidates):
            add(candidate, f"candidates[{index}]")

    return records


def extract_evidence_records(
    raw_response: Mapping[str, Any],
    phase: str,
) -> list[dict[str, str]]:
    """Collect bounded overview and financially relevant organic evidence."""
    if not isinstance(raw_response, Mapping):
        return []
    records: list[dict[str, str]] = []
    ai_overview = raw_response.get("ai_overview")
    if isinstance(ai_overview, Mapping):
        contents = ai_overview.get("ai_overview_contents")
        if isinstance(contents, list):
            for index, content in enumerate(contents):
                if not isinstance(content, Mapping):
                    continue
                raw_text = content.get("text")
                if not isinstance(raw_text, str):
                    continue
                text = raw_text.strip()
                if text:
                    records.append({
                        "evidence_id": f"{phase}.overview.{index}",
                        "phase": phase,
                        "source_field": (
                            f"ai_overview.ai_overview_contents[{index}].text"
                        ),
                        "text": text[:3000],
                    })

    organic_results = raw_response.get("organic_results")
    if isinstance(organic_results, list):
        for index, result in enumerate(organic_results):
            if not isinstance(result, Mapping):
                continue
            raw_text = result.get("snippet")
            if not isinstance(raw_text, str):
                continue
            text = raw_text.strip()
            if text and _FINANCIAL_MARKER_RE.search(text):
                records.append({
                    "evidence_id": f"{phase}.organic.{index}",
                    "phase": phase,
                    "source_field": f"organic_results[{index}].snippet",
                    "text": text[:1500],
                })
    return records


def _meaningful_name_tokens(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {
        token for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 2 and token not in _NAME_STOPWORDS
    }


def has_positive_financial_evidence(
    records: Sequence[Mapping[str, object]],
    x_name: str | None = None,
    y_name: str | None = None,
) -> bool:
    """Return whether one record is financial and not explicitly negative."""
    grounding_required = x_name is not None or y_name is not None
    x_tokens = _meaningful_name_tokens(x_name) if grounding_required else set()
    y_tokens = _meaningful_name_tokens(y_name) if grounding_required else set()
    if grounding_required and (not x_tokens or not y_tokens):
        return False

    for record in records:
        if not isinstance(record, Mapping):
            continue
        raw_text = record.get("text")
        if not isinstance(raw_text, str):
            continue
        text = raw_text.strip()
        if grounding_required:
            text_tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
            if not (x_tokens & text_tokens and y_tokens & text_tokens):
                continue
        if ((_POSITIVE_FINANCIAL_RE.search(text)
             or (grounding_required and _GROUNDED_ACTIVE_FINANCIAL_RE.search(text)))
                and not _NEGATIVE_FINANCIAL_RE.search(text)):
            return True
    return False


def company_y_appears_on_x_site(
    raw_response: Mapping[str, Any],
    x_domain: str,
) -> bool:
    """Return whether an organic result points to Company X's domain."""
    if not isinstance(raw_response, Mapping):
        return False
    organic_results = raw_response.get("organic_results")
    if not isinstance(organic_results, list):
        return False
    for result in organic_results:
        if not isinstance(result, Mapping):
            continue
        for key in ("link", "url"):
            canonical = canonicalize_official_url(str(result.get(key) or ""))
            if canonical and url_matches_domain(canonical, x_domain):
                return True
    return False


def normalize_search_policy(value: object) -> SearchPolicy:
    if isinstance(value, str) and value.strip().lower() == "sequential":
        return "sequential"
    return "adaptive"


def select_next_phase(
    phases: Sequence[RelationshipPhase],
    executed: Collection[str],
    policy: SearchPolicy,
    relationship_evidenced: bool,
    valid_url_found: bool,
) -> RelationshipPhase | None:
    enabled = [phase for phase in phases if phase.enabled]
    remaining = [phase for phase in enabled if phase.name not in executed]
    if relationship_evidenced and valid_url_found or not remaining:
        return None
    if policy == "sequential":
        return remaining[0]

    if not executed:
        return remaining[0]

    remaining = [phase for phase in remaining if phase.name != enabled[0].name]
    target_goal: PhaseGoal = (
        "official_url" if relationship_evidenced else "relationship_evidence"
    )
    return next(
        (phase for phase in remaining if phase.goal in {target_goal, "combined"}),
        None,
    )


def _build_relationship_and_url_query(
    search_input: RelationshipSearchInput,
    evidence: Sequence[Mapping[str, object]],
) -> str:
    return (
        f'Company X is "{search_input.x_name}" and its official website is '
        f'"{search_input.input_url}".\n\n'
        "What documented financial relationship exists between Company X and the "
        f'company identified as "{search_input.y_name}"?\n\n'
        "Identify the exact Company Y and type its official website as one complete "
        "plain-text URL beginning with https://. Do not use hyperlink text. Do not "
        "return Company X's website. If no financial relationship is documented, "
        "say so explicitly."
    )


def _build_financial_evidence_query(
    search_input: RelationshipSearchInput,
    evidence: Sequence[Mapping[str, object]],
) -> str:
    return (
        "What documented financial relationship—such as investment, funding-round "
        "participation, portfolio ownership, acquisition, or financial backing—exists "
        f'between "{search_input.x_name}" (official website: '
        f'"{search_input.input_url}") and the company identified as '
        f'"{search_input.y_name}"?\n\n'
        "Name the exact Company Y and describe the specific transaction, funding "
        "round, portfolio listing, acquisition, or investment evidence. Mere "
        "partnership or co-mention is not sufficient. If no financial relationship "
        "is documented, say so explicitly."
    )


def _build_official_url_recovery_query(
    search_input: RelationshipSearchInput,
    evidence: Sequence[Mapping[str, object]],
) -> str:
    entries = []
    for record in evidence:
        evidence_id = str(record.get("evidence_id") or "").strip()
        text = str(record.get("text") or "").strip()
        if evidence_id and text:
            entries.append(f"[{evidence_id}] {text}")
    relationship_evidence = "\n".join(entries)[:6000]

    return (
        "The following search evidence describes a documented financial relationship "
        f'between "{search_input.x_name}" (official website: '
        f'"{search_input.input_url}") and the company identified as '
        f'"{search_input.y_name}":\n\n'
        f'"{relationship_evidence}"\n\n'
        "What is the official website of the exact Company Y described by this "
        "evidence?\n\n"
        "Return one complete plain-text URL beginning with https://. Do not use "
        "hyperlink text. Do not return Company X's website, a social profile, "
        "directory, database, news article, or search-result page."
    )


RELATIONSHIP_PHASES = (
    RelationshipPhase(
        "phase1_relationship_and_url",
        "combined",
        _build_relationship_and_url_query,
    ),
    RelationshipPhase(
        "phase2_financial_evidence",
        "relationship_evidence",
        _build_financial_evidence_query,
    ),
    RelationshipPhase(
        "phase3_official_url_recovery",
        "official_url",
        _build_official_url_recovery_query,
    ),
)
