"""Conservative local repairs. Preserve evidence and redirect duplicates instead of deleting."""

import hashlib
import json
import re
from collections import defaultdict

from sqlalchemy import select

from .extraction import extract_rules
from .models import (
    Batch,
    ChangeEvent,
    Deadline,
    Document,
    JobPosting,
    Opportunity,
    PersonalState,
    ReviewRecord,
)
from .schemas import DocumentInput
from .store import documents_for
from .urls import canonical


def saved_input(doc, cfg):
    path = cfg.data_dir / doc.snapshot_path if doc.snapshot_path else None
    if path and path.is_file():
        return DocumentInput.model_validate_json(path.read_text())
    return DocumentInput(
        url=doc.url,
        title=doc.title,
        text=doc.text,
        published_at=doc.published_at,
        source_id=doc.source_id,
        attachments=doc.attachments,
        employer_hint=(doc.extraction or {}).get("employer", ""),
    )


def label_group(doc):
    result = extract_rules(doc)
    title = re.sub(r"[-—_|]\s*国家大学生就业服务平台.*$", "", doc.title)
    title = re.sub(r"\s+", "", title)
    return hashlib.sha256(json.dumps([title, result.cohorts], ensure_ascii=False).encode()).hexdigest()


def repair(db, cfg, *, apply=False):
    groups = defaultdict(list)
    with db.session() as s:
        rows = list(s.scalars(select(Opportunity).where(Opportunity.merged_into_id.is_(None))))
        for row in rows:
            years = (
                row.cohorts
                or extract_rules(DocumentInput(url=row.official_url, title=row.title, text="")).cohorts
            )
            scope = row.scope
            if re.fullmatch(
                r"(?:20\d{2}[届年]?)?(?:春季|秋季|春招|秋招)?(?:校园招聘|校招|全部岗位|本次招聘)", scope
            ):
                scope = "本批次"
            season = "春招" if "春" in row.title else "秋招" if "秋" in row.title else ""
            period = row.published_at[:4] if not years and row.published_at else ""
            groups[
                (
                    canonical(row.official_url),
                    tuple(sorted(years)),
                    s.get(Batch, row.batch_id).name,
                    scope,
                    season,
                    period,
                )
            ].append(row)
        merged = []
        for key, members in groups.items():
            if len(members) < 2:
                continue
            members.sort(
                key=lambda r: (
                    r.review_status in ("manual", "rejected"),
                    r.review_status == "auto",
                    len(documents_for(s, r)) > 0,
                    r.updated_at,
                ),
                reverse=True,
            )
            winner = members[0]
            for loser in members[1:]:
                merged.append({"from": loser.id, "to": winner.id})
                if not apply:
                    continue
                for model in (Document, Deadline, ChangeEvent, JobPosting, ReviewRecord):
                    for record in s.scalars(select(model).where(model.opportunity_id == loser.id)):
                        record.opportunity_id = winner.id
                old = s.get(PersonalState, loser.id)
                if old:
                    current = s.get(PersonalState, winner.id) or PersonalState(
                        opportunity_id=winner.id, saved=False, applied=False, notes=""
                    )
                    current.saved = current.saved or old.saved
                    current.applied = current.applied or old.applied
                    if old.notes and old.notes not in current.notes:
                        current.notes = (current.notes + "\n[合并保留的备注] " + old.notes).strip()
                    s.add(current)
                loser.merged_into_id = winner.id
                loser.review_status = "merged"
                loser.needs_review = False
                loser.review_reasons = ["重复记录已合并至：" + winner.id]
                s.add(
                    ReviewRecord(
                        opportunity_id=winner.id,
                        decision="merge",
                        payload={"merged_record": loser.id, "reason": "同一规范地址、届别、批次与范围"},
                    )
                )
        return {
            "applied": apply,
            "merged": merged,
            "records_before": len(rows),
            "records_after": len(rows) - len(merged),
        }


async def reparse(db, cfg):
    from .sync import Pipeline

    pipe = Pipeline(cfg, db, use_models=False)
    with db.session() as s:
        docs = list(s.scalars(select(Document).order_by(Document.fetched_at.desc())))
        selected = {}
        for doc in docs:
            value = saved_input(doc, cfg)
            years = extract_rules(value).cohorts
            selected.setdefault((canonical(doc.url), tuple(years)), (doc.fetched_at, value))
    totals = {"documents": len(selected), "excluded": 0, "pending": 0, "errors": []}
    try:
        for fetched, doc in selected.values():
            try:
                result = await pipe.process(doc, live=False)
                totals["excluded"] += bool(result.get("excluded"))
                totals["pending"] += result.get("review_status") == "pending"
                if result.get("id"):
                    with db.session() as s:
                        s.get(Opportunity, result["id"]).last_checked = fetched
            except Exception as exc:
                totals["errors"].append({"url": doc.url, "reason": str(exc)})
    finally:
        await pipe.close()
    return totals
