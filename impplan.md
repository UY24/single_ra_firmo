# website_url_finder Rework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify CSV input across all pipelines, restructure the backend into a proper package, add Supabase company tracking to every pipeline, ship two AI modes (`ai_bulk` / `ai_deep`) on one engine with a unified confidence/flags/attempt_log output, and replace the monolithic UI with a structured FastAPI-served HTML app.

**Architecture:** FastAPI backend restructured into `backend/app/{core,routers,services,models,prompts,static,templates}` via pure-move-then-modify steps. The existing AI Mode two-phase engine (parallel scrape-all → LLM clean-all → assemble) is kept and parameterized by a `ModeConfig`. Supabase (two tables: `companies`, `runs`) is accessed ONLY by FastAPI and is bookkeeping-only — it never fails a run. UI is static HTML/JS served by FastAPI, no build step.

**Tech Stack:** Python 3.12 (`.venv/` at `website_url_finder/`), FastAPI, httpx, `supabase` (supabase-py v2), unittest (NO pytest in the venv), Tailwind via CDN, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-06-11-rework-design.md` — read it first. HANDOFF context: `HANDOFF.md`.

---

## Executor context (read before Task 0)

- Working dir for all commands: `/Users/ujjwalyadav/coding/forage/website_url_finder` (called `$ROOT` below).
- **Not a git repo yet** — Task 0 fixes that. Commit after every task.
- Tests: `cd $ROOT/backend && ../.venv/bin/python -m unittest discover -s tests -v` (after Task 2 moves things into `backend/`; before that, `cd $ROOT && .venv/bin/python -m unittest discover -s tests -v`). pytest is NOT installed.
- Import smoke check (after Task 2): `cd $ROOT/backend && ../.venv/bin/python -c "import app.main"`.
- Run the server (after Task 2): `cd $ROOT/backend && ../.venv/bin/python -m app.main` → `http://localhost:8080/ui` (port from `$ROOT/.env`, `API_PORT=8080`). RabbitMQ is optional (startup wraps it in try/except).
- The SerpWow pipelines (gmaps/gsearch/full/firmographics/url_discovery, RabbitMQ, S3, XLSX) must keep working. They are moved, not rewritten.
- `.env` lives at `$ROOT/.env` and must keep being loaded after the restructure.
- Existing tests that must keep passing throughout: `tests/test_timing_summary.py`, `tests/test_gemini_batch.py` (17 tests).
- When a step says "grep", run it and read the hits before editing — line numbers in this plan drift as files move.

### Pure-move discipline (Tasks 2–5)

Every move task: move code verbatim (no renames of functions, no logic edits), fix imports/paths only, then run the import smoke check + full test suite before committing. Behavior changes start at Task 6.

---

## Phase 0 — Baseline

### Task 0: git init + baseline commit

**Files:**
- Create: `$ROOT/.gitignore`

- [ ] **Step 1: Create `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.env
ai_mode_result/
ai_mode_results/
*.xlsx
.DS_Store
```

- [ ] **Step 2: Init repo and commit baseline**

```bash
cd /Users/ujjwalyadav/coding/forage/website_url_finder
git init
git add -A
git commit -m "chore: baseline before rework"
```

### Task 1: Clean root clutter

**Files:**
- Create: `$ROOT/samples/` (move sample/test CSVs into it)
- Delete: stray run artifacts at root

- [ ] **Step 1: Move sample inputs, delete artifacts**

```bash
cd $ROOT
mkdir -p samples
git mv sample_company.csv sample_address.csv mediumtest.csv smalltest.csv smalltest1.csv smalltestfailed.csv poc1kresidue191.csv poc_1k_sample.csv samples/ 2>/dev/null || true
git rm -f caa8a70f-*.xlsx d6584620-*.xlsx d6584620-*_googlephases.csv fc1c7ff1-*.xlsx output.xlsx smalltestfailed.csv.xlsx input.json 2>/dev/null || true
```

(If any file is already gitignored/untracked, plain `mv`/`rm` it.) Keep `AI descriptions requirements.pdf`, `convertjtox.py`, `batch_runner.py` — check `git grep -l convertjtox batch_runner` for references first; if nothing references them and they're standalone one-off scripts, move them to `scripts/`.

- [ ] **Step 2: Verify nothing imports the moved files**

```bash
grep -rn "sample_company\|smalltest\|poc_1k" --include="*.py" . | grep -v samples/
```
Expected: no hits (docs/HANDOFF mentions are fine).

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "chore: move sample CSVs to samples/, delete stray run artifacts"
```

---

## Phase 1 — Backend package skeleton & pure moves

### Task 2: Create `backend/` package and move everything (pure move)

**Files:**
- Create: `backend/app/__init__.py`, `backend/app/main.py`, `backend/app/core/__init__.py`, `backend/app/core/config.py`, `backend/app/routers/__init__.py`, `backend/app/services/__init__.py`, `backend/app/services/serpwow/__init__.py`, `backend/app/services/ai_mode/__init__.py`, `backend/app/models/__init__.py`, `backend/app/prompts/` (dir)
- Move: `app.py` → `backend/app/services/serpwow/legacy_app.py`; `worker.py`, `gmaps.py`, `codetails.py` → `backend/app/services/serpwow/`; `ai_mode_service.py`, `gemini_batch.py` → `backend/app/services/ai_mode/`; `scrapedo_finder/` → `backend/app/services/ai_mode/scrapedo_finder/` (flattened in Task 4); `templates/` → `backend/app/templates/`; `tests/` → `backend/tests/`; `requirements.txt` → `backend/requirements.txt`; `scripts/` → `backend/scripts/`

- [ ] **Step 1: Create skeleton + `core/config.py`**

```python
# backend/app/core/config.py
"""Single source of truth for paths and env loading."""
from pathlib import Path
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent          # backend/app
BACKEND_DIR = APP_DIR.parent                              # backend
PROJECT_ROOT = BACKEND_DIR.parent                         # website_url_finder
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
PROMPTS_DIR = APP_DIR / "prompts"
AI_MODE_RESULTS_DIR = PROJECT_ROOT / "ai_mode_results"    # new layout (Task 10)
LEGACY_AI_MODE_RESULT_DIR = PROJECT_ROOT / "ai_mode_result"

load_dotenv(PROJECT_ROOT / ".env")
```

- [ ] **Step 2: `git mv` the files per the list above**

- [ ] **Step 3: Fix path/import breakage**

In `legacy_app.py` and `ai_mode_service.py`, find every relative filesystem path and route it through `app.core.config`:

```bash
cd $ROOT/backend && grep -n "templates\|ai_mode_result\|os.path\|Path(\|open(" app/services/serpwow/legacy_app.py app/services/ai_mode/ai_mode_service.py | grep -v "^.*#"
```

Typical fixes: `"templates/ui.html"` → `str(TEMPLATES_DIR / "ui.html")`; `ai_mode_result` base dir → `LEGACY_AI_MODE_RESULT_DIR` (keep old runs readable). Fix intra-repo imports: `import ai_mode_service` → `from app.services.ai_mode import ai_mode_service`; `from scrapedo_finder...` → `from app.services.ai_mode.scrapedo_finder...`; `import gemini_batch` → `from app.services.ai_mode import gemini_batch`; same for `gmaps`, `codetails`, `worker`. Update the two test files' imports likewise.

- [ ] **Step 4: Create `backend/app/main.py`**

```python
# backend/app/main.py
"""App entrypoint. The SerpWow monolith still owns the FastAPI instance; new
routers are attached to it here. Run: cd backend && ../.venv/bin/python -m app.main"""
import os

from app.core import config  # noqa: F401  (loads .env first)
from app.services.serpwow.legacy_app import app  # the existing FastAPI instance

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("API_PORT", "11500")),
        reload=os.getenv("API_RELOAD", "false").lower() == "true",
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
    )
```

Remove the `if __name__ == "__main__":` block from `legacy_app.py` (it moved here).

- [ ] **Step 5: Verify**

```bash
cd $ROOT/backend && ../.venv/bin/python -c "import app.main" && ../.venv/bin/python -m unittest discover -s tests -v
```
Expected: import OK, 17/17 pass. Also boot the server once and load `/ui` to confirm the template still renders.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: move backend into backend/app package (pure move)"
```

### Task 3: Delete dead scrapedo files, fix broken scripts

**Files:**
- Delete: `backend/app/services/ai_mode/scrapedo_finder/{__main__.py,cli.py,cleanup_cli.py,runner.py,cleanup_runner.py,company_cleanup_runner.py,reporting.py,csv_loader.py}`
- Modify: `backend/scripts/push_processed_rows_to_gemini_batch.py`, `backend/scripts/requeue_wait_and_push_remaining_to_gemini_batch.py`

- [ ] **Step 1: Confirm the 8 files are unreferenced, then delete**

