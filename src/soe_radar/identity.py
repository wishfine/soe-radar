"""Merge display-name variants into one employer identity without losing history."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import select

from .coverage import split_alias
from .coverage_tasks import recompute_scope, scope_evidence
from .models import (
    Activity,
    CoverageTask,
    Employer,
    Opportunity,
    Relationship,
    SourceCandidate,
    SourceEmployerScope,
    stamp,
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
    tasks = list(
        session.scalars(
            select(CoverageTask).where(CoverageTask.scope_type == scope_type, CoverageTask.scope_id == old_id)
        )
    )
    if not tasks:
        return
    status_order = {"done": 0, "needs_review": 1, "blocked": 2, "pending": 3, "running": 4}
    kept = {}
    for task in tasks:
        new_key = _key(scope_type, new_id, task.action, task.period_key)
        conflict = kept.get(new_key) or session.scalar(
            select(CoverageTask).where(CoverageTask.task_key == new_key, CoverageTask.id != task.id)
        )
        if conflict:
            merged_ids = list(
                dict.fromkeys(
                    [
                        *((conflict.evidence or {}).get("merged_task_ids", [])),
                        task.id,
                    ]
                )
            )
            conflict.evidence = {
                **(conflict.evidence or {}),
                **(task.evidence or {}),
                "merged_task_ids": merged_ids,
            }
            if status_order.get(task.status, 0) > status_order.get(conflict.status, 0):
                conflict.status = task.status
            conflict.attempts = max(conflict.attempts, task.attempts)
            conflict.owner = conflict.owner or task.owner
            if task.due_at and (not conflict.due_at or task.due_at < conflict.due_at):
                conflict.due_at = task.due_at
            session.delete(task)
        else:
            kept[new_key] = task
    for new_key, task in kept.items():
        task.scope_id = new_id
        task.task_key = new_key
    session.flush()


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
        records = scope_evidence(existing)
        for record in scope_evidence(scope):
            if record not in records:
                records.append(record)
        legacy = any(not record.get("decided_at") for record in records)
        if legacy:
            cohorts = {cohort for record in records for cohort in (record.get("cohorts") or [])}
            conflicts = []
            for cohort in sorted(cohorts):
                statuses = {
                    record.get("status") for record in records if cohort in (record.get("cohorts") or [])
                }
                if {"confirmed", "needs_review"} <= statuses:
                    conflicts.append(cohort)
            if conflicts:
                records.append(
                    {
                        "url": "",
                        "quote": "旧记录缺少审核时间，确认与撤回冲突，保留待复核",
                        "cohorts": conflicts,
                        "batch_scope": scope.batch_scope,
                        "status": "needs_review",
                        "decided_at": stamp(),
                        "verified_at": None,
                        "merge_conflict": True,
                    }
                )
        records = sorted(
            enumerate(records),
            key=lambda item: (
                0 if not item[1].get("decided_at") else 1,
                item[1].get("decided_at") or "",
                item[0],
            ),
        )
        existing.provenance = [record for _, record in records]
        recompute_scope(existing)
        session.delete(scope)
        return
    scope.employer_id = target.id
    recompute_scope(scope)


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
