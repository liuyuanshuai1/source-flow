from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class AnalyzeRequest(BaseModel):
    repository_url: HttpUrl


class Score(BaseModel):
    key: Literal["private_deployment", "implementation", "saas", "desktop"]
    label: str
    value: int = Field(ge=0, le=100)
    rationale: str


class Evidence(BaseModel):
    label: str
    detail: str
    source: str
    url: str | None = None


class LicenseAssessment(BaseModel):
    status: Literal["green", "yellow", "red"]
    detected: str
    summary: str
    manual_review_required: bool


class OpportunityReport(BaseModel):
    id: str
    repository: str
    repository_url: str
    description: str
    language: str
    stars: int | None = None
    updated_at: str | None = None
    analyzed_at: datetime
    is_sample: bool = False
    verdict: Literal["worth_validating", "watch", "pass"]
    verdict_label: str
    recommended_path: str
    buyer: str
    paid_reason: str
    opportunity_summary: str
    scores: list[Score]
    license: LicenseAssessment
    paid_features: list[str]
    delivery_plan: list[str]
    risks: list[str]
    evidence: list[Evidence]
    seven_day_plan: list[str]
    confidence: int = Field(ge=0, le=100)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    analyzer: str
