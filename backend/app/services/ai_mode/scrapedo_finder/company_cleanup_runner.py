from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Protocol

from .company_csv_loader import load_company_entities
from .company_extraction import build_company_messages, parse_company_results
from .company_reporting import build_company_report_dict, write_company_outputs
from .models import CompanyCleanResult, CompanyEntityInput, TokenUsage, utc_now_iso


class LLMClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> tuple[dict, TokenUsage]: ...


class CompanyCleanupRunner:
    def __init__(
        self,
        llm_client: LLMClient,
        llm_meta: dict[str, str],
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.llm_meta = llm_meta
        self.log = log

    def run(self, batch_dir: Path, csv_path: Path | None = None) -> dict:
        batch_dir = Path(batch_dir)
        report = json.loads((batch_dir / "report.json").read_text(encoding="utf-8"))
        requests = report.get("requests", [])

        csv_file = csv_path or Path(report.get("csv_file", ""))
        lookup = self._load_lookup(csv_file)

        results: list[CompanyCleanResult] = []
        usage_total = TokenUsage()
        without_scrape_data = 0
        llm_errors = 0
        sno = 0
        started = time.time()

        for request in requests:
            names = list(request.get("entity_names", []))
            raw_file = request.get("raw_json_file")
            if not raw_file:
                without_scrape_data += len(names)
                self._log(
                    f"request {request.get('request_index')}: no scrape data, "
                    f"skipping {len(names)} company(ies)"
                )
                continue

            entities = [
                lookup.get(name) or CompanyEntityInput(company_name_eng=name) for name in names
            ]
            raw = json.loads((batch_dir / raw_file).read_text(encoding="utf-8"))
            messages = build_company_messages(entities, raw.get("text_blocks"), raw.get("references"))
            try:
                parsed, usage = self.llm_client.complete_json(messages)
                usage_total = usage_total + usage
                request_results = parse_company_results(parsed, entities)
            except Exception as exc:  # noqa: BLE001 - record per-request failure
                self._log(f"request {request.get('request_index')}: LLM error: {exc}")
                request_results = [
                    CompanyCleanResult(
                        company_name_eng=e.company_name_eng,
                        company_name_local=e.company_name_local,
                        country_code=e.country_code,
                        error=f"LLM error: {exc}",
                    )
                    for e in entities
                ]

            for result in request_results:
                if result.error:
                    llm_errors += 1
                sno += 1
                result.sno = sno
                results.append(result)
            self._log(
                f"request {request.get('request_index')}: cleaned {len(request_results)} company(ies)"
            )

        report_dict = build_company_report_dict(
            source_batch_dir=str(batch_dir),
            generated_at=utc_now_iso(),
            llm=self.llm_meta,
            results=results,
            total_input_entities=int(
                report.get("total_entities", len(results) + without_scrape_data)
            ),
            entities_without_scrape_data=without_scrape_data,
            llm_errors=llm_errors,
            token_usage=usage_total,
            time_taken_seconds=time.time() - started,
        )
        write_company_outputs(batch_dir, report_dict, results)
        self._log(
            f"Done: processed {len(results)}, found {report_dict['summary']['websites_found']}, "
            f"not found {report_dict['summary']['websites_not_found']}, "
            f"skipped {without_scrape_data}, tokens {usage_total.total_tokens}"
        )
        return report_dict

    def _load_lookup(self, csv_file: Path) -> dict[str, CompanyEntityInput]:
        lookup: dict[str, CompanyEntityInput] = {}
        if csv_file and Path(csv_file).is_file():
            for entity in load_company_entities(Path(csv_file)):
                lookup[entity.company_name_eng] = entity
        return lookup

    def _log(self, message: str) -> None:
        if self.log:
            self.log(message)
