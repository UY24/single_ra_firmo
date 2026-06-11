from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Protocol

from .cleanup_reporting import build_report_dict, write_outputs
from .csv_loader import load_entities
from .extraction import build_messages, parse_results
from .models import EntityCleanResult, TokenUsage, utc_now_iso


class LLMClient(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> tuple[dict, TokenUsage]: ...


class CleanupRunner:
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
        location_by_name, country_by_name = self._load_entity_lookup(csv_file)

        results: list[EntityCleanResult] = []
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
                    f"skipping {len(names)} entity(ies)"
                )
                continue

            raw = json.loads((batch_dir / raw_file).read_text(encoding="utf-8"))
            messages = build_messages(names, raw.get("text_blocks"), raw.get("references"))
            try:
                parsed, usage = self.llm_client.complete_json(messages)
                usage_total = usage_total + usage
                request_results = parse_results(parsed, names)
            except Exception as exc:  # noqa: BLE001 - record per-request failure
                self._log(f"request {request.get('request_index')}: LLM error: {exc}")
                request_results = [
                    EntityCleanResult(entity_name=name, error=f"LLM error: {exc}") for name in names
                ]

            for result in request_results:
                if result.error:
                    llm_errors += 1
                sno += 1
                result.sno = sno
                result.location = location_by_name.get(result.entity_name, "")
                result.country = country_by_name.get(result.entity_name, "")
                results.append(result)
            self._log(
                f"request {request.get('request_index')}: cleaned {len(request_results)} entity(ies)"
            )

        report_dict = build_report_dict(
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
        write_outputs(batch_dir, report_dict, results)
        self._log(
            f"Done: processed {len(results)}, found {report_dict['summary']['websites_found']}, "
            f"not found {report_dict['summary']['websites_not_found']}, "
            f"skipped {without_scrape_data}, tokens {usage_total.total_tokens}"
        )
        return report_dict

    def _load_entity_lookup(self, csv_file: Path) -> tuple[dict[str, str], dict[str, str]]:
        location_by_name: dict[str, str] = {}
        country_by_name: dict[str, str] = {}
        if csv_file and Path(csv_file).is_file():
            for entity in load_entities(Path(csv_file)):
                location_by_name[entity.entity_name] = entity.address
                country_by_name[entity.entity_name] = entity.country
        return location_by_name, country_by_name

    def _log(self, message: str) -> None:
        if self.log:
            self.log(message)
