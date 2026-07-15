"""Data-driven search phases for relationship-mode lookups."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Collection, Literal, Mapping, Sequence


PhaseGoal = Literal["combined", "relationship_evidence", "official_url"]
SearchPolicy = Literal["adaptive", "sequential"]


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
