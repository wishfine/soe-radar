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
                    if parent_id == child_id:
                        s.delete(relationship)
                        continue
                    existing = s.scalar(
                        select(Relationship).where(
                            Relationship.parent_id == parent_id,
                            Relationship.child_id == child_id,
                            Relationship.id != relationship.id,
                        )
                    )
                    if existing:
                        s.delete(relationship)
                    else:
                        relationship.parent_id = parent_id
                        relationship.child_id = child_id
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
        return plans