```bash
cd $ROOT/backend
for f in __main__ cli cleanup_cli runner cleanup_runner company_cleanup_runner reporting csv_loader; do echo "== $f"; grep -rn "scrapedo_finder.$f\|from .$f\|from scrapedo_finder import.*$f" --include="*.py" app tests scripts | grep -v "scrapedo_finder/$f.py"; done
```
Expected: no hits (note `cleanup_reporting`/`company_reporting` are DIFFERENT files — keep them). Then `git rm` the 8 files.

- [ ] **Step 2: Fix `app.upload_lock` → `app.get_upload_lock(upload_id)` in both scripts** (grep `upload_lock` in `backend/scripts/`; the legacy module path also changed — update their imports to `app.services.serpwow.legacy_app`).

- [ ] **Step 3: Verify + commit**

```bash
cd $ROOT/backend && ../.venv/bin/python -c "import app.main" && ../.venv/bin/python -m unittest discover -s tests -v
git add -A && git commit -m "refactor: delete dead scrapedo CLI layer, fix recovery scripts"
```

### Task 4: Flatten `scrapedo_finder/` into `services/ai_mode/` (pure move)

**Files:**
- Move: `scrapedo_finder/{scrapedo_client.py,llm_client.py,models.py,settings.py,extraction.py,company_extraction.py,prompting.py,company_prompting.py,cleanup_reporting.py,company_reporting.py,company_csv_loader.py}` → `backend/app/services/ai_mode/`
- Move: `scrapedo_finder/prompts/*.txt` → `backend/app/prompts/`
- Delete: empty `scrapedo_finder/` dir + its `__init__.py`

- [ ] **Step 1: `git mv` files, update all imports** (`from app.services.ai_mode.scrapedo_finder.X` → `from app.services.ai_mode.X`; prompt-loading paths in `prompting.py`/`company_prompting.py` → `PROMPTS_DIR / "search_query_template.txt"` etc. via `app.core.config`).

- [ ] **Step 2: Verify + commit** (same import + test commands as Task 3 Step 3). Commit: `"refactor: flatten scrapedo_finder into services/ai_mode"`.

### Task 5: Extract AI-mode endpoints into `routers/ai_mode.py` (pure move)

**Files:**
- Create: `backend/app/routers/ai_mode.py`
- Modify: `backend/app/services/serpwow/legacy_app.py` (remove the 4 AI-mode endpoints), `backend/app/main.py`

- [ ] **Step 1: Move the 4 endpoints** (`POST /uploads/ai-mode`, `GET /uploads/ai-mode`, `GET /uploads/ai-mode/{run_id}/status`, `GET /uploads/ai-mode/{run_id}/result`) and the module-level `ai_mode_tasks` set out of `legacy_app.py` into:

```python
# backend/app/routers/ai_mode.py
from fastapi import APIRouter

router = APIRouter()
ai_mode_tasks: set = set()

# ... the 4 endpoint functions moved verbatim, decorated with @router.post/@router.get
```

In `main.py` add:

```python
from app.routers.ai_mode import router as ai_mode_router
app.include_router(ai_mode_router)
```

- [ ] **Step 2: Verify (import + tests + boot server, upload a sample via the existing UI tab) + commit** `"refactor: extract AI-mode endpoints to routers/ai_mode"`.

---

## Phase 2 — Unified input format (TDD from here on)

### Task 6: Canonical CSV parser in `models/entities.py`

