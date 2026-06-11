import argparse
import json
import os
from typing import Any

import httpx

API_URL = "https://api.serpwow.com/live/search"


def load_local_env(env_path: str = ".env") -> None:
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value


load_local_env()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Look up address, phone, email, industry, products, services for a domain."
    )
    parser.add_argument(
        "domain",
        nargs="?",
        default=os.getenv("SERPWOW_DOMAIN", "frabelle.com"),
        help="Domain to look up (e.g. frabelle.com). Defaults to SERPWOW_DOMAIN env var or frabelle.com.",
    )
    return parser.parse_args()


def build_query(domain: str) -> str:
    return f"What is the address, phone, email, industry, products, services of {domain}"


def build_fallback_query(domain: str) -> str:
    return f"address phone email industry products services {domain}"


def build_params(query: str, gl: str | None = None) -> dict[str, str]:
    params = {
        "api_key": os.getenv("SERPWOW_API_KEY", ""),
        "q": query,
        "hl": "en",
        "engine": "google",
        "include_ai_overview": "true",
    }
    if gl and str(gl).strip():
        params["gl"] = str(gl).strip().lower()
    return params


def get_ai_overview(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("ai_overview", {}) or {}


def ai_overview_is_placeholder(ai_overview: dict[str, Any]) -> bool:
    contents = ai_overview.get("ai_overview_contents")
    if not isinstance(contents, list):
        return False
    for item in contents:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip().lower()
        if "something went wrong with this response" in text:
            return True
    return False


def fetch_serpwow(query: str, gl: str | None = None) -> dict[str, Any]:
    params = build_params(query, gl=gl)
    with httpx.Client(timeout=120.0) as client:
        response = client.get(API_URL, params=params)
        response.raise_for_status()
    print("\nRequest URL:")
    print(response.url)
    return response.json()


def main() -> None:
    args = parse_args()
    domain = args.domain

    if not os.getenv("SERPWOW_API_KEY"):
        print("❌ SERPWOW_API_KEY missing in .env")
        return

    query = build_query(domain)
    print(f"\n🔍 Looking up: {domain}")

    try:
        data = fetch_serpwow(query)
    except httpx.HTTPError as exc:
        print(f"❌ SerpWow request error: {exc}")
        return

    request_info = data.get("request_info", {})
    if request_info.get("success") is not True:
        print("❌ SerpWow request failed:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    ai_overview = get_ai_overview(data)

    # Retry once with a narrower query if SerpWow returns placeholder AI overview.
    if ai_overview_is_placeholder(ai_overview):
        fallback_query = build_fallback_query(domain)
        print("\nℹ️ Placeholder AI overview received. Retrying with fallback query...")
        try:
            data_retry = fetch_serpwow(fallback_query)
            request_info_retry = data_retry.get("request_info", {})
            if request_info_retry.get("success") is True:
                ai_overview_retry = get_ai_overview(data_retry)
                if ai_overview_retry and not ai_overview_is_placeholder(ai_overview_retry):
                    data = data_retry
                    ai_overview = ai_overview_retry
        except httpx.HTTPError as exc:
            print(f"ℹ️ Retry failed: {exc}")

    print("\n✅ Full ai_overview:\n")
    print(json.dumps(ai_overview, indent=2, ensure_ascii=False))

    if not ai_overview:
        print("\nℹ️ ai_overview missing; full response:\n")
        print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
