from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cleanup_runner import CleanupRunner
from .company_cleanup_runner import CompanyCleanupRunner
from .llm_client import GeminiClient, OpenAICompatibleClient, make_llm_client
from .settings import BASE_DIR, LLMConfig, load_llm_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean Scrape.do raw responses with an LLM and write final_report.json + CSVs."
    )
    parser.add_argument(
        "--batch-dir",
        required=True,
        help="Batch directory containing report.json and raw_scrapedo_response/.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Override input CSV path (defaults to report.json csv_file).",
    )
    parser.add_argument(
        "--input-type",
        choices=["address", "company"],
        default=None,
        help="Pipeline type. Defaults to report.json input_type, else address.",
    )
    parser.add_argument("--env-file", default=None, help="Optional .env path.")
    return parser.parse_args()


def resolve_input_type(batch_dir: Path, override: str | None) -> str:
    if override:
        return override
    try:
        report = json.loads((batch_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "address"
    value = report.get("input_type")
    return value if value in ("address", "company") else "address"


def build_client(config: LLMConfig) -> OpenAICompatibleClient | GeminiClient:
    return make_llm_client(config)


def resolve_batch_dir(raw_path: str) -> Path:
    batch_dir = Path(raw_path).expanduser()
    if not batch_dir.is_absolute():
        batch_dir = BASE_DIR / batch_dir
    if not (batch_dir / "report.json").is_file():
        raise SystemExit(f"report.json not found in batch dir: {batch_dir}")
    return batch_dir


def main() -> None:
    args = parse_args()
    env_file = Path(args.env_file).expanduser() if args.env_file else None
    batch_dir = resolve_batch_dir(args.batch_dir)
    csv_path = Path(args.csv).expanduser() if args.csv else None

    input_type = resolve_input_type(batch_dir, args.input_type)

    config = load_llm_config(env_file)
    client = build_client(config)
    llm_meta = {"provider": config.provider, "base_url": config.base_url, "model": config.model}
    runner_cls = CompanyCleanupRunner if input_type == "company" else CleanupRunner
    runner = runner_cls(client, llm_meta=llm_meta, log=print)
    report = runner.run(batch_dir, csv_path)

    summary = report["summary"]
    print(f"Cleanup complete: {batch_dir}")
    print(f"Entities processed: {summary['entities_processed']}")
    print(f"Websites found: {summary['websites_found']}")
    print(f"Websites not found: {summary['websites_not_found']}")
    print(f"Without scrape data: {summary['entities_without_scrape_data']}")
    print(f"LLM errors: {summary['llm_errors']}")
    print(f"Total tokens: {summary['token_usage']['total_tokens']}")