**Files:**
- Create: `backend/app/models/entities.py`
- Test: `backend/tests/test_entities.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_entities.py
import unittest
from app.models.entities import Entity, InvalidCSVError, parse_entities_csv

class TestParseEntitiesCSV(unittest.TestCase):
    def test_canonical_headers(self):
        csv = "company_name,country\nAcme KK,Japan\n"
        parsed = parse_entities_csv(csv)
        self.assertEqual(len(parsed.entities), 1)
        e = parsed.entities[0]
        self.assertEqual((e.company_name, e.country, e.sno), ("Acme KK", "Japan", 1))
        self.assertIsNone(e.company_local_name)

    def test_aliases_and_optionals(self):
        csv = ("Company,Nation,Local Name,Address,FirmID,Industry\n"
               "Acme,Japan,アクメ,Tokyo 1-2-3,F1,Manufacturing\n")
        e = parse_entities_csv(csv).entities[0]
        self.assertEqual(e.company_local_name, "アクメ")
        self.assertEqual(e.address, "Tokyo 1-2-3")
        self.assertEqual(e.firm_id, "F1")
        self.assertEqual(e.industry, "Manufacturing")

    def test_old_company_mode_format_rejected(self):
        csv = "Company Name ENG,Company Name Local,Country Code,ISIC\nAcme,アクメ,JP,2200\n"
        with self.assertRaises(InvalidCSVError) as ctx:
            parse_entities_csv(csv)
        self.assertIn("company_name", str(ctx.exception))

    def test_positional_fallback_when_headerless(self):
        csv = "Acme KK,Japan\nBeta GmbH,Germany\n"
        parsed = parse_entities_csv(csv)
        self.assertEqual(len(parsed.entities), 2)
        self.assertTrue(any("positional" in w for w in parsed.warnings))

    def test_empty_rows_skipped_and_zero_rows_rejected(self):
        with self.assertRaises(InvalidCSVError):
            parse_entities_csv("company_name,country\n,\n")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL** — `cd $ROOT/backend && ../.venv/bin/python -m unittest tests.test_entities -v` → `ModuleNotFoundError: app.models.entities`.

- [ ] **Step 3: Implement**

```python
# backend/app/models/entities.py
"""The ONE canonical CSV input format, used by every pipeline (spec §3)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

COMPANY_ALIASES = {"company_name", "company", "name", "entity_name", "entity",
                   "organization", "organisation", "legal_name"}
COUNTRY_ALIASES = {"country", "country_name", "nation"}
LOCAL_NAME_ALIASES = {"company_local_name", "local_name", "company_name_local", "name_local"}
ADDRESS_ALIASES = {"full_address", "address", "fulladdress", "input_full_address"}
FIRM_ID_ALIASES = {"firm_id", "firmid", "id"}
INDUSTRY_ALIASES = {"industry", "input_industry"}

REQUIRED_MESSAGE = (
    "CSV must contain a company name column (accepted: company_name, company, name, "
    "entity_name, entity, organization, organisation, legal_name) and a country column "
    "(accepted: country, country_name, nation). Optional: company_local_name/local_name, "
    "address/full_address, firm_id/id, industry. Headerless 2+ column files are parsed "
    "positionally (col 1 = company, col 2 = country)."
)


class InvalidCSVError(ValueError):
    pass


@dataclass
class Entity:
    company_name: str
    country: str
    sno: int
    company_local_name: str | None = None
    address: str | None = None
    firm_id: str | None = None
    industry: str | None = None


@dataclass
class ParsedCSV:
    entities: list[Entity]
    columns_detected: dict[str, str]   # canonical field -> original header
    warnings: list[str] = field(default_factory=list)
    positional: bool = False


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (header or "").strip().lower()).strip("_")


def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for original in fieldnames:
        n = _norm(original)
        for canon, aliases in (
            ("company_name", COMPANY_ALIASES), ("country", COUNTRY_ALIASES),
            ("company_local_name", LOCAL_NAME_ALIASES), ("address", ADDRESS_ALIASES),
            ("firm_id", FIRM_ID_ALIASES), ("industry", INDUSTRY_ALIASES),
        ):
            if n in aliases and canon not in mapping:
                mapping[canon] = original
    return mapping


def parse_entities_csv(raw: str | bytes) -> ParsedCSV:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(raw)))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        raise InvalidCSVError("CSV is empty. " + REQUIRED_MESSAGE)

    mapping = _resolve_columns(rows[0])
    warnings: list[str] = []
    entities: list[Entity] = []

    if "company_name" in mapping and "country" in mapping:
        reader = csv.DictReader(io.StringIO(raw))
        sno = 0
        for record in reader:
            name = (record.get(mapping["company_name"]) or "").strip()
            country = (record.get(mapping["country"]) or "").strip()
            if not name:
                continue
            sno += 1

            def opt(canon: str) -> str | None:
                header = mapping.get(canon)
                value = (record.get(header) or "").strip() if header else ""
                return value or None

            entities.append(Entity(
                company_name=name, country=country, sno=sno,
                company_local_name=opt("company_local_name"), address=opt("address"),
                firm_id=opt("firm_id"), industry=opt("industry"),
            ))
        positional = False
    else:
        if max(len(r) for r in rows) < 2:
            raise InvalidCSVError("Unrecognized CSV format. " + REQUIRED_MESSAGE)
        warnings.append("No recognized headers found; positional parsing used "
                        "(column 1 = company name, column 2 = country).")
        mapping = {"company_name": "<col 1>", "country": "<col 2>"}
        entities = [
            Entity(company_name=r[0].strip(), country=(r[1].strip() if len(r) > 1 else ""), sno=i)
            for i, r in enumerate(rows, start=1) if (r and r[0].strip())
        ]
        positional = True

    if not entities:
        raise InvalidCSVError("No valid rows found. " + REQUIRED_MESSAGE)
    return ParsedCSV(entities=entities, columns_detected=mapping,
                     warnings=warnings, positional=positional)
```

- [ ] **Step 4: Run tests, verify PASS**, then run the FULL suite.

- [ ] **Step 5: Commit** `"feat: canonical CSV entity parser (unified input format)"`.

### Task 7: `{entities}` prompt-block formatter

**Files:**
- Modify: `backend/app/models/entities.py` (append)
- Test: `backend/tests/test_entities.py` (append)

- [ ] **Step 1: Write failing tests**

```python
class TestFormatEntitiesForPrompt(unittest.TestCase):
    def test_minimal_fields(self):
        from app.models.entities import Entity, format_entities_for_prompt
        block = format_entities_for_prompt([Entity("Acme KK", "Japan", 1)])
        self.assertEqual(block, "1. Acme KK — Japan")

    def test_all_optional_fields_appended_only_when_present(self):
        from app.models.entities import Entity, format_entities_for_prompt
        e = Entity("Acme KK", "Japan", 1, company_local_name="アクメ株式会社",
                   address="1-2-3 Shibuya, Tokyo", industry="manufacturing")
        block = format_entities_for_prompt([e])
        self.assertEqual(block, "1. Acme KK (local: アクメ株式会社) — Japan — "
                                "1-2-3 Shibuya, Tokyo — industry: manufacturing")
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement (append to `entities.py`)**

```python
def format_entities_for_prompt(entities: list[Entity]) -> str:
    """Builds the {entities} block. Optional fields appear only when present (spec §3)."""
    lines = []
    for e in entities:
        line = e.company_name
        if e.company_local_name:
            line += f" (local: {e.company_local_name})"
        line += f" — {e.country}"
        if e.address:
            line += f" — {e.address}"
        if e.industry:
            line += f" — industry: {e.industry}"
        lines.append(f"{e.sno}. {line}")
    return "\n".join(lines)
```

- [ ] **Step 4: Tests PASS + full suite. Commit** `"feat: entities prompt-block formatter"`.

---

## Phase 3 — Unified output schema & prompts as files

### Task 8: Prompt files

**Files:**
- Create: `backend/app/prompts/ai_bulk_search.txt`, `backend/app/prompts/ai_deep_search.txt`, `backend/app/prompts/ai_cleanup.txt`
- Delete (after Task 9 wires the new ones): `backend/app/prompts/search_query_template.txt`, `backend/app/prompts/company_search_template.txt`

- [ ] **Step 1: `ai_bulk_search.txt`** (placeholder wording — user refines later; adapted from today's company template intent):

```text
Find the official corporate website for EACH of the following companies. Work through
the whole list; do not skip any entry.

Companies:
{entities}

For each company:
- Search by the English name plus country; if a local-language name is given, search it too.
- The official website is the company's own domain — NOT social networks, maps listings,
  business directories, marketplaces, or news articles.
- Prefer domains whose TLD or content matches the company's country.

For EACH company report:
- The official website URL (or state clearly that none was found)
- A confidence score from 0-100
- Flags explaining the decision (e.g. name_match, local_name_match, tld_match,
  country_mismatch, only_social, directory_only, multiple_candidates, no_results),
  each with a short reason
- Every search attempt you made: the query, what you found, and any URL
```

- [ ] **Step 2: `ai_deep_search.txt`** (placeholder — thorough multi-angle):

```text
You are an OSINT researcher. For EACH of the following companies, do a thorough,
multi-angle investigation to find the official corporate website.

Companies:
{entities}

Investigate each company through MULTIPLE angles before concluding:
1. Search the English name + country; then the local-language name if provided.
2. Search national business registries and chamber-of-commerce style sources for the
   company, and look for a website listed there.
3. Try likely domain patterns (company name + country TLD, .com) and check whether the
   domain actually belongs to this company (matching name, address, or industry).
4. Cross-check candidates against the provided address/industry where available.
5. If results conflict, prefer the domain confirmed by the most independent sources.

The official website is the company's own domain — NOT social networks, maps listings,
business directories, marketplaces, or news articles.

For EACH company report:
- The official website URL (or state clearly that none was found)
- A confidence score from 0-100
- Flags explaining the decision (e.g. name_match, local_name_match, tld_match,
  registry_confirmed, country_mismatch, only_social, directory_only,
  multiple_candidates, no_results), each with a short reason
- EVERY attempt you made: the query or check, what you found, and any URL
```

- [ ] **Step 3: `ai_cleanup.txt`** (the LLM cleanup system prompt, moved OUT of Python; aligned to the unified schema — fixes HANDOFF §6a):

```text
You clean up raw Google AI Mode search-response text. The text contains research notes
about a numbered list of companies. Extract a structured result for EVERY input company.

Return ONLY a JSON array, one object per input company, in input order:
[
  {
    "sno": <int, the company's number from the input list>,
    "company_name": "<input English name, unchanged>",
    "company_local_name": "<input local name, unchanged, or null>",
    "country": "<input country, unchanged>",
    "website_url": "<official website URL or null>",
    "confidence": <int 0-100>,
    "flags": [{"flag": "<short_tag>", "why": "<short reason>"}],
    "attempt_log": [{"query": "<search query or check performed>",
                     "result": "<short outcome>", "url": "<url or null>"}]
  }
]

Rules:
- website_url must be the company's OWN official domain. Never return Google Maps,
  social networks (facebook/linkedin/instagram/x), directories, marketplaces, or news
  sites as the official website — if only those were found, set website_url to null and
  add a flag such as only_social or directory_only.
- Include ALL flags that influenced the decision, each with a short reason. Common tags:
  name_match, local_name_match, tld_match, registry_confirmed, country_mismatch,
  only_social, directory_only, multiple_candidates, no_results.
- attempt_log must include EVERY attempt mentioned in the text for that company, in order.
- If the text contains nothing about a company, return it with website_url null,
  confidence 0, flags [{"flag": "no_results", "why": "no data in response"}], and an
  empty attempt_log.
- Output the JSON array only - no prose, no markdown fences.
```

- [ ] **Step 4: Commit** `"feat: AI mode prompt files (bulk/deep search + unified cleanup)"`.

### Task 9: `EntityResult` model + CSV serialization + unified extraction

**Files:**
- Create: `backend/app/models/results.py`
- Create: `backend/app/services/ai_mode/cleanup.py` (replaces `extraction.py` + `company_extraction.py`)
- Test: `backend/tests/test_results.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_results.py
import unittest
from app.models.results import AttemptLogEntry, EntityResult, Flag

class TestEntityResultSerialization(unittest.TestCase):
    def _result(self):
        return EntityResult(
            company_name="Acme KK", company_local_name="アクメ", country="Japan",
            website_url="https://acme.jp", confidence=85,
            flags=[Flag("name_match", "exact ENG match"), Flag("tld_match", ".jp matches country")],
            attempt_log=[AttemptLogEntry("Acme KK Japan official site", "found acme.jp", "https://acme.jp"),
                         AttemptLogEntry("アクメ 株式会社", "same domain confirmed", None)],
            sno=1)

    def test_flags_csv(self):
        self.assertEqual(self._result().flags_csv(),
                         "name_match: exact ENG match; tld_match: .jp matches country")

    def test_attempt_log_csv(self):
        self.assertEqual(self._result().attempt_log_csv(),
                         "1. Acme KK Japan official site → found acme.jp [https://acme.jp]\n"
                         "2. アクメ 株式会社 → same domain confirmed")

    def test_from_llm_object_tolerates_missing_fields(self):
        r = EntityResult.from_llm_object({"sno": 2, "company_name": "Beta"},
                                         fallback_country="Germany")
        self.assertEqual((r.sno, r.country, r.confidence, r.website_url),
                         (2, "Germany", 0, None))

class TestCleanupMessages(unittest.TestCase):
    def test_build_messages_uses_prompt_file(self):
        from app.services.ai_mode.cleanup import build_cleanup_messages
        msgs = build_cleanup_messages("RAW RESPONSE TEXT")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("attempt_log", msgs[0]["content"])
        self.assertIn("RAW RESPONSE TEXT", msgs[1]["content"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement `models/results.py`**

```python
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

    def flags_csv(self) -> str:
        return "; ".join(f"{f.flag}: {f.why}" for f in self.flags)

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
```

- [ ] **Step 4: Implement `services/ai_mode/cleanup.py`**

```python
# backend/app/services/ai_mode/cleanup.py
"""Unified LLM cleanup: prompt from prompts/ai_cleanup.txt, results as EntityResult.
Replaces extraction.py + company_extraction.py."""
from __future__ import annotations

from app.core.config import PROMPTS_DIR
from app.models.entities import Entity, format_entities_for_prompt
from app.models.results import EntityResult

_SYSTEM_PROMPT = (PROMPTS_DIR / "ai_cleanup.txt").read_text(encoding="utf-8")


def build_cleanup_messages(raw_response_text: str, entities: list[Entity] | None = None) -> list[dict]:
    user = ""
    if entities:
        user += "Input companies:\n" + format_entities_for_prompt(entities) + "\n\n"
    user += "Raw search response:\n" + raw_response_text
    return [{"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def parse_cleanup_response(parsed_json: object, entities: list[Entity]) -> list[EntityResult]:
    """Map LLM array → one EntityResult per input entity (by sno; fall back to order)."""
    by_sno: dict[int, dict] = {}
    if isinstance(parsed_json, list):
        for obj in parsed_json:
            if isinstance(obj, dict):
                try:
                    by_sno[int(obj.get("sno") or 0)] = obj
                except (TypeError, ValueError):
                    pass
    results = []
    for e in entities:
        obj = by_sno.get(e.sno)
        if obj is None:
            results.append(EntityResult(
                company_name=e.company_name, country=e.country, sno=e.sno,
                company_local_name=e.company_local_name,
                error="missing from LLM response"))
        else:
            results.append(EntityResult.from_llm_object(
                obj, fallback_country=e.country, fallback_name=e.company_name,
                fallback_local=e.company_local_name, fallback_sno=e.sno))
    return results
```

(Reuse the existing JSON-parsing helper — `gemini_batch.parse_json_from_text` or the equivalent in `llm_client` — between the raw LLM text and `parse_cleanup_response`; grep for how `extraction.py` parses today and call the same helper.)

- [ ] **Step 5: Tests PASS + full suite. Commit** `"feat: unified EntityResult schema + cleanup module"`.

### Task 10: Reporting — new CSVs, merged `final_report.json`, per-company run dirs

**Files:**
- Create: `backend/app/services/ai_mode/run_reporting.py`
- Create: `backend/app/services/ai_mode/run_store.py`
- Test: `backend/tests/test_run_reporting.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_run_reporting.py
import csv, io, json, tempfile, unittest
from pathlib import Path
from app.models.results import AttemptLogEntry, EntityResult, Flag
from app.services.ai_mode.run_reporting import write_outputs
from app.services.ai_mode.run_store import slugify_company

class TestSlugify(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify_company("Acme Corp (JP)!"), "acme-corp-jp")

class TestWriteOutputs(unittest.TestCase):
    def test_found_notfound_and_final_report(self):
        results = [
            EntityResult("Acme", "Japan", 1, website_url="https://acme.jp", confidence=90,
                         flags=[Flag("name_match", "exact")],
                         attempt_log=[AttemptLogEntry("q1", "found", "https://acme.jp")]),
            EntityResult("Beta", "Germany", 2, confidence=10,
                         flags=[Flag("no_results", "nothing found")], error="llm: no data"),
        ]
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            write_outputs(run_dir, results, summary={"status": "completed"},
                          requests=[{"batch": 1, "status": "ok"}])
            found = list(csv.DictReader(io.StringIO((run_dir / "found.csv").read_text())))
            notfound = list(csv.DictReader(io.StringIO((run_dir / "notFound.csv").read_text())))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["website_url"], "https://acme.jp")
            self.assertEqual(found[0]["flags"], "name_match: exact")
            self.assertIn("attempt_log", found[0])
            self.assertEqual(notfound[0]["error"], "llm: no data")
            report = json.loads((run_dir / "final_report.json").read_text())
            self.assertEqual(report["summary"]["status"], "completed")
            self.assertEqual(len(report["requests"]), 1)
            self.assertEqual(len(report["entities"]), 2)
            self.assertFalse((run_dir / "report.json").exists())

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**

```python
# backend/app/services/ai_mode/run_store.py
"""Run directories under ai_mode_results/<company_slug>/<run_id>/ (spec §6)."""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import AI_MODE_RESULTS_DIR


def slugify_company(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "unnamed"


def run_dir_for(company_name: str, run_id: str) -> Path:
    d = AI_MODE_RESULTS_DIR / slugify_company(company_name) / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "raw_responses").mkdir(exist_ok=True)
    return d


def find_run_dir(run_id: str) -> Path | None:
    if not AI_MODE_RESULTS_DIR.exists():
        return None
    hits = list(AI_MODE_RESULTS_DIR.glob(f"*/{run_id}"))
    return hits[0] if hits else None


def list_run_dirs() -> list[Path]:
    if not AI_MODE_RESULTS_DIR.exists():
        return []
    return sorted((p for p in AI_MODE_RESULTS_DIR.glob("*/*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
```

```python
# backend/app/services/ai_mode/run_reporting.py
"""found.csv / notFound.csv / single merged final_report.json (spec §§5-6)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.models.results import EntityResult

