from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from .company_csv_loader import load_company_entities
from .company_prompting import build_company_search_query
from .csv_loader import load_entities
from .prompting import build_search_query, chunked
from .runner import BatchRunner
from .settings import BASE_DIR, load_settings


def select_pipeline(input_type: str):
    """Return (loader, query_builder) for the given input type."""
    if input_type == "company":
        return load_company_entities, build_company_search_query
    return load_entities, build_search_query


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Scrape.do Google AI Mode responses for CSV entities.")
    parser.add_argument("--csv", required=True, help="CSV path with input entities.")
    parser.add_argument(
        "--input-type",
        choices=["address", "company"],
        default="address",
        help="address: entity_name/country/address/firm_id columns. "
        "company: ISIC/Country Code/Company Name ENG/Company Name Local columns.",
    )
    parser.add_argument("--batch-size", type=int, default=None, help="Companies per query. Defaults to SCRAPEDO_BATCH_SIZE.")
    parser.add_argument("--env-file", default=None, help="Optional .env path.")
    parser.add_argument(
        "--preview-queries",
        action="store_true",
        help="Print rendered Scrape.do query prompts and exit without API calls.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env_file = Path(args.env_file).expanduser() if args.env_file else None
    csv_path = resolve_csv_path(args.csv)
    loader, query_builder = select_pipeline(args.input_type)
    if args.preview_queries:
        entities = loader(csv_path)
        print(
            render_query_preview(
                entities,
                preview_batch_size(env_file, args.batch_size),
                query_builder=query_builder,
            )
        )
        return

    settings = load_settings(env_file, batch_size=args.batch_size)
    runner = BatchRunner(settings, log=print, query_builder=query_builder)
    run, batch_dir = runner.run_csv(csv_path, loader=loader, input_type=args.input_type)
    print(f"Batch complete: {batch_dir}")
    print(f"Total entities: {run.total_entities}")
    print(f"Scrape.do requests: {run.scrapedo_request_count}")
    print(f"Failed requests: {sum(1 for record in run.request_records if record.error)}")


def resolve_csv_path(raw_path: str) -> Path:
    csv_path = Path(raw_path).expanduser()
    if not csv_path.is_absolute():
        csv_path = BASE_DIR / csv_path
    if not csv_path.is_file():
        raise SystemExit(f"CSV file not found: {csv_path}")
    return csv_path


def preview_batch_size(env_file: Path | None, batch_size: int | None) -> int:
    load_dotenv(env_file or BASE_DIR / ".env")
    if batch_size is not None:
        return batch_size
    value = os.getenv("SCRAPEDO_BATCH_SIZE")
    return int(value) if value else 5


def render_query_preview(
    entities: list,
    batch_size: int,
    query_builder=build_search_query,
) -> str:
    sections = []
    groups = list(chunked(entities, batch_size))
    for group_number, group in enumerate(groups, start=1):
        sections.append(f"# Query {group_number}/{len(groups)} ({len(group)} entities)")
        sections.append(query_builder(group))
    return "\n\n".join(sections)
