"""Merge display-name variants into one employer identity without losing history."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import select

from .coverage import split_alias
from .models import (
    Activity,
    CoverageTask,
    Employer,
    Opportunity,
    Relationship,
    SourceCandidate,
    SourceEmployerScope,
)


def identity_key(name: str) -> str:
    canonical, _ = split_alias(name)
    return canonical.replace("（", "(").replace("）", ")").replace(" ", "")


def _target_score(employer):
    canonical, _ = split_alias(employer.name)
    return (
        1 if canonical == employer.name else 0,
        employer.verified_at or "",
        len(employer.aliases or []),
        -len(employer.name),
    )


def _relationship_evidence(record):
    values = list(record.provenance or [])
    if not values and record.evidence_url:
        values.append(
            {
                "url": record.evidence_url,
                "quote": record.quote,
                "date": record.evidence_date,
                "status": record.status,
            }
        )
    return values


def _merge_relationship(session, relationship, parent_id, child_id):
    if parent_id == child_id:
        session.delete(relationship)
        return
    existing = session.scalar(
        select(Relationship).where(
            Relationship.parent_id == parent_id,
            Relationship.child_id == child_id,
            Relationship.id != relationship.id,
        )
    )
    if not existing:
        relationship.parent_id = parent_id
        relationship.child_id = child_id
        return
    provenance = _relationship_evidence(existing)
    for item in _relationship_evidence(relationship):
        if item not in provenance:
            provenance.append(item)
    existing.provenance = provenance
    if existing.status != "confirmed" and relationship.status == "confirmed":
        existing.status = "confirmed"
        existing.evidence_url = relationship.evidence_url
        existing.quote = relationship.quote
        existing.evidence_date = relationship.evidence_date
    elif not existing.evidence_url:
        existing.evidence_url = relationship.evidence_url
        existing.quote = relationship.quote
        existing.evidence_date = relationship.evidence_date
    session.delete(relationship)


def _safe_parent(session, target, candidate):
    if not candidate or candidate == target.id:
        return None
    ancestor = session.get(Employer, candidate)
    visited = {target.id}
    while ancestor:
        if ancestor.id in visited:
            return None
        visited.add(ancestor.id)
        ancestor = session.get(Employer, ancestor.parent_id) if ancestor.parent_id else None
    return candidate


def _key(*parts):
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def _remap_task(session, scope_type, old_id, new_id):
    for task in list(
        session.scalars(
            select(CoverageTask).where(CoverageTask.scope_type == scope_type, CoverageTask.scope_id == old_id)
        )
    ):
        task.scope_id = new_id
        task.task_key = _key(scope_type, new_id, task.action, task.period_key)
        conflict = session.scalar(
            select(CoverageTask).where(CoverageTask.task_key == task.task_key, CoverageTask.id != task.id)
        )
        if conflict:
            conflict.evidence = {
                **(conflict.evidence or {}),
                **(task.evidence or {}),
                "merged_task_ids": list(
                    dict.fromkeys(
                        [
                            *((conflict.evidence or {}).get("merged_task_ids", [])),
                            task.id,
                        ]
                    )
                ),
            }
            session.delete(task)


def _merge_candidate(session, candidate, target):
    existing = session.scalar(
        select(SourceCandidate).where(
            SourceCandidate.employer_id == target.id,
            SourceCandidate.url == candidate.url,
        )
    )
    if existing:
        order = {"lead": 0, "identity_checked": 1, "readable": 2, "verified": 3, "blocked": 4, "rejected": 5}
        if order.get(candidate.status, 0) > order.get(existing.status, 0):
            existing.status = candidate.status
        existing.list_result = existing.list_result or candidate.list_result
        existing.detail_result = existing.detail_result or candidate.detail_result
        existing.attachment_result = existing.attachment_result or candidate.attachment_result
        existing.evidence_path = existing.evidence_path or candidate.evidence_path
        _remap_task(session, "source_candidate", candidate.id, existing.id)
        session.delete(candidate)
        return existing.id
    candidate.employer_id = target.id
    candidate.candidate_key = _key(target.id, candidate.url)
    _remap_task(session, "source_candidate", candidate.id, candidate.id)
    return candidate.id


def _merge_scope(session, scope, target):
    existing = session.scalar(
        select(SourceEmployerScope).where(
            SourceEmployerScope.source_id == scope.source_id,
            SourceEmployerScope.employer_id == target.id,
            SourceEmployerScope.mode == scope.mode,
            SourceEmployerScope.batch_scope == scope.batch_scope,
        )
    )
    if existing:
        existing.quote = "\n---\n".join(
            value for value in (existing.quote, scope.quote) if value and value not in existing.quote
        )
        existing.cohorts = sorted(set((existing.cohorts or []) + (scope.cohorts or [])))
        if scope.status == "confirmed":
            existing.status = "confirmed"
            existing.verified_at = existing.verified_at or scope.verified_at
        existing.evidence_url = existing.evidence_url or scope.evidence_url
        session.delete(scope)
        return
    scope.employer_id = target.id


def merge_alias_employers(db, *, apply=False):
    groups = defaultdict(list)
    with db.session() as s:
        for employer in s.scalars(select(Employer)):
            groups[identity_key(employer.name)].append(employer)
        plans = []
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            target = max(rows, key=_target_score)
            duplicates = [row for row in rows if row.id != target.id]
            plans.append(
                {
                    "key": key,
                    "target": target.name,
                    "duplicates": [row.name for row in duplicates],
                    "target_id": target.id,
                    "duplicate_ids": [row.id for row in duplicates],
                }
            )
        if not apply:
            return plans
        for plan in plans:
            target = s.get(Employer, plan["target_id"])
            for duplicate_id in plan["duplicate_ids"]:
                duplicate = s.get(Employer, duplicate_id)
                if not duplicate:
                    continue
                duplicate_parent_id = duplicate.parent_id
                for opportunity in s.scalars(
                    select(Opportunity).where(Opportunity.employer_id == duplicate.id)
                ):
                    opportunity.employer_id = target.id
                for activity in s.scalars(select(Activity).where(Activity.employer_id == duplicate.id)):
                    activity.employer_id = target.id
                for child in s.scalars(select(Employer).where(Employer.parent_id == duplicate.id)):
                    child.parent_id = target.id
                for candidate in list(
                    s.scalars(select(SourceCandidate).where(SourceCandidate.employer_id == duplicate.id))
                ):
                    _merge_candidate(s, candidate, target)
                for scope in list(
                    s.scalars(
                        select(SourceEmployerScope).where(SourceEmployerScope.employer_id == duplicate.id)
                    )
                ):
                    _merge_scope(s, scope, target)
                for candidate in s.scalars(select(SourceCandidate)):
                    ids = [
                        target.id if value == duplicate.id else value
                        for value in (candidate.scope_employer_ids or [])
                    ]
                    candidate.scope_employer_ids = list(dict.fromkeys(ids))
                _remap_task(s, "employer", duplicate.id, target.id)
                for relationship in list(
                    s.scalars(
                        select(Relationship).where(
                            (Relationship.parent_id == duplicate.id) | (Relationship.child_id == duplicate.id)
                        )
                    )
                ):
                    parent_id = (
                        target.id if relationship.parent_id == duplicate.id else relationship.parent_id
                    )
                    child_id = target.id if relationship.child_id == duplicate.id else relationship.child_id
                    _merge_relationship(s, relationship, parent_id, child_id)
                # Relationship rows must be written before the duplicate employer is removed.
                s.flush()

                confirmed = list(
                    s.scalars(
                        select(Relationship).where(
                            Relationship.child_id == target.id,
                            Relationship.status == "confirmed",
                        )
                    )
                )
                parent_ids = {row.parent_id for row in confirmed}
                chosen = target.parent_id or duplicate_parent_id
                if chosen is None and parent_ids:
                    chosen = next(iter(parent_ids))
                if chosen and chosen not in parent_ids and chosen != target.parent_id:
                    # A needs_review-only parent link is not promoted to the employer pointer.
                    chosen = None
                chosen = _safe_parent(s, target, chosen)
                target.parent_id = chosen
                for relationship in confirmed:
                    if relationship.parent_id != chosen:
                        relationship.status = "needs_review"
                        provenance = _relationship_evidence(relationship)
                        provenance.append(
                            {
                                "note": "合并后发现多个已确认母公司，保留证据但降为待复核。",
                                "date": relationship.evidence_date,
                            }
                        )
                        relationship.provenance = provenance
                target.aliases = list(
                    dict.fromkeys(
                        [
                            *(target.aliases or []),
                            duplicate.name,
                            *(duplicate.aliases or []),
                        ]
                    )
                )
                target.aliases = [value for value in target.aliases if value != target.name]
                if target.org_type != "soe" and duplicate.org_type == "soe":
                    target.org_type = duplicate.org_type
                    target.proof_url = target.proof_url or duplicate.proof_url
                    target.verified_at = target.verified_at or duplicate.verified_at
                if not target.proof_url:
                    target.proof_url = duplicate.proof_url
                if not target.verified_at:
                    target.verified_at = duplicate.verified_at
                if target.industry == "综合" and duplicate.industry != "综合":
                    target.industry = duplicate.industry
                s.delete(duplicate)
                s.flush()
        return plans