CSV_COLUMNS = ["company_name", "company_local_name", "country", "website_url",
               "confidence", "flags", "attempt_log"]


def _row(r: EntityResult) -> dict:
    return {"company_name": r.company_name, "company_local_name": r.company_local_name or "",
            "country": r.country, "website_url": r.website_url or "",
            "confidence": r.confidence, "flags": r.flags_csv(),
            "attempt_log": r.attempt_log_csv()}


def write_outputs(run_dir: Path, results: list[EntityResult],
                  summary: dict, requests: list[dict]) -> dict[str, Path]:
    found = [r for r in results if r.website_url]
    notfound = [r for r in results if not r.website_url]

    paths: dict[str, Path] = {}
    for name, rows, extra in (("found.csv", found, []), ("notFound.csv", notfound, ["error"])):
        path = run_dir / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS + extra)
            writer.writeheader()
            for r in rows:
                row = _row(r)
                if extra:
                    row["error"] = r.error or ""
                writer.writerow(row)
        paths[name] = path

    report = {"summary": summary, "requests": requests,
              "entities": [r.to_report_dict() for r in results]}
    report_path = run_dir / "final_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["final_report.json"] = report_path
    return paths
```

- [ ] **Step 4: Tests PASS + full suite. Commit** `"feat: unified reporting + per-company run dirs"`.

### Task 11: Rewire the engine (`ai_mode_service.py`) onto the new parser/cleanup/reporting + ModeConfig

This is the biggest integration task. The two-phase pipeline structure in `run_ai_mode_sync` (Phase 1 parallel scrape with resume → Phase 2 sync/Gemini-batch clean → Phase 3 assemble) **stays**; what changes is what flows through it.

**Files:**
- Create: `backend/app/services/ai_mode/mode_config.py`
- Modify: `backend/app/services/ai_mode/ai_mode_service.py`
- Delete: `backend/app/services/ai_mode/{extraction.py,company_extraction.py,prompting.py,company_prompting.py,cleanup_reporting.py,company_reporting.py,company_csv_loader.py}` and `backend/app/prompts/{search_query_template.txt,company_search_template.txt}`
- Test: `backend/tests/test_mode_config.py`

- [ ] **Step 1: Write failing test for ModeConfig**

```python
# backend/tests/test_mode_config.py
import os, unittest
from unittest import mock
from app.services.ai_mode.mode_config import MODES, get_mode

class TestModeConfig(unittest.TestCase):
    def test_modes_exist(self):
        self.assertEqual(set(MODES), {"ai_bulk", "ai_deep"})

    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_BULK_BATCH_SIZE", None)
            os.environ.pop("AI_DEEP_BATCH_SIZE", None)
            self.assertEqual(get_mode("ai_bulk").batch_size(), 10)
            self.assertEqual(get_mode("ai_deep").batch_size(), 3)

    def test_unknown_mode_raises(self):
        with self.assertRaises(KeyError):
            get_mode("ai_turbo")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL. Implement:**

```python
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
        env = os.getenv(self.batch_size_env)
        if env:
            return max(1, int(env))
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
```

