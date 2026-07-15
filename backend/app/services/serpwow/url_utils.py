# backend/app/services/serpwow/url_utils.py
"""Domain/URL normalization, plausibility, and disallow-list checks for SerpWow."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

from app.services.serpwow.geo import _country_to_gl


def _normalized_domain(url_or_domain: str) -> str:
    value = (url_or_domain or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def _normalize_url_for_compare(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    return f"{scheme}://{host}{path}"


def _candidate_domain_is_plausible_for_company(domain: str, company_name: str, country: str) -> bool:
    host = _normalized_domain(domain)
    if not host:
        return False

    company_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", (company_name or "").lower())
        if len(token) >= 4 and token not in {"company", "corporation", "limited", "ltd", "group", "trading"}
    ]
    if any(token in host for token in company_tokens):
        return True

    # Country-code TLD alone is weak for short/generic company names.
    # Only treat it as plausible when we also have meaningful company tokens.
    country_gl = _country_to_gl(country)
    if company_tokens and country_gl and host.endswith(f".{country_gl}"):
        return True

    return False


def _official_website_looks_plausible(url: str, company_name: str, country: str) -> bool:
    host = _normalized_domain(url)
    if not host:
        return False
    return _candidate_domain_is_plausible_for_company(host, company_name, country)


def is_disallowed_official_url(url: Optional[str]) -> bool:
    if not url:
        return True

    value = url.strip().lower()
    if not value.startswith(("http://", "https://")):
        return True

    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if "google." in host or host.endswith(".google"):
        return True

    blocked_domains = (
        "gstatic.com",
        "youtube.com",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "wikipedia.org",
        "zoominfo.com",
        "crunchbase.com",
        "bloomberg.com",
        "dnb.com",
        "dandb.com",
        "rocketreach.co",
        "volza.com",
        "opencorporates.com",
        "zaubacorp.com",
        "bangladeshyp.com",
        "globalsuppliersonline.com",
        "eximpedia.app",
        "go4worldbusiness.com",
        "yellowpages.com",
        "yelp.com",
        "manta.com",
        "ecohubmap.com",
        "infobel.ba",
        "biz-gid.com",
        "exportgenius.in",
        "scribd.com",
    )
    if any(host == domain or host.endswith(f".{domain}") for domain in blocked_domains):
        return True

    # Reject direct file/document URLs as official websites.
    file_extensions = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".csv",
        ".ppt",
        ".pptx",
        ".pps",
        ".ppsx",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
        ".txt",
        ".zip",
        ".rar",
        ".7z",
    )
    path = (parsed.path or "").lower()
    disallowed_path_markers = (
        "/document/",
        "/document",
        "/searchviewer/",
        "/snmp/",
        "/wp-content/uploads/",
        "/public/storage/upload/",
    )
    if any(marker in path for marker in disallowed_path_markers):
        return True
    if any(path.endswith(ext) for ext in file_extensions):
        return True

    # Some sites expose downloadable files via query params.
    query = (parsed.query or "").lower()
    query_file_markers = ("file=", "filename=", "download=", "format=pdf", "export=pdf")
    if any(marker in query for marker in query_file_markers):
        for ext in file_extensions:
            if ext in query:
                return True

    return False


def canonicalize_official_url(url: str) -> str:
    """Normalize a URL for equality/dedup: lower scheme+host, force https, strip a
    leading www., drop fragment, normalize a bare trailing slash. Returns "" if the
    input has no host (invalid). Does NOT mutate a meaningful path."""
    if not isinstance(url, str) or not url.strip():
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or ""
    if path == "/":
        path = ""
    return urlunsplit(("https", host, path, "", ""))


def dedupe_candidate_urls(urls: list[str]) -> list[str]:
    """Drop later URLs whose canonical form already appeared; keep first-seen order
    and the ORIGINAL string (canonicalization is for comparison only)."""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        c = canonicalize_official_url(u)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(u)
    return out


def _domain_from_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def _normalize_website_input(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    host = (parsed.netloc or parsed.path or "").strip()
    if not host:
        return ""
    path = parsed.path if parsed.netloc else ""
    normalized = f"{parsed.scheme or 'https'}://{host}{path}"
    return normalized.rstrip("/")


def x_domain_from_input_url(input_url: str) -> str:
    """Registrable-ish host of Company X's portfolio page (spec §3.1).

    Used to hard-blacklist X's own site from Y's candidate URLs — the probe's
    worst failure mode was returning X's website as Y's. Returns "" when the
    input is blank or unparseable (no scheme -> no netloc).
    """
    value = str(input_url or "").strip()
    if not value:
        return ""
    try:
        host = (urlparse(value).netloc or "").strip().lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else ""


def url_matches_domain(url: str, domain: str) -> bool:
    """True when url's host equals `domain` or is a subdomain of it."""
    dom = str(domain or "").strip().lower()
    if not dom:
        return False
    try:
        host = (urlparse(str(url or "").strip()).netloc or "").strip().lower()
    except ValueError:
        return False
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host == dom or host.endswith("." + dom)
