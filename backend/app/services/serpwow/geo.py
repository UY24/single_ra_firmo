# backend/app/services/serpwow/geo.py
"""Country name/code -> Google ``gl`` locale mapping used across SerpWow queries."""
from __future__ import annotations

import re
from typing import Optional

COUNTRY_GL_ALIASES: dict[str, str] = {
    "united states": "us",
    "usa": "us",
    "u.s.a": "us",
    "u.s.": "us",
    "us": "us",
    "united kingdom": "gb",
    "uk": "gb",
    "great britain": "gb",
    "england": "gb",
    "gb": "gb",
    "india": "in",
    "in": "in",
    "bangladesh": "bd",
    "bd": "bd",
    "canada": "ca",
    "ca": "ca",
    "australia": "au",
    "au": "au",
    "germany": "de",
    "de": "de",
    "france": "fr",
    "fr": "fr",
    "italy": "it",
    "it": "it",
    "spain": "es",
    "es": "es",
    "netherlands": "nl",
    "nl": "nl",
    "sweden": "se",
    "se": "se",
    "norway": "no",
    "no": "no",
    "denmark": "dk",
    "dk": "dk",
    "finland": "fi",
    "fi": "fi",
    "japan": "jp",
    "jp": "jp",
    "south korea": "kr",
    "korea": "kr",
    "kr": "kr",
    "china": "cn",
    "cn": "cn",
    "singapore": "sg",
    "sg": "sg",
    "united arab emirates": "ae",
    "uae": "ae",
    "ae": "ae",
    "saudi arabia": "sa",
    "sa": "sa",
    "qatar": "qa",
    "qa": "qa",
    "kuwait": "kw",
    "kw": "kw",
    "oman": "om",
    "om": "om",
    "bahrain": "bh",
    "bh": "bh",
    "ireland": "ie",
    "ie": "ie",
    "poland": "pl",
    "pl": "pl",
    "switzerland": "ch",
    "ch": "ch",
    "austria": "at",
    "at": "at",
    "belgium": "be",
    "be": "be",
    "portugal": "pt",
    "pt": "pt",
    "mexico": "mx",
    "mx": "mx",
    "brazil": "br",
    "br": "br",
    "argentina": "ar",
    "ar": "ar",
    "south africa": "za",
    "za": "za",
    "new zealand": "nz",
    "nz": "nz",
}


def _country_to_gl(country: Optional[str]) -> str:
    value = str(country or "").strip().lower()
    if not value:
        return "us"
    if value in COUNTRY_GL_ALIASES:
        return COUNTRY_GL_ALIASES[value]
    compact = re.sub(r"[^a-z]", "", value)
    if compact in COUNTRY_GL_ALIASES:
        return COUNTRY_GL_ALIASES[compact]
    if len(compact) == 2:
        return compact
    return "us"
