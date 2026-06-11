from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .csv_loader import load_entities
from .models import BatchRunResult, EntityInput, ScrapeDoRequestRecord, utc_now_iso
from .prompting import build_search_query, chunked

QueryBuilder = Callable[[list], str]
EntityLoader = Callable[[Path], list]
from .reporting import make_batch_dir, save_reports, write_scrapedo_response
from .scrapedo_client import ScrapeDoClient
from .settings import BASE_DIR, Settings


class BatchRunner:
    def __init__(
        self,
        settings: Settings,
        scrapedo_client: ScrapeDoClient | None = None,
        reports_root: Path | None = None,
        log: Callable[[str], None] | None = None,
        query_builder: QueryBuilder = build_search_query,
    ) -> None:
        self.settings = settings
        self.query_builder = query_builder
        self.scrapedo_client = scrapedo_client or ScrapeDoClient(
            settings.scrapedo_token,
            timeout_seconds=settings.scrapedo_timeout_seconds,
            max_retries=settings.scrapedo_max_retries,
            device=settings.scrapedo_device,
            hl=settings.scrapedo_hl,
            gl=settings.scrapedo_gl,
            google_domain=settings.scrapedo_google_domain,
            safe=settings.scrapedo_safe,
            include_html=settings.scrapedo_include_html,
        )
        self.reports_root = reports_root or BASE_DIR
        self.log = log

    def run_csv(
        self,
        csv_path: Path,
        loader: EntityLoader = load_entities,
        input_type: str = "address",
    ) -> tuple[BatchRunResult, Path]:
        entities = loader(csv_path)
        self._log(f"Loaded {len(entities)} entities from {csv_path}")
        return self.run_entities(entities, str(csv_path), input_type=input_type)

    def run_entities(
        self,
        entities: list[EntityInput],
        csv_file: str = "",
        input_type: str = "address",
    ) -> tuple[BatchRunResult, Path]:
        batch_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        batch_id = f"batch_{batch_timestamp}"
        batch_dir = make_batch_dir(self.reports_root, batch_id)
        started = time.time()
        started_at = utc_now_iso()
        request_records: list[ScrapeDoRequestRecord] = []
        groups = list(chunked(entities, self.settings.batch_size))

        self._log(f"Starting {batch_id}: {len(entities)} entities, batch size {self.settings.batch_size}")
        self._log(f"Reports directory: {batch_dir}")
        self._log(f"Scrape.do phase: {len(entities)} entities in {len(groups)} request(s)")

        for group_number, group in enumerate(groups, start=1):
            request_index = len(request_records) + 1
            query = self.query_builder(group)
            names = ", ".join(entity.entity_name for entity in group)
            request_started = time.time()
            query_chars = len(query)
            self._log(
                f"[{group_number}/{len(groups)}] Scrape.do request {request_index}: "
                f"{names} (query chars={query_chars})"
            )

            try:
                self._validate_query_size(query_chars)
                payload = self.scrapedo_client.search_google_ai_mode(query)
                response_file = write_scrapedo_response(batch_dir, request_index, payload)
                metadata = payload.get("search_metadata") or {}
                search_parameters = payload.get("search_parameters") or {}
                record = ScrapeDoRequestRecord(
                    request_index=request_index,
                    entity_names=[entity.entity_name for entity in group],
                    query=str(search_parameters.get("q") or query),
                    status=metadata.get("status") or "success",
                    request_id=metadata.get("id"),
                    json_endpoint=metadata.get("json_endpoint"),
                    raw_json_file=response_file,
                    time_taken_seconds=time.time() - request_started,
                )
                self._log(
                    f"[{group_number}/{len(groups)}] Saved raw Scrape.do response to "
                    f"{response_file} in {record.time_taken_seconds:.1f}s"
                )
            except Exception as exc:
                record = ScrapeDoRequestRecord(
                    request_index=request_index,
                    entity_names=[entity.entity_name for entity in group],
                    query=query,
                    time_taken_seconds=time.time() - request_started,
                    error=str(exc),
                )
                self._log(f"[{group_number}/{len(groups)}] ERROR: {exc}")
            request_records.append(record)

        completed_at = utc_now_iso()
        run = BatchRunResult(
            batch_id=batch_id,
            csv_file=csv_file,
            total_entities=len(entities),
            started_at=started_at,
            completed_at=completed_at,
            batch_duration_seconds=time.time() - started,
            scrapedo_request_count=len(request_records),
            request_records=request_records,
            input_type=input_type,
        )
        save_reports(run, batch_dir)
        failures = sum(1 for record in request_records if record.error)
        self._log(
            f"Done: {run.scrapedo_request_count} Scrape.do request(s), "
            f"{failures} failed, {run.batch_duration_seconds:.1f}s"
        )
        return run, batch_dir

    def _log(self, message: str) -> None:
        if self.log:
            self.log(message)

    def _validate_query_size(self, query_chars: int) -> None:
        max_query_chars = self.settings.scrapedo_max_query_chars
        if max_query_chars <= 0 or query_chars <= max_query_chars:
            return
        raise ValueError(
            f"Rendered query is {query_chars} characters, over SCRAPEDO_MAX_QUERY_CHARS={max_query_chars}. "
            "Because Scrape.do AI Mode uses a GET q parameter, very large prompts can become oversized URLs "
            "and fail as 502 Bad Gateway. Shorten prompts/search_query_template.txt, lower SCRAPEDO_BATCH_SIZE, "
            "or set SCRAPEDO_MAX_QUERY_CHARS=0 to disable this guard."
        )
