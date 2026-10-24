"""Merge display-name variants into one employer identity without losing history."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from .coverage import split_alias
from .models import Activity, Employer, Opportunity, Relationship


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