- [ ] **Step 3: Rewire `ai_mode_service.py`** — work through `prepare_ai_mode_run` and `run_ai_mode_sync` top to bottom:

  1. `prepare_ai_mode_run(raw_csv, filename, *, mode_key: str, company_name: str, company_id: str)` — replace `_detect_input_type` + `load_company_entities`/`load_address_entities` with `parse_entities_csv` (raise its `InvalidCSVError` so the router maps it to HTTP 400). Persist into `status.json`: `mode` (`ai_bulk`/`ai_deep`), `company_id`, `company_name`, `columns_detected`, `warnings`. Delete `input_type` everywhere (grep for it).
  2. Run dir: replace the old `ai_mode_result/<run_id>` base with `run_store.run_dir_for(company_name, run_id)`; replace all internal `os.path.join(run_dir, ...)` lookups accordingly; `list_ai_mode_runs`/`get_ai_mode_status`/`get_ai_mode_result_path` now resolve via `run_store.find_run_dir`/`list_run_dirs` (keep a legacy fallback that also checks `LEGACY_AI_MODE_RESULT_DIR/<run_id>` so old runs stay viewable).
  3. Batching: group entities into chunks of `get_mode(mode_key).batch_size()`. Search query per batch = `mode.search_prompt().replace("{entities}", format_entities_for_prompt(batch))` (keep `_geo_params_for_group` geo-targeting as is — it reads country from the entities; adapt its input to the new `Entity` objects).
  4. Cleanup: replace calls into `extraction`/`company_extraction` with `cleanup.build_cleanup_messages(raw_text, batch_entities)` → existing llm client / gemini-batch path → JSON parse → `cleanup.parse_cleanup_response(parsed, batch_entities)`. Both sync and `AI_MODE_LLM_BATCH=true` paths go through these (in batch mode the messages builder feeds `gemini_batch.messages_to_gemini_request` exactly as the old builders did).
  5. Assemble: replace the old found/notFound/`report.json`+`final_report.json` writers with ONE call to `run_reporting.write_outputs(run_dir, all_results, summary=..., requests=per_request_entries)` where `summary` carries what today's final report summary has (status, counts, timings, token_usage) and `requests` carries today's per-request entries (`scrapedo_seconds`, `llm_seconds`, `raw_json_file`, `scrapedo_params`, `status`, `error`). Delete all `report.json` writing.
  6. Logging: merge `run.log` + `ai_mode_debug.log` into a single `run.log` — keep the debug logger but point both handlers at `run.log` with level from `AI_MODE_LOG_LEVEL` (secret redaction stays). Raw responses dir renames from `raw_scrapedo_response/` to `raw_responses/` (run_store already creates it; update the writer + resume-check paths).
  7. Update the result-endpoint allowlist (in `routers/ai_mode.py`) to: `final_report.json, found.csv, notFound.csv, run.log, input.csv`.
  8. Delete the dead modules + old prompt files listed in **Files** (grep each name first to confirm nothing references it).

- [ ] **Step 4: Update `routers/ai_mode.py` upload endpoint**

```python
@router.post("/uploads/ai-mode")
async def upload_ai_mode(
    file: UploadFile = File(...),
    mode: str = Form("ai_bulk"),
    company_id: str = Form(...),
):
    if mode not in MODES:
        raise HTTPException(400, f"mode must be one of {sorted(MODES)}")
    company = company_service.get_company(company_id)   # wired fully in Task 14
    if company is None:
        raise HTTPException(400, "unknown company_id — create the company first")
    raw = await file.read()
    try:
        info = prepare_ai_mode_run(raw, file.filename, mode_key=mode,
                                   company_name=company["name"], company_id=company_id)
    except InvalidCSVError as exc:
        raise HTTPException(400, str(exc))
    ...  # schedule asyncio.to_thread(run_ai_mode_sync, run_id) — unchanged
```

(Until Task 14 lands, stub `company_service.get_company` to return `{"id": company_id, "name": company_id}` so this task stays runnable; mark with a `# replaced in Task 14` comment.)

