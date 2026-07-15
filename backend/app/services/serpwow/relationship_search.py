"""Data-driven search phases for relationship-mode lookups."""
from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address
from socket import inet_aton
from typing import Any, Awaitable, Callable, Collection, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from app.services.serpwow.outcomes import categorize_http_error
from app.services.serpwow.url_utils import (
    _candidate_domain_has_company_token,
    _candidate_domain_is_plausible_for_company,
    _host_looks_like_filename,
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
_NON_WEB_SUFFIXES = frozenset({"txt"})
_BARE_FILE_SUFFIXES = frozenset({
    "cfg", "ini", "json", "md", "pdf", "sh", "toml", "txt", "yaml", "yml",
})
_CLAUSE_BOUNDARY_RE = re.compile(
    r"[.;\n]+|,\s*(?:and|then)\b|"
    r"\b(?:but|however|although|though|yet|whereas)\b",
    re.IGNORECASE,
)
_OFFICIAL_SITE_RE = re.compile(
    r"\b(?:official\s+(?:web)?site|(?:web)?site\s+is)\b", re.IGNORECASE)
_FINANCIAL_MARKER_RE = re.compile(
    r"\binvest(?:s|ed|ing|ment(?:s)?|or(?:s)?)?\b|"
    r"\bfund(?:s|ed|ing)?\b|"
    r"\bfinanc(?:e|es|ed|ing)\b|"
    r"\b(?:financial\s+)?backing\b|\bfinancially\s+backs?\b|"
    r"\bportfolio\b|\bbacked\b|"
    r"\bacquisition(?:s)?\b|\bacquired\b|"
    r"\bparent\s+company\s+of\b|\b\w+['’]s\s+parent\s+company\b|"
    r"\bsubsidiar(?:y|ies)\s+of\b|\bis\s+(?:an?\s+)?\w+\s+subsidiary\b|"
    r"\bownership\b|"
    r"\bseries\s+[a-z0-9]+\b",
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
    r"\bparent\s+company\s+of\b|\b\w+['’]s\s+parent\s+company\b|"
    r"\bsubsidiar(?:y|ies)\s+of\b|\bis\s+(?:an?\s+)?\w+\s+subsidiary\b|"
    r"\bownership\s+(?:of|in)\b|"
    r"\b(?:led|participated\s+in)\b[^.;\n]{0,80}\bseries\s+[a-z0-9]+\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|cannot|can['’]t|couldn['’]t|won['’]t|"
    r"wouldn['’]t|isn['’]t|wasn['’]t|didn['’]t|doesn['’]t|don['’]t)\b",
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
SearchCallable = Callable[[str], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class RelationshipPhase:
    name: str
    goal: PhaseGoal
    build_query: QueryBuilder
    enabled: bool = True


@dataclass
class RelationshipSearchResult:
    executed_phases: list[str]
    queries: list[tuple[str, str]]
    candidates: list[str]
    candidate_evidence: list[dict[str, str]]
    evidence: list[dict[str, str]]
    search_attempts: list[dict[str, Any]]
    formatted_results: list[dict[str, Any]]
    relationship_evidenced: bool
    x_site_hit: bool
    successful_requests: int

    @property
    def request_count(self) -> int:
        return len(self.queries)


def _source_declares_candidate_official(source_text: str, canonical: str) -> bool:
    if not source_text or not _OFFICIAL_SITE_RE.search(source_text):
        return False
    host = urlsplit(canonical).hostname or ""
    if not host:
        return False
    official = r"(?:official\s+(?:web)?site|(?:web)?site\s+is)"
    escaped_host = re.escape(host.removeprefix("www."))
    return bool(re.search(
        rf"(?:{official}.{{0,80}}{escaped_host}|{escaped_host}.{{0,80}}{official})",
        source_text,
        re.IGNORECASE,
    ))


def extract_candidate_records(
    raw_result: Mapping[str, Any],
    phase: str,
    x_domain: str,
    y_name: str = "",
    country: str = "",
) -> list[dict[str, str]]:
    """Extract canonical URL candidates with first-seen field provenance."""
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(
        value: object,
        source_field: str,
        *,
        authoritative: bool = False,
        source_text: str = "",
        strict_y_host: bool = False,
    ) -> None:
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
        except ValueError:
            return
        if parsed_host:
            host = parsed_host.lower()
            labels = host.split(".")
            suffix = labels[-1]
            bare = "://" not in original
            if (suffix in _NON_WEB_SUFFIXES
                    or _host_looks_like_filename(host)
                    or (bare and suffix in _BARE_FILE_SUFFIXES)):
                return
            try:
                ip_address(host)
                return
            except ValueError:
                try:
                    inet_aton(host)
                except (OSError, UnicodeError):
                    pass
                else:
                    return
        canonical = canonicalize_official_url(original)
        if (not canonical or canonical in seen
                or is_disallowed_official_url(canonical)
                or url_matches_domain(canonical, x_domain)):
            return
        y_host_relevant = (
            _candidate_domain_has_company_token(canonical, y_name)
            if strict_y_host
            else _candidate_domain_is_plausible_for_company(
                canonical, y_name, country)
        )
        if (not authoritative and y_name and not y_host_relevant
                and not _source_declares_candidate_official(
                    source_text, canonical)):
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
            add(token, source_field, source_text=value)

    raw = raw_result.get("raw_response")
    if not isinstance(raw, Mapping):
        raw = {}

    knowledge_graph = raw.get("knowledge_graph")
    if isinstance(knowledge_graph, Mapping):
        add(knowledge_graph.get("website"), "knowledge_graph.website",
            authoritative=True)

    answer_box = raw.get("answer_box")
    if isinstance(answer_box, Mapping):
        for key in ("link", "url"):
            add(answer_box.get(key), f"answer_box.{key}", authoritative=True)

    ai_overview = raw.get("ai_overview")

    organic_results = raw.get("organic_results")
    if isinstance(organic_results, list):
        for index, result in enumerate(organic_results):
            if not isinstance(result, Mapping):
                continue
            source_text = " ".join(
                str(result.get(key) or "")
                for key in ("title", "displayed_link", "snippet"))
            for key in ("link", "url"):
                add(result.get(key), f"organic_results[{index}].{key}",
                    source_text=source_text, strict_y_host=True)

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
            add(candidate, f"candidates[{index}]", strict_y_host=True)

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


def has_positive_financial_evidence(
    records: Sequence[Mapping[str, object]],
    x_name: str | None = None,
    y_name: str | None = None,
) -> bool:
    """Return whether one record is financial and not explicitly negative."""
    grounding_required = x_name is not None or y_name is not None
    grounded_re: re.Pattern[str] | None = None
    if grounding_required:
        if not isinstance(x_name, str) or not isinstance(y_name, str):
            return False
        x_folded = " ".join(x_name.casefold().split())
        y_folded = " ".join(y_name.casefold().split())
        if not x_folded or not y_folded:
            return False
        x = rf"(?<!\w){re.escape(x_folded)}(?!\w)"
        y = rf"(?<!\w){re.escape(y_folded)}(?!\w)"
        possessive = r"['’]s"
        series_round = r"[\w-]+"
        grounded_re = re.compile("|".join((
            rf"{x}\s+(?:invested|invests)\s+in\s+{y}",
            rf"{x}\s+is\s+(?:an?\s+)?investor\s+in\s+{y}",
            rf"{y}\s+(?:(?:was|is)\s+)?(?:funded|financed|backed)\s+by\s+{x}",
            rf"{x}\s+(?:funded|financed|backed|acquired)\s+{y}",
            rf"{y}\s+(?:was\s+)?acquired\s+by\s+{x}",
            rf"{x}\s+(?:led|participated\s+in)\s+{y}{possessive}\s+series\s+{series_round}",
            rf"{y}{possessive}\s+series\s+{series_round}"
            rf"[^.;\n]{{0,60}}\b(?:led|participation)\s+by\s+{x}",
            rf"{y}\s+is\s+(?:a\s+)?portfolio\s+company\s+of\s+{x}",
            rf"{x}(?:{possessive})?\s+portfolio\s+includes\s+{y}",
            rf"{x}\s+is\s+(?:the\s+)?parent(?:\s+company)?\s+of\s+{y}",
            rf"{y}\s+is\s+(?:a\s+)?subsidiary\s+of\s+{x}",
            rf"{x}\s+is\s+{y}{possessive}\s+parent\s+company",
            rf"{y}\s+is\s+{x}{possessive}\s+subsidiary",
        )))

    for record in records:
        if not isinstance(record, Mapping):
            continue
        raw_text = record.get("text")
        if not isinstance(raw_text, str):
            continue
        for clause in _CLAUSE_BOUNDARY_RE.split(raw_text.strip()):
            clause = clause.strip()
            if not clause or _NEGATION_RE.search(clause):
                continue
            positive = (
                bool(grounded_re.search(" ".join(clause.casefold().split())))
                if grounded_re is not None
                else bool(_POSITIVE_FINANCIAL_RE.search(clause))
            )
            if positive:
                return True
    return False


def trusted_positive_record_ids(
    records: Sequence[Mapping[str, object]],
    x_name: str | None = None,
    y_name: str | None = None,
) -> set[str]:
    """Return unique IDs whose own record positively grounds the relationship."""
    trusted: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        evidence_id = str(record.get("evidence_id") or "").strip()
        if (evidence_id and has_positive_financial_evidence(
                [record], x_name=x_name, y_name=y_name)):
            trusted.add(evidence_id)
    return trusted


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


async def run_relationship_phases(
    search: SearchCallable,
    inputs: RelationshipSearchInput,
    policy: SearchPolicy,
    max_requests: int,
    phases: Sequence[RelationshipPhase] = RELATIONSHIP_PHASES,
) -> RelationshipSearchResult:
    """Run relationship phases one request at a time until complete or capped."""
    cap = max(1, int(max_requests))
    executed_phases: list[str] = []
    queries: list[tuple[str, str]] = []
    candidates: list[str] = []
    candidate_evidence: list[dict[str, str]] = []
    evidence: list[dict[str, str]] = []
    search_attempts: list[dict[str, Any]] = []
    formatted_results: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    relationship_evidenced = False
    x_site_hit = False
    successful_requests = 0

    while len(queries) < cap:
        phase = select_next_phase(
            phases,
            executed_phases,
            normalize_search_policy(policy),
            relationship_evidenced,
            bool(candidates),
        )
        if phase is None:
            break

        query = phase.build_query(inputs, evidence)
        executed_phases.append(phase.name)
        queries.append((phase.name, query))
        try:
            response = await search(query)
            if not isinstance(response, Mapping):
                raise TypeError("search result must be a mapping")
            raw_result = dict(response)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raw_result = {
                "provider": "serpwow",
                "used": False,
                "query": query,
                "official_website": None,
                "candidates": [],
                "status_code": getattr(exc, "status_code", None),
                "search_url": None,
                "raw_response": None,
                "error": error,
                "error_category": categorize_http_error(
                    getattr(exc, "status_code", None), error),
            }

        successful_requests += int(bool(raw_result.get("used")))
        raw_response = raw_result.get("raw_response")
        if not isinstance(raw_response, Mapping):
            raw_response = {}

        phase_candidates = extract_candidate_records(
            raw_result, phase.name, inputs.x_domain,
            y_name=inputs.y_name, country=inputs.country)
        candidate_evidence.extend(phase_candidates)
        for record in phase_candidates:
            url = record["url"]
            if url not in seen_candidates:
                seen_candidates.add(url)
                candidates.append(url)

        evidence.extend(extract_evidence_records(raw_response, phase.name))
        x_site_hit = x_site_hit or company_y_appears_on_x_site(
            raw_response, inputs.x_domain)
        relationship_evidenced = has_positive_financial_evidence(
            evidence, inputs.x_name, inputs.y_name)

        formatted_results.append({
            "phase": phase.name,
            "query": query,
            "success": bool(raw_result.get("used")),
            "error": raw_result.get("error"),
            "error_category": raw_result.get("error_category"),
            "status_code": raw_result.get("status_code"),
            "search_url": raw_result.get("search_url"),
            "raw_response": raw_result.get("raw_response"),
        })
        search_attempts.append({
            "attempt": phase.name,
            "query": query,
            "search_url": raw_result.get("search_url"),
            "status": (
                "candidates_found" if phase_candidates else "no_candidates"
            ),
            "status_code": raw_result.get("status_code"),
            "error": raw_result.get("error"),
        })

    return RelationshipSearchResult(
        executed_phases=executed_phases,
        queries=queries,
        candidates=candidates,
        candidate_evidence=candidate_evidence,
        evidence=evidence,
        search_attempts=search_attempts,
        formatted_results=formatted_results,
        relationship_evidenced=relationship_evidenced,
        x_site_hit=x_site_hit,
        successful_requests=successful_requests,
    )
