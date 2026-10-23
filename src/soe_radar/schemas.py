from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    season: str = ""
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


class DocumentInput(BaseModel):
    url: str
    title: str
    text: str
    published_at: date | None = None
    source_id: str = "manual"
    employer_hint: str = ""
    attachments: list[dict] = Field(default_factory=list)


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    saved: bool | None = None
    applied: bool | None = None
    notes: str | None = Field(default=None, max_length=10000)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    extraction: ExtractionResult | None = None
    official_url: str | None = None
    employer_proof_url: str | None = None
    target_opportunity_id: str | None = None


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
    fallback_urls: list[str] = Field(default_factory=list)


def local_now() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai"))