- [ ] **Step 5: Verify** — full suite green; then an end-to-end smoke test mirroring the existing one (scrape.do + LLM mocked — see how the previous session's smoke test mocked them, or write `backend/tests/test_engine_smoke.py` mocking `scrapedo_client` fetch + llm client to return a canned cleanup JSON array) proving: bulk mode batches of 10, deep mode batches of 3, outputs land in `ai_mode_results/<slug>/<run_id>/` with the new file set, both CSVs have `confidence/flags/attempt_log` columns.

- [ ] **Step 6: Commit** `"feat: unified two-mode AI engine (ai_bulk/ai_deep) on new schema"`.

---

## Phase 4 — Supabase

### Task 12: SQL migration

**Files:**
- Create: `supabase/migrations/001_init.sql` (at `$ROOT/supabase/`)

- [ ] **Step 1: Write the migration** — exactly the SQL from spec §4 (`companies` + `runs` + `runs_company_idx`; `runs.cost jsonb`, `runs.file_links jsonb`, `runs.rerun_of uuid references runs(id)`).

- [ ] **Step 2: Apply it** in the Supabase project's SQL editor (user creates the project and puts `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `$ROOT/.env`). If credentials aren't available yet, continue — everything below is tested against mocks; flag it in the final report.

- [ ] **Step 3: Commit** `"feat: supabase schema migration"`.

### Task 13: Supabase client + company/run service

**Files:**
- Create: `backend/app/core/supabase_client.py`, `backend/app/services/companies.py`
- Modify: `backend/requirements.txt` (add `supabase`)
- Test: `backend/tests/test_companies_service.py`

- [ ] **Step 1: Write failing tests (mocked client — no network)**

```python
# backend/tests/test_companies_service.py
import unittest
from unittest import mock
from app.services.companies import CompanyService

def make_table_mock(result_data):
    table = mock.MagicMock()
    for m in ("insert", "select", "update", "eq", "order"):
        getattr(table, m).return_value = table
    table.execute.return_value = mock.MagicMock(data=result_data)
    return table

class TestCompanyService(unittest.TestCase):
    def test_create_company(self):
        client = mock.MagicMock()
        client.table.return_value = make_table_mock([{"id": "u1", "name": "Acme"}])
        svc = CompanyService(client)
        self.assertEqual(svc.create_company("Acme")["id"], "u1")
        client.table.assert_called_with("companies")

    def test_update_run_retries_then_succeeds(self):
        client = mock.MagicMock()
        good = make_table_mock([{"id": "r1"}])
        bad = mock.MagicMock(); bad.update.side_effect = RuntimeError("down")
        client.table.side_effect = [bad, bad, good]
        svc = CompanyService(client, retry_sleep=0)
        self.assertTrue(svc.update_run("r1", status="completed"))

    def test_update_run_gives_up_quietly(self):
        client = mock.MagicMock()
        bad = mock.MagicMock(); bad.update.side_effect = RuntimeError("down")
        client.table.side_effect = [bad, bad, bad]
        svc = CompanyService(client, retry_sleep=0)
        self.assertFalse(svc.update_run("r1", status="completed"))  # logs, never raises

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL. Install dep:** `cd $ROOT && .venv/bin/pip install supabase` and add `supabase` to `backend/requirements.txt`.

- [ ] **Step 3: Implement**

```python
# backend/app/core/supabase_client.py
"""FastAPI is the ONLY Supabase client (spec §4). Lazy singleton; None when unconfigured."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_client = None
_attempted = False


def get_supabase():
    global _client, _attempted
    if not _attempted:
        _attempted = True
        url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if url and key:
            from supabase import create_client
            _client = create_client(url, key)
        else:
            logger.warning("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set — "
                           "company tracking disabled")
    return _client
```

```python
# backend/app/services/companies.py
"""Company + run-row lifecycle. Bookkeeping only: update failures NEVER raise (spec §4)."""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class CompanyService:
    def __init__(self, client, retry_sleep: float = 1.0):
        self.client = client
        self.retry_sleep = retry_sleep

    # --- companies ---------------------------------------------------------
    def create_company(self, name: str) -> dict:
        res = self.client.table("companies").insert({"name": name.strip()}).execute()
        return res.data[0]

    def list_companies(self) -> list[dict]:
        return self.client.table("companies").select("*").order("created_at").execute().data

    def get_company(self, company_id: str) -> dict | None:
        data = (self.client.table("companies").select("*")
                .eq("id", company_id).execute().data)
        return data[0] if data else None

    # --- runs --------------------------------------------------------------
    def create_run(self, *, company_id: str, pipeline: str, run_ref: str,
                   total_rows: int | None = None, rerun_of: str | None = None) -> str | None:
        try:
            row = {"company_id": company_id, "pipeline": pipeline, "run_ref": run_ref,
                   "status": "queued", "total_rows": total_rows, "rerun_of": rerun_of}
            res = self.client.table("runs").insert(row).execute()
            return res.data[0]["id"]
        except Exception:
            logger.exception("supabase: create_run failed (run continues without tracking)")
            return None

    def update_run(self, run_db_id: str | None, **fields: Any) -> bool:
        if not run_db_id:
            return False
        for attempt in range(3):
            try:
                (self.client.table("runs").update(fields)
                 .eq("id", run_db_id).execute())
                return True
            except Exception:
                logger.exception("supabase: update_run attempt %s failed", attempt + 1)
                time.sleep(self.retry_sleep)
        logger.error("supabase: giving up updating run %s — stats lost, run unaffected",
                     run_db_id)
        return False

    def list_runs(self, company_id: str | None = None, pipeline: str | None = None,
                  limit: int = 200) -> list[dict]:
        q = self.client.table("runs").select("*")
        if company_id:
            q = q.eq("company_id", company_id)
        if pipeline:
            q = q.eq("pipeline", pipeline)
        return q.order("created_at", desc=True).limit(limit).execute().data

    def company_stats(self) -> list[dict]:
        """Aggregate per company in Python (internal tool, low volume)."""
        companies = self.list_companies()
        runs = self.client.table("runs").select(
            "company_id,status,total_rows,success_count,failed_count,"
            "websites_found,websites_not_found,cost,token_usage").execute().data
        by_company: dict[str, list[dict]] = {}
        for r in runs:
            by_company.setdefault(r["company_id"], []).append(r)
        out = []
        for c in companies:
            rs = by_company.get(c["id"], [])
            def s(key):
                return sum((r.get(key) or 0) for r in rs)
            cost = sum(((r.get("cost") or {}).get("total_usd") or 0) for r in rs)
            tokens = sum(((r.get("token_usage") or {}).get("total_tokens") or 0) for r in rs)
            out.append({**c, "runs": len(rs), "total_rows": s("total_rows"),
                        "success_count": s("success_count"), "failed_count": s("failed_count"),
                        "websites_found": s("websites_found"),
                        "websites_not_found": s("websites_not_found"),
                        "total_cost_usd": round(cost, 4), "total_tokens": tokens})
        return out


def get_company_service() -> CompanyService | None:
    from app.core.supabase_client import get_supabase
    client = get_supabase()
    return CompanyService(client) if client else None
```

- [ ] **Step 4: Tests PASS + full suite. Commit** `"feat: supabase company/run service"`.

### Task 14: `routers/companies.py` + wire company tracking into ALL pipelines

**Files:**
- Create: `backend/app/routers/companies.py`
- Modify: `backend/app/main.py` (include router), `backend/app/routers/ai_mode.py` (replace Task 11's stub), `backend/app/services/ai_mode/ai_mode_service.py` (run-row lifecycle), `backend/app/services/serpwow/legacy_app.py` (every upload endpoint + completion hook)

- [ ] **Step 1: Companies router**

```python
# backend/app/routers/companies.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.companies import get_company_service

router = APIRouter(prefix="/companies")


class CompanyIn(BaseModel):
    name: str


def _svc():
    svc = get_company_service()
    if svc is None:
        raise HTTPException(503, "Supabase not configured (set SUPABASE_URL + "
                                 "SUPABASE_SERVICE_ROLE_KEY)")
    return svc


@router.post("")
def create_company(body: CompanyIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "company name is required")
    try:
        return _svc().create_company(name)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(409, f"company '{name}' already exists")
        raise


@router.get("")
def list_companies():
    return {"companies": _svc().list_companies()}


@router.get("/stats")
def company_stats():
    return {"companies": _svc().company_stats()}


@router.get("/runs")
def list_runs(company_id: str | None = None, pipeline: str | None = None):
    return {"runs": _svc().list_runs(company_id=company_id, pipeline=pipeline)}
```

- [ ] **Step 2: AI-mode run lifecycle** — in `routers/ai_mode.py`: replace the Task 11 stub with `_svc().get_company(company_id)` (404→400 as before); after `prepare_ai_mode_run`, call `create_run(company_id=..., pipeline=mode, run_ref=run_id, total_rows=...)` and store the returned `run_db_id` inside the run's `status.json`. In `run_ai_mode_sync`: at start → `update_run(run_db_id, status="running", started_at=utcnow_iso)`; at terminal → one `update_run` with `status`, `success_count` (= websites_found), `failed_count` (= websites_not_found + llm_errors), `websites_found`, `websites_not_found`, `token_usage`, `cost` (Task 15), `duration_seconds`, `file_links` (the dict of absolute paths from `write_outputs`, plus `input.csv`/`run.log`), `finished_at`, `error`. Service is fetched via `get_company_service()`; when it's `None` everything no-ops (warning already logged).

- [ ] **Step 3: SerpWow uploads** — in `legacy_app.py`, find every upload endpoint: `grep -n '@app.post("/uploads' app/services/serpwow/legacy_app.py`. For EACH: add `company_id: str = Form(...)` (or to its existing request model), validate via the service (unknown → 400), call `create_run(pipeline=<mode string>, run_ref=upload_id, total_rows=row_count)`, and persist `run_db_id` + `company_id` into the upload's state (the same `state.json`/dict that tracks upload status — grep how `upload_id` state is stored). Then find where an upload reaches terminal status (grep `"completed"` / `completed_with_errors` writes in `process_upload_job` and the result-assembly paths) and add ONE `update_run(...)` call mapping the SerpWow stats: `success_count` = rows with URL found, `failed_count` = failed rows, `token_usage`/`cost` from the existing token/cost tracking if present (else null), `file_links` = the S3/JSON/XLSX output references the upload already records. Mapping precision matters less than never raising — wrap the whole hook in the service's no-raise semantics.

- [ ] **Step 4: Verify** — full suite; boot server; `curl -X POST localhost:8080/companies -H 'content-type: application/json' -d '{"name":"TestCo"}'` (against real Supabase if configured, else expect 503 and verify uploads still work without tracking). Upload a sample CSV to AI mode with `mode=ai_bulk&company_id=<id>` and confirm a `runs` row appears and gets updated.

- [ ] **Step 5: Commit** `"feat: company tracking across all pipelines"`.

### Task 15: Cost accuracy

**Files:**
- Create: `backend/app/services/ai_mode/cost.py`
- Modify: `backend/app/services/ai_mode/scrapedo_client.py` (capture per-request cost), `ai_mode_service.py` (aggregate into summary + run row)
- Test: `backend/tests/test_cost.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_cost.py
import os, unittest
from unittest import mock
from app.services.ai_mode.cost import build_cost_summary, extract_scrapedo_request_cost

class TestScrapedoCost(unittest.TestCase):
    def test_extracts_header_cost(self):
        headers = {"Scrape.do-Request-Cost": "5"}
        self.assertEqual(extract_scrapedo_request_cost(headers), 5.0)

    def test_missing_header_returns_none(self):
        self.assertIsNone(extract_scrapedo_request_cost({}))

class TestBuildCostSummary(unittest.TestCase):
    def test_headers_present_sums_credits(self):
        with mock.patch.dict(os.environ, {"SCRAPEDO_COST_PER_REQUEST_USD": "0.002"}):
            c = build_cost_summary(llm_usd=1.5, request_costs=[5.0, 5.0], request_count=2)
        self.assertEqual(c["scrapedo_requests"], 2)
        self.assertEqual(c["scrapedo_credits"], 10.0)
        self.assertAlmostEqual(c["total_usd"], 1.5 + c["scrapedo_usd"])

    def test_fallback_estimate_when_no_headers(self):
        with mock.patch.dict(os.environ, {"SCRAPEDO_COST_PER_REQUEST_USD": "0.002"}):
            c = build_cost_summary(llm_usd=1.0, request_costs=[None, None], request_count=2)
        self.assertAlmostEqual(c["scrapedo_usd"], 0.004)
        self.assertTrue(c["scrapedo_cost_estimated"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL. Implement:**

```python
# backend/app/services/ai_mode/cost.py
"""Per-run cost: LLM + scrape.do (spec §4 add-on #2).

scrape.do reports per-request credit cost in a response header. Header name must be
verified against a real response on first live run (candidates below) — until then the
env-rate estimate covers it.
"""
from __future__ import annotations

import os

_HEADER_CANDIDATES = ("Scrape.do-Request-Cost", "Scrapedo-Request-Cost",
                      "X-Scrapedo-Request-Cost", "sd-request-cost")


def extract_scrapedo_request_cost(headers) -> float | None:
    for name in _HEADER_CANDIDATES:
        value = headers.get(name) if hasattr(headers, "get") else None
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def build_cost_summary(*, llm_usd: float, request_costs: list[float | None],
                       request_count: int) -> dict:
    per_request_usd = float(os.getenv("SCRAPEDO_COST_PER_REQUEST_USD", "0") or 0)
    known = [c for c in request_costs if c is not None]
    if known and len(known) == len(request_costs):
        credits = sum(known)
        # credits→USD conversion depends on the scrape.do plan; expose credits raw and
        # use the env rate per request for the USD figure until a credit rate is known.
        scrapedo_usd = request_count * per_request_usd
        estimated = False
    else:
        credits = sum(known) if known else None
        scrapedo_usd = request_count * per_request_usd
        estimated = True
    return {"llm_usd": round(llm_usd, 6), "scrapedo_usd": round(scrapedo_usd, 6),
            "scrapedo_credits": credits, "scrapedo_requests": request_count,
            "scrapedo_cost_estimated": estimated,
            "total_usd": round(llm_usd + scrapedo_usd, 6)}
```

- [ ] **Step 3: Wire it** — in `scrapedo_client.py`, where the HTTP response is received, call `extract_scrapedo_request_cost(response.headers)` and return/record it alongside the existing per-attempt logging (add it to the per-request entry that lands in `report` requests). In `ai_mode_service.py` Phase 3, compute `llm_usd` from the existing token-usage→cost helpers (sync path + `gemini_batch.calculate_gemini_batch_cost_usd` for batch path), call `build_cost_summary`, put it in `final_report.json` summary and the run row's `cost` field. Add `SCRAPEDO_COST_PER_REQUEST_USD=0` to `.env.example`. On the first real run, check `run.log` for the actual header name and correct `_HEADER_CANDIDATES` if needed.

- [ ] **Step 4: Tests PASS + full suite. Commit** `"feat: per-run cost summary (llm + scrape.do)"`.

---

## Phase 5 — Add-ons

### Task 16: CSV preview endpoint

**Files:**
- Modify: `backend/app/routers/ai_mode.py`
- Test: `backend/tests/test_preview.py`

- [ ] **Step 1: Write failing test** (use `fastapi.testclient.TestClient` — `starlette` ships it; if `httpx` version complains, call the underlying function directly):

```python
# backend/tests/test_preview.py
import unittest
from app.routers.ai_mode import build_preview

class TestPreview(unittest.TestCase):
    def test_preview_payload(self):
        csv = "company_name,country,local_name\nAcme,Japan,アクメ\nBeta,Germany,\n"
        p = build_preview(csv.encode())
        self.assertEqual(p["total_rows"], 2)
        self.assertEqual(len(p["sample_rows"]), 2)
        self.assertEqual(p["columns_detected"]["company_name"], "company_name")
        self.assertEqual(p["sample_rows"][0]["company_local_name"], "アクメ")

    def test_preview_invalid_csv(self):
        from app.models.entities import InvalidCSVError
        with self.assertRaises(InvalidCSVError):
            build_preview(b"")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL. Implement in `routers/ai_mode.py`:**

```python
from dataclasses import asdict
from app.models.entities import InvalidCSVError, parse_entities_csv


def build_preview(raw: bytes) -> dict:
    parsed = parse_entities_csv(raw)
    return {"total_rows": len(parsed.entities),
            "columns_detected": parsed.columns_detected,
            "warnings": parsed.warnings,
            "positional": parsed.positional,
            "sample_rows": [asdict(e) for e in parsed.entities[:5]]}


@router.post("/uploads/preview")
async def preview_upload(file: UploadFile = File(...)):
    try:
        return build_preview(await file.read())
    except InvalidCSVError as exc:
        raise HTTPException(400, str(exc))
```

- [ ] **Step 3: Tests PASS + full suite. Commit** `"feat: CSV preview endpoint"`.

### Task 17: Re-run failed/partial AI-mode runs

**Files:**
- Create: `backend/app/services/ai_mode/rerun.py`
- Modify: `backend/app/routers/ai_mode.py`, `ai_mode_service.py` (merge carryover at assemble)
- Test: `backend/tests/test_rerun.py`

- [ ] **Step 1: Write failing test for row selection**

```python
# backend/tests/test_rerun.py
import json, tempfile, unittest
from pathlib import Path
from app.services.ai_mode.rerun import split_for_rerun

class TestSplitForRerun(unittest.TestCase):
    def test_split(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            (run_dir / "input.csv").write_text(
                "company_name,country\nAcme,Japan\nBeta,Germany\nGamma,France\n")
            report = {"entities": [
                {"sno": 1, "company_name": "Acme", "country": "Japan",
                 "website_url": "https://acme.jp", "confidence": 90,
                 "flags": [], "attempt_log": [], "error": None,
                 "company_local_name": None},
                {"sno": 2, "company_name": "Beta", "country": "Germany",
                 "website_url": None, "confidence": 0, "flags": [],
                 "attempt_log": [], "error": "llm timeout",
                 "company_local_name": None},
            ]}  # Gamma never made it into the report (never scraped)
            (run_dir / "final_report.json").write_text(json.dumps(report))
            retry_csv, carryover = split_for_rerun(run_dir)
            self.assertEqual(len(carryover), 1)            # Acme carried over
            self.assertIn("Beta", retry_csv)               # failed → retried
            self.assertIn("Gamma", retry_csv)              # unscraped → retried
            self.assertNotIn("Acme", retry_csv)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify FAIL. Implement:**

```python
# backend/app/services/ai_mode/rerun.py
"""Re-run: feed back only failed/unscraped rows; carry successes (spec §7 add-on #4)."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from app.models.entities import parse_entities_csv


def _key(name: str, country: str) -> tuple[str, str]:
    return (name.strip().lower(), country.strip().lower())


def split_for_rerun(prev_run_dir: Path) -> tuple[str, list[dict]]:
    """Returns (retry_input_csv_text, carryover_entity_report_dicts)."""
    report = json.loads((prev_run_dir / "final_report.json").read_text(encoding="utf-8"))
    succeeded: dict[tuple[str, str], dict] = {}
    for ent in report.get("entities", []):
        if ent.get("website_url"):
            succeeded[_key(ent["company_name"], ent.get("country", ""))] = ent

    raw_input = (prev_run_dir / "input.csv").read_text(encoding="utf-8")
    parsed = parse_entities_csv(raw_input)

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["company_name", "country", "company_local_name",
                     "address", "firm_id", "industry"])
    retry_count = 0
    for e in parsed.entities:
        if _key(e.company_name, e.country) in succeeded:
            continue
        writer.writerow([e.company_name, e.country, e.company_local_name or "",
                         e.address or "", e.firm_id or "", e.industry or ""])
        retry_count += 1
    if retry_count == 0:
        raise ValueError("nothing to re-run: every row already succeeded")
    return out.getvalue(), list(succeeded.values())
```

- [ ] **Step 3: Endpoint + carryover merge.** In `routers/ai_mode.py`:

```python
@router.post("/uploads/ai-mode/{run_id}/rerun")
async def rerun_ai_mode(run_id: str):
    prev_dir = run_store.find_run_dir(run_id)
    if prev_dir is None:
        raise HTTPException(404, "run not found")
    prev_status = get_ai_mode_status(run_id)
    if prev_status.get("status") not in ("failed", "completed_with_errors"):
        raise HTTPException(400, "re-run is only for failed / completed_with_errors runs")
    try:
        retry_csv, carryover = split_for_rerun(prev_dir)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    info = prepare_ai_mode_run(retry_csv.encode(), f"rerun_of_{run_id}.csv",
                               mode_key=prev_status["mode"],
                               company_name=prev_status["company_name"],
                               company_id=prev_status["company_id"])
    new_dir = run_store.find_run_dir(info["run_id"])
    (new_dir / "carryover.json").write_text(json.dumps(carryover, ensure_ascii=False))
    # create_run with rerun_of=<prev run_db_id from prev status.json>
    ...  # schedule run exactly like the normal upload endpoint
    return info
```

In `ai_mode_service.py` Phase 3 (assemble): if `carryover.json` exists in the run dir, load it, convert each dict to `EntityResult` via `EntityResult.from_llm_object` and append to results before `write_outputs` (carried entities keep their original data; add a flag `{"flag": "carried_over", "why": "from previous run"}` to each).

- [ ] **Step 4: Tests PASS + full suite + manual check** (force a failed run by running without `SCRAPEDO_TOKEN`, then hit the rerun endpoint). **Commit** `"feat: re-run failed/partial AI-mode runs with carryover"`.

---

## Phase 6 — UI

The UI talks ONLY to existing FastAPI endpoints. Look at the old `templates/ui.html` for the exact request/response shapes of the SerpWow tabs you're porting — but write new, clean code. Tailwind via CDN. One shared shell, one JS module per view.

### Task 18: App shell + shared JS + static mounting

**Files:**
- Create: `backend/app/templates/index.html`, `backend/app/static/css/app.css`, `backend/app/static/js/api.js`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Mount static + new UI route in `main.py`**

```python
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.core.config import STATIC_DIR, TEMPLATES_DIR

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/app")
def serve_app():
    return FileResponse(str(TEMPLATES_DIR / "index.html"))
```

(Old `/ui` keeps serving `ui.html` until Task 22 deletes it.)

- [ ] **Step 2: `index.html`** — single-page shell: fixed left sidebar nav (Dashboard, Companies, New Run, Runs, Operations), a `<main id="view">` container, Tailwind CDN `<script src="https://cdn.tailwindcss.com"></script>`, `app.css` for the few custom bits (status badge colors), and `<script type="module" src="/static/js/main.js">`. Views are `<section data-view="dashboard">…` blocks toggled by nav clicks (hash routing: `#/dashboard`, `#/runs/<id>`). Aesthetic: neutral grays, white cards with `rounded-xl shadow-sm border`, one accent color (indigo), `text-sm` tables with `divide-y`, generous padding. No charts.

- [ ] **Step 3: `api.js`**

```javascript
// backend/app/static/js/api.js
export async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

export function pollStatus(path, onUpdate, intervalMs = 2000) {
  let stopped = false;
  async function tick() {
    if (stopped) return;
    try {
      const status = await api(path);
      onUpdate(status);
      if (["completed", "completed_with_errors", "failed"].includes(status.status)) return;
    } catch (e) { console.error(e); }
    setTimeout(tick, intervalMs);
  }
  tick();
  return () => { stopped = true; };
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children);
  return node;
}

export const fmtUsd = (n) => n == null ? "—" : `$${Number(n).toFixed(4)}`;
export const fmtNum = (n) => n == null ? "—" : Number(n).toLocaleString();
```

Create `main.js` with the hash router: map `#/dashboard|companies|new-run|runs|operations|runs/<id>` → show the section and call that module's `render()` export.

- [ ] **Step 4: Verify in browser** (`/app` renders shell, nav switches views) **+ commit** `"feat: UI shell (FastAPI-served, no build step)"`.

### Task 19: Dashboard + Companies views

**Files:**
- Create: `backend/app/static/js/dashboard.js`, `backend/app/static/js/companies.js`
- Modify: `backend/app/templates/index.html` (the two sections), `backend/app/static/js/main.js`

- [ ] **Step 1: `dashboard.js`** — `render()`: `api("/companies/stats")` → company cards grid (name, runs, success/failed, websites found/not, tokens, cost via `fmtUsd`) + `api("/companies/runs")` → "Recent runs" table (latest 20: company, pipeline, status badge, rows, found, cost, created; row click → `location.hash = "#/runs/" + run.run_ref`). Empty state: "No companies yet — create one" linking to `#/companies`. If the stats call returns 503, show a setup card telling the user to configure `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`.

- [ ] **Step 2: `companies.js`** — `render()`: create form (name input + button → `POST /companies`, on 409 show "already exists" inline) above a table of companies with their stats (reuse `/companies/stats`), row click → `#/runs` filtered by that company.

- [ ] **Step 3: Verify in browser against real/empty Supabase, commit** `"feat: dashboard + companies views"`.

### Task 20: New Run view (with preview step)

**Files:**
- Create: `backend/app/static/js/new_run.js`
- Modify: `index.html` section, `main.js`

- [ ] **Step 1: Implement the stepper** in `new_run.js`:

1. **Company** — `<select>` from `GET /companies` + "create new" inline shortcut.
2. **Pipeline** — radio cards: `AI Mode 1 — Bulk (ai_bulk)`, `AI Mode 2 — Deep Search (ai_deep)`, `Google Maps (gmaps)`, `Google Search (gsearch)`, `Full`, `Firmographics` — each with its one-line description.
3. **File + Preview** — file input; on select, `POST /uploads/preview` (FormData) → render `columns_detected` mapping table, `total_rows`, warnings (amber callout), and the 5 sample rows; on 400, red callout with the server's message (which lists accepted columns). Confirm button stays disabled until preview succeeds.
4. **Confirm & start** — AI modes: `POST /uploads/ai-mode` with `file`, `mode`, `company_id` → `location.hash = "#/runs/" + run_id`. SerpWow pipelines: post to the existing upload endpoint for that mode (find form fields in old `ui.html`'s Upload Console JS) + `company_id` → navigate to `#/runs`.

- [ ] **Step 2: Verify in browser end-to-end with `samples/sample_address.csv` (should preview fine) and an old `Company Name ENG` file (should show the 400 message). Commit** `"feat: new-run flow with CSV preview"`.

### Task 21: Run detail + Runs history views

**Files:**
- Create: `backend/app/static/js/run_detail.js`, `backend/app/static/js/runs.js`
- Modify: `index.html` sections, `main.js`

- [ ] **Step 1: `run_detail.js`** — `render(runId)`: `pollStatus("/uploads/ai-mode/" + runId + "/status", update)`. Layout: header (company, mode label, status badge, phase chip `scraping`/`cleaning`), progress bar (`batches_done/batches_total`), stat tiles (entities processed, found/not found, llm errors, scrape.do requests/failures, tokens, cost, duration), downloads row — buttons for `final_report.json`, `found.csv`, `notFound.csv`, `run.log`, `input.csv` hitting `/uploads/ai-mode/<id>/result?file=<name>&download=true`. **Re-run button** visible only when status ∈ {failed, completed_with_errors}: `POST /uploads/ai-mode/<id>/rerun` → navigate to the new run's detail. If the runId isn't an AI-mode run (status 404), fall back to rendering the SerpWow upload status via its existing status endpoint (grep old `ui.html` for the upload-status URL it polls) with its download links.

- [ ] **Step 2: `runs.js`** — `render()`: filter bar (company select, pipeline select, status select) → `GET /companies/runs?company_id=&pipeline=` table; status filter applied client-side; row click → `#/runs/<run_ref>`.

- [ ] **Step 3: Verify in browser with a real ai_bulk sample run start-to-finish, including downloads + a rerun. Commit** `"feat: run detail + runs history views"`.

### Task 22: Operations view (port Batch Manager + Retry) + delete old UI

**Files:**
- Create: `backend/app/static/js/operations.js`
- Modify: `index.html` section, `main.js`
- Delete: `backend/app/templates/ui.html`, `backend/app/templates/ui copy.html`, the `/ui` route in `legacy_app.py`

- [ ] **Step 1: Port** — open old `ui.html`, locate the Batch Manager and Retry Operations tab scripts (search `Batch Manager` / `Retry`), list every endpoint they call, and rebuild the same actions in `operations.js` as two cards (Gemini batch jobs: list/status/actions; Retry: the existing retry-failed-rows forms) using `api()`/`el()` and the shell's styling. Same requests, same fields — new markup only.

- [ ] **Step 2: Switch `/ui`** — make `/ui` redirect (307) to `/app`, delete `ui.html` + `ui copy.html`, delete the old template-serving code.

- [ ] **Step 3: Click through every view once more; run full test suite. Commit** `"feat: operations view; retire legacy ui.html"`.

---

## Phase 7 — Finish

### Task 23: `.env.example`, docs, final verification

**Files:**
- Modify: `$ROOT/.env.example`, `$ROOT/HANDOFF.md`, `$ROOT/readme.md`

- [ ] **Step 1: Complete `.env.example`** — add with comments: `SUPABASE_URL=`, `SUPABASE_SERVICE_ROLE_KEY=`, `AI_BULK_BATCH_SIZE=10`, `AI_DEEP_BATCH_SIZE=3`, `SCRAPEDO_COST_PER_REQUEST_USD=0`, `AI_MODE_LOG_LEVEL=INFO`, `API_RELOAD=false`, `UVICORN_LOG_LEVEL=info`, `GEMINI_MODEL=gemini-2.5-flash-lite`.

- [ ] **Step 2: Update `HANDOFF.md`** — new §: repo layout (`backend/app/...`), run commands (`cd backend && ../.venv/bin/python -m app.main`, UI at `/app`), the unified input format, the two modes, Supabase setup (migration file + env keys), what was deleted. Update `readme.md` run instructions.

- [ ] **Step 3: Final verification**

```bash
cd $ROOT/backend
../.venv/bin/python -c "import app.main"
../.venv/bin/python -m unittest discover -s tests -v   # all green, incl. the original 17
```
Boot the server; run one real `ai_bulk` and one real `ai_deep` sample (3-row CSV) against a real company; confirm: per-company output dir layout, both CSVs have confidence/flags/attempt_log, single `final_report.json` + single `run.log`, Supabase run row updated with stats + cost + file_links, dashboard shows the company aggregates.

- [ ] **Step 4: Commit** `"docs: env example + handoff for the rework"`.

---

## Spec-coverage map (self-review)

| Spec § | Tasks |
|---|---|
| §1 goals | all |
| §2 restructure (pure moves, dead files, broken scripts, root cleanup) | 0–5 |
| §3 unified input (+local name, {entities}, 400) | 6, 7, 11, 16 |
| §4 Supabase (schema, service, never-fail, all pipelines, cost) | 12–15 |
| §5 two modes / ModeConfig / unified schema / prompts as files (§6a fix) | 8–11 |
| §6 output layout (per-company dirs, merged report, merged log) | 10, 11 |
| §7 UI (shell, dashboard, companies, new run + preview, run detail + rerun, runs, operations) | 16–22 |
| §8 env example, tests, docs | every task's verify steps + 23 |
| §9 out of scope | not planned (correct) |

**Known judgment calls baked in:** the SerpWow monolith stays as one `legacy_app.py` service module with routers attached on top (deep function-level splitting of its 7.6k lines is intentionally NOT in scope — moving + containing it is); scrape.do cost header name is verified on first live run with an env-rate fallback; Supabase aggregates computed in Python (internal volumes).
