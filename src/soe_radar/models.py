from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return uuid4().hex


def stamp() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class Employer(Base):
    __tablename__ = "employers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String, unique=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("employers.id"))
    org_type: Mapped[str] = mapped_column(String, default="unknown")
    proof_url: Mapped[str] = mapped_column(String, default="")
    verified_at: Mapped[str | None] = mapped_column(String)
    industry: Mapped[str] = mapped_column(String, default="综合")


class Relationship(Base):
    __tablename__ = "relationships"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    parent_id: Mapped[str] = mapped_column(ForeignKey("employers.id"), index=True)
    child_id: Mapped[str] = mapped_column(ForeignKey("employers.id"), index=True)
    relation_type: Mapped[str] = mapped_column(String, default="subsidiary")
    evidence_url: Mapped[str] = mapped_column(String)
    quote: Mapped[str] = mapped_column(Text)
    evidence_date: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="confirmed")
    provenance: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String, default=stamp)
    __table_args__ = (UniqueConstraint("parent_id", "child_id"),)


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    employer_id: Mapped[str] = mapped_column(ForeignKey("employers.id"))
    identity: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(String)
    cohorts: Mapped[list] = mapped_column(JSON, default=list)
    season: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, default=stamp)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id"))
    name: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("activity_id", "name"),)


class Opportunity(Base):
    __tablename__ = "opportunities"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id"))
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    employer_id: Mapped[str] = mapped_column(ForeignKey("employers.id"))
    scope: Mapped[str] = mapped_column(String, default="本批次")
    title: Mapped[str] = mapped_column(String)
    source_id: Mapped[str] = mapped_column(String, index=True)
    official_url: Mapped[str] = mapped_column(String)
    apply_url: Mapped[str] = mapped_column(String, default="")
    cohorts: Mapped[list] = mapped_column(JSON, default=list)
    season: Mapped[str] = mapped_column(String, default="")
    recruitment_type: Mapped[str] = mapped_column(String, default="unknown")
    locations: Mapped[list] = mapped_column(JSON, default=list)
    majors: Mapped[list] = mapped_column(JSON, default=list)
    education: Mapped[list] = mapped_column(JSON, default=list)
    positions: Mapped[list] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String, default="pending", index=True)
    lifecycle: Mapped[str] = mapped_column(String, default="unknown")
    latest_deadline_id: Mapped[str | None] = mapped_column(String)
    extraction: Mapped[dict] = mapped_column(JSON, default=dict)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    published_at: Mapped[str | None] = mapped_column(String)
    first_seen: Mapped[str] = mapped_column(String, default=stamp)
    last_checked: Mapped[str] = mapped_column(String, default=stamp)
    updated_at: Mapped[str] = mapped_column(String, default=stamp)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_into_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"))
    __table_args__ = (UniqueConstraint("batch_id", "scope"),)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(String, index=True)
    url: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(String)
    fetched_at: Mapped[str] = mapped_column(String, default=stamp)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_path: Mapped[str] = mapped_column(String, default="")
    extraction: Mapped[dict | None] = mapped_column(JSON)
    parser_version: Mapped[str] = mapped_column(String, default="")
    parse_attempt_at: Mapped[str | None] = mapped_column(String)
    opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"))
    parse_state: Mapped[str] = mapped_column(String, default="pending")
    __table_args__ = (UniqueConstraint("url", "digest"),)


class JobPosting(Base):
    __tablename__ = "job_postings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    locations: Mapped[list] = mapped_column(JSON, default=list)
    majors: Mapped[list] = mapped_column(JSON, default=list)
    education: Mapped[list] = mapped_column(JSON, default=list)


class Deadline(Base):
    __tablename__ = "deadlines"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    kind: Mapped[str] = mapped_column(String)
    value: Mapped[str | None] = mapped_column(String)
    quote: Mapped[str] = mapped_column(Text, default="")
    start: Mapped[int] = mapped_column(Integer, default=-1)
    end: Mapped[int] = mapped_column(Integer, default=-1)
    scope: Mapped[str] = mapped_column(String, default="本批次")
    year_basis: Mapped[str] = mapped_column(String, default="")
    verified_by: Mapped[str] = mapped_column(String, default="auto")
    created_at: Mapped[str] = mapped_column(String, default=stamp)


class ChangeEvent(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    kind: Mapped[str] = mapped_column(String)
    old_value: Mapped[str | None] = mapped_column(String)
    new_value: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=stamp)


class PersonalState(Base):
    __tablename__ = "personal_states"
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), primary_key=True)
    saved: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")


class ReviewRecord(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"))
    decision: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=stamp)


class SourceState(Base):
    __tablename__ = "source_states"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, default="unverified")
    last_success: Mapped[str | None] = mapped_column(String)
    last_attempt: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str] = mapped_column(String, default="")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_count: Mapped[int] = mapped_column(Integer, default=0)
    peak_count: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    network_attempts: Mapped[list] = mapped_column(JSON, default=list)


class RegistrySnapshot(Base):
    __tablename__ = "registry_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    registry_id: Mapped[str] = mapped_column(String, index=True)
    digest: Mapped[str] = mapped_column(String)
    source_url: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[str] = mapped_column(String, default=stamp)
    entries: Mapped[list] = mapped_column(JSON, default=list)
    raw_path: Mapped[str] = mapped_column(String)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("registry_id", "digest"),)


class RegistryState(Base):
    __tablename__ = "registry_states"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    active_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("registry_snapshots.id"))
    status: Mapped[str] = mapped_column(String, default="unverified")
    last_attempt: Mapped[str | None] = mapped_column(String)
    last_success: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str] = mapped_column(String, default="")


class CrawlRun(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    source_ids: Mapped[list | None] = mapped_column(JSON)
    discover: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[str | None] = mapped_column(String)
    finished_at: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=stamp)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String, index=True)
    prompt_version: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="reserved")
    created_at: Mapped[str] = mapped_column(String, default=stamp)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str] = mapped_column(String, default="")


class Lease(Base):
    __tablename__ = "leases"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    owner: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)


class ProviderState(Base):
    __tablename__ = "provider_states"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    paused_until: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String, default="")


class DiscoveryCall(Base):
    __tablename__ = "discovery_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String, default=stamp)
    query: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="reserved")
