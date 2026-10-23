from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeadlineKind(StrEnum):
    DATE = "date"
    DATETIME = "datetime"
    ROLLING = "rolling"
    LONG_TERM = "long_term"
    UNANNOUNCED = "unannounced"
    CONFLICT = "conflict"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: DeadlineKind = DeadlineKind.UNANNOUNCED
    value: str | None = None
    quote: str = ""
    start: int = -1
    end: int = -1
    scope: str = "本批次"
    year_basis: str = ""


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    locations: list[str] = Field(default_factory=list)
    majors: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employer: str = ""
    title: str = ""
    recruitment_type: Literal[
        "campus", "social", "internal", "intern", "postdoc", "notice", "other", "unknown"
    ] = "unknown"
    cohorts: list[int] = Field(default_factory=list)
    season: Literal["秋招", "春招", ""] = ""
    batch: str = "正式批"
    positions: list[Position] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    majors: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    application_start: str | None = None
    deadline: Claim = Field(default_factory=Claim)
    scoped_deadlines: list[Claim] = Field(default_factory=list)
    action: Literal["recruitment", "extend", "close", "reopen"] = "recruitment"
    reference_urls: list[str] = Field(default_factory=list)
    target_title: str = ""
    apply_url: str = ""
    urgency_note: str = ""
    review_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_campaign(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value["season"] = {"秋季": "秋招", "春季": "春招", "秋季招聘": "秋招", "春季招聘": "春招"}.get(
                value.get("season"), value.get("season", "")
            )
            claim = value.get("deadline")
            if isinstance(claim, dict) and re.fullmatch(
                r"(?:20\d{2}[届年]?)?(?:春季|秋季|春招|秋招)?(?:校园招聘|校招|本次招聘|全部岗位|本次校招)",
                claim.get("scope", ""),
            ):
                value["deadline"] = {**claim, "scope": "本批次"}
        return value


class DocumentInput(BaseModel):
    url: str
    title: str
    text: str
    published_at: date | None = None
    source_id: str = "manual"
    employer_hint: str = ""
    attachments: list[dict] = Field(default_factory=list)
    structured: dict = Field(default_factory=dict)


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    saved: bool | None = None
    applied: bool | None = None
    notes: str | None = Field(default=None, max_length=10000)


class ManualEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attachment_url: str
    location: str = Field(min_length=2, max_length=500)
    quote: str = Field(min_length=2, max_length=10000)
    scope: str = "本批次"


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    extraction: ExtractionResult | None = None
    official_url: str | None = None
    employer_proof_url: str | None = None
    target_opportunity_id: str | None = None
    snapshot_id: str | None = None
    use_snapshot: bool = False
    published_at: date | None = None
    publication_evidence: str = Field(default="", max_length=2000)
    evidence: list[ManualEvidence] = Field(default_factory=list, max_length=20)
    resolution_note: str = Field(default="", max_length=5000)
    parent_employer_id: str | None = None
    relationship_proof_url: str | None = None


class SyncInput(BaseModel):
    source_ids: list[str] | None = None
    discover: bool = False


class SourceSpec(BaseModel):
    id: str
    name: str
    url: str
    domains: list[str]
    employer: str = ""
    aliases: list[str] = Field(default_factory=list)
    industry: str = "综合"
    policy: str = "company"
    adapter: str = "html"
    enabled: bool = True
    browser: bool = False
    list_selector: str | None = None
    api_items_path: str = ""
    api_fields: dict[str, str] = Field(default_factory=dict)
    api_url_template: str = ""
    api_next_param: str = ""
    api_required_fields: dict[str, str] = Field(default_factory=dict)
    api_method: Literal["GET", "POST"] = "GET"
    api_body: dict = Field(default_factory=dict)
    fallback_urls: list[str] = Field(default_factory=list)
    entry_is_document: bool = False


def local_now() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai"))
