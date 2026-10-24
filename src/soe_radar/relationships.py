"""Evidence-gated parent/subsidiary relationships."""

from __future__ import annotations

import re

from sqlalchemy import select

from .models import Employer, Relationship
from .urls import public_url


def add_relationship(
    db,
    parent_name,
    child_name,
    *,
    relation_type="subsidiary",
    evidence_url,
    quote,
    evidence_date,
    status="confirmed",
):
    if not evidence_url or not quote.strip() or not evidence_date:
        raise ValueError("母子公司关系必须同时提供证据URL、原文片段和日期")
    public_url(evidence_url)
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", evidence_date):
        raise ValueError("关系证据日期需为 YYYY-MM-DD")
    with db.session() as s:
        parent = s.scalar(select(Employer).where(Employer.name == parent_name))
        child = s.scalar(select(Employer).where(Employer.name == child_name))
        if not parent or not child:
            raise ValueError("母公司或子公司未建档；不能凭名称相似新建隶属关系")
        if parent.id == child.id:
            raise ValueError("母公司和子公司不能相同")
        if child.parent_id and child.parent_id != parent.id:
            raise ValueError("子公司已关联不同母公司；请先核对历史关系")
        ancestor = parent
        visited = {child.id}
        while ancestor:
            if ancestor.id in visited:
                raise ValueError("母子公司关系不能形成循环")
            visited.add(ancestor.id)
            ancestor = s.get(Employer, ancestor.parent_id) if ancestor.parent_id else None
        record = s.scalar(
            select(Relationship).where(Relationship.parent_id == parent.id, Relationship.child_id == child.id)
        )
        if not record:
            record = Relationship(parent_id=parent.id, child_id=child.id, status=status)
            s.add(record)
        evidence = {
            "url": evidence_url,
            "quote": quote.strip()[:5000],
            "date": evidence_date,
            "status": status,
        }
        provenance = list(record.provenance or [])
        if not provenance and record.evidence_url:
            provenance.append(
                {
                    "url": record.evidence_url,
                    "quote": record.quote,
                    "date": record.evidence_date,
                    "status": record.status,
                }
            )
        if evidence not in provenance:
            provenance.append(evidence)
        record.provenance = provenance
        record.relation_type = relation_type
        if status == "confirmed" and record.status != "confirmed":
            record.evidence_url = evidence_url
            record.quote = quote.strip()[:5000]
            record.evidence_date = evidence_date
        elif not record.evidence_url:
            record.evidence_url = evidence_url
            record.quote = quote.strip()[:5000]
            record.evidence_date = evidence_date
        record.status = "confirmed" if status == "confirmed" or record.status == "confirmed" else status
        if record.status == "confirmed":
            child.parent_id = parent.id
        elif child.parent_id == parent.id:
            child.parent_id = None
        s.flush()
        return {
            "id": record.id,
            "parent": parent.name,
            "child": child.name,
            "relation_type": record.relation_type,
            "evidence_url": record.evidence_url,
            "status": record.status,
        }
