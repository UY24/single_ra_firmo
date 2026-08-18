# backend/app/services/serpwow/schemas.py
"""Pydantic request/response models for SerpWow crawl endpoints + executors."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class CrawlRequest(BaseModel):
    company_name: str
    country: str
    firm_id: Optional[str] = None
    industry: Optional[str] = None
    full_address: Optional[str] = None


class FirmographicsRequest(BaseModel):
    official_website: str
    company_name: Optional[str] = None
    country: Optional[str] = None
    firm_id: Optional[str] = None
    industry: Optional[str] = None
    full_address: Optional[str] = None


class CrawlResponse(BaseModel):
    company_name: str
    country: str
    firm_id: Optional[str] = None
    input_industry: Optional[str] = None
    input_full_address: Optional[str] = None
    official_website: Optional[str]
    summary: str
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None
    products: list[str] = []
    services: list[str] = []
    website_company_descirption_ai: Optional[str] = None
    website_company_descirption_translated_ai: Optional[str] = None
    # Provider-specific costs are Optional: a pipeline that doesn't use a provider
    # leaves it None ("not applicable") rather than reporting a misleading $0.00.
    # gmaps runs on scrape.do (credits, not USD) and sets neither.
    massive_proxy_cost_usd: Optional[float] = None
    serpwow_cost_usd: Optional[float] = None
    gemini_cost_usd: float
    total_cost_usd: float
    context: dict[str, Any]
