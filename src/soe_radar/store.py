from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime

from sqlalchemy import delete, select

from .config import Settings
from .db import Database
from .extraction import PARSER_VERSION, validate_extraction
from .models import (
    Activity,
    Batch,
    ChangeEvent,
    Deadline,
    Document,
    Employer,
    JobPosting,
    Opportunity,
    PersonalState,
    ReviewRecord,
    stamp,
)
from .schemas import DocumentInput, ExtractionResult, local_now
from .urls import canonical, on_domains, public_url


def digest_document(doc: DocumentInput) -> str:
    data = {
        "text": doc.text,
        "title": doc.title,
        "published_at": str(doc.published_at),
        "attachments": doc.attachments,
        "employer": doc.employer_hint,
        "structured": doc.structured,
    }
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _normal_title(title: str) -> str:
    return re.sub(r"[\s：:—\-【】《》()（）]", "", title)


def _event_core(title: str) -> str:
    text = _normal_title(title).removeprefix("关于")
    return re.sub(
        r"(?:延长|延期|提前结束|提前关闭|重新开放|恢复报名|取消招聘|终止招聘).*", "", text
    ).removesuffix("公告")


def _event_target(s, result: ExtractionResult, employer: Employer):
    matches = list(s.scalars(select(Opportunity).where(Opportunity.employer_id == employer.id)))
    matches = [x for x in matches if x.cohorts == result.cohorts and x.scope == result.deadline.scope]
    references = {canonical(x) for x in result.reference_urls if x.startswith(("http://", "https://"))}
    by_url = [x for x in matches if canonical(x.official_url) in references]
    if len(by_url) == 1:
        return by_url[0]
    core = _event_core(result.target_title or result.title)
    matches = [x for x in matches if len(core) >= 8 and _event_core(x.title) == core]
    return matches[0] if len(matches) == 1 else None


def documents_for(s, opportunity):
    ids = set(s.scalars(select(Deadline.document_id).where(Deadline.opportunity_id == opportunity.id)))
    return list(
        s.scalars(
            select(Document)
            .where((Document.opportunity_id == opportunity.id) | Document.id.in_(ids))
            .order_by(Document.fetched_at.desc())
        )
    )


def ingest(
    db: Database,
    cfg: Settings,
    doc: DocumentInput,
    result: ExtractionResult,
    *,
    manual=False,
    proof_url="",
    target_id: str | None = None,
    review=None,
    record_id: str | None = None,
    excluded=False,
) -> dict:
    url = canonical(doc.url)
    source = next((x for x in cfg.sources if x.id == doc.source_id), None)
    official = bool(source and on_domains(url, source.domains))
    fingerprint = digest_document(doc)
    with db.session() as s:
        version = s.scalar(select(Document).where(Document.url == url, Document.digest == fingerprint))
        if (
            version
            and version.opportunity_id
            and version.parse_state == "done"
            and version.parser_version == PARSER_VERSION
            and not manual
        ):
            return serialize(s, s.get(Opportunity, version.opportunity_id), cfg)
        if (
            version
            and version.opportunity_id
            and not manual
            and version.extraction == result.model_dump(mode="json")
            and version.parser_version == PARSER_VERSION
        ):
            return serialize(s, s.get(Opportunity, version.opportunity_id), cfg)
        if not version:
            version = Document(
                url=url,
                source_id=doc.source_id,
                title=doc.title,
                text=doc.text,
                published_at=str(doc.published_at) if doc.published_at else None,
                digest=fingerprint,
                attachments=doc.attachments,
            )
            s.add(version)
            s.flush()
            folder = cfg.data_dir / "snapshots"
            folder.mkdir(exist_ok=True)
            path = folder / f"{version.id}.json"
            path.write_text(doc.model_dump_json(indent=2))
            version.snapshot_path = str(path.relative_to(cfg.data_dir))
        version.extraction = result.model_dump(mode="json")
        version.parser_version = PARSER_VERSION
        validation_doc = (
            doc.model_copy(update={"published_at": review.published_at})
            if review and review.published_at
            else doc
        )
        reasons = validate_extraction(
            result,
            validation_doc,
            manual=manual,
            evidence=review.evidence if review else (),
            resolution_note=review.resolution_note if review else "",
        )
        if (
            review
            and review.published_at
            and review.published_at != doc.published_at
            and not review.publication_evidence.strip()
        ):
            reasons.append("补充发布日期需要填写年份依据")
        public_institution = bool(re.search(r"(?:事业单位|事业编制|事业编)", doc.title + doc.text))
        if public_institution:
            reasons.append("原文涉及事业单位或事业编制，不能自动归为国有企业")
        if not official:
            reasons.append("官方公告来源尚未核验")
        employer_name = result.employer or doc.employer_hint or "招聘主体待确认"
        employer = s.scalar(select(Employer).where(Employer.name == employer_name))
        if not employer and source and source.employer:
            if employer_name == source.employer or employer_name in source.aliases:
                employer = s.scalar(select(Employer).where(Employer.name == source.employer))
        if not employer:
            for entity in s.scalars(select(Employer)):
                if employer_name in entity.aliases:
                    employer = entity
                    break
        if not employer:
            employer = Employer(name=employer_name)
            s.add(employer)
            s.flush()
        # A source-specific known employer, or the explicitly SOE-only government section, is evidence.
        if (
            official
            and not public_institution
            and source
            and (
                (source.employer and employer.name == source.employer)
                or (source.policy == "government_soe" and employer_name != "招聘主体待确认")
            )
        ):
            employer.org_type = "soe"
            employer.proof_url = source.homepage_url or source.url
            employer.verified_at = stamp()
            employer.industry = source.industry
        if manual and proof_url:
            employer.proof_url = public_url(proof_url)
            employer.org_type = "soe"
            employer.verified_at = stamp()
        if manual and review and review.parent_employer_id:
            parent = s.get(Employer, review.parent_employer_id)
            if not parent or parent.id == employer.id or not review.relationship_proof_url:
                raise ValueError("集团关联需要有效母公司和关系证据地址")
            public_url(review.relationship_proof_url)
            ancestor = parent
            visited = {employer.id}
            while ancestor:
                if ancestor.id in visited:
                    raise ValueError("集团关联不能形成循环")
                visited.add(ancestor.id)
                ancestor = s.get(Employer, ancestor.parent_id) if ancestor.parent_id else None
            employer.parent_id = parent.id
        if employer.org_type != "soe" or not employer.proof_url:
            reasons.append("企业国有性质缺少核验依据")
        if (
            official
            and source
            and source.employer
            and employer.name != source.employer
            and not (manual and proof_url)
        ):
            source_employer = s.scalar(select(Employer).where(Employer.name == source.employer))
            if not source_employer or employer.parent_id != source_employer.id:
                reasons.append("招聘主体与来源所属集团的关系未核验")
        if manual:
            # Manual confirmation may resolve classification/scope, never fabricate evidence.
            reasons = [r for r in reasons if r not in ("官方公告来源尚未核验",)]
            if not proof_url and not official:
                reasons.append("人工确认需要企业性质证据地址")
        identity_data = [
            employer.id,
            result.cohorts,
            result.season,
            _normal_title(result.title or doc.title),
            doc.published_at.year if doc.published_at else None,
        ]
        identity = hashlib.sha256(json.dumps(identity_data, ensure_ascii=False).encode()).hexdigest()
        activity = s.scalar(select(Activity).where(Activity.identity == identity))
        target = None
        anchor = None
        if result.action == "recruitment":
            candidates = list(s.scalars(select(Opportunity).where(Opportunity.merged_into_id.is_(None))))
            if record_id:
                candidates.sort(key=lambda x: x.id != record_id)
            elif version.opportunity_id:
                candidates.sort(key=lambda x: x.id != version.opportunity_id)
            for previous in candidates:
                if canonical(previous.official_url) != url and previous.id != record_id:
                    continue
                if previous.cohorts and result.cohorts and set(previous.cohorts) != set(result.cohorts):
                    continue
                if (
                    not (previous.cohorts and result.cohorts)
                    and previous.published_at
                    and doc.published_at
                    and previous.published_at[:4] != str(doc.published_at.year)
                ):
                    continue
                if previous.id != record_id and (
                    s.get(Batch, previous.batch_id).name != result.batch
                    or previous.scope != result.deadline.scope
                    or previous.season
                    and result.season
                    and {"春季": "春招", "秋季": "秋招"}.get(previous.season, previous.season)
                    != result.season
                ):
                    continue
                anchor = previous
                break
        if result.action != "recruitment":
            if target_id:
                target = s.get(Opportunity, target_id)
                if target and (
                    target.employer_id != employer.id
                    or target.cohorts != result.cohorts
                    or target.scope != result.deadline.scope
                ):
                    raise ValueError("原招聘记录的主体、届别或适用范围不匹配")
            else:
                target = _event_target(s, result, employer)
            if not target:
                reasons.append("变更公告尚未唯一关联原招聘活动")
        if target or anchor:
            existing = target or anchor
            activity = s.get(Activity, existing.activity_id)
            batch = s.get(Batch, existing.batch_id)
        else:
            if not activity:
                activity = Activity(
                    employer_id=employer.id,
                    identity=identity,
                    title=result.title or doc.title,
                    cohorts=result.cohorts,
                    season=result.season,
                )
                s.add(activity)
                s.flush()
            batch = s.scalar(
                select(Batch).where(Batch.activity_id == activity.id, Batch.name == result.batch)
            )
            if not batch:
                batch = Batch(activity_id=activity.id, name=result.batch)
                s.add(batch)
                s.flush()
        opportunity = (
            target
            or anchor
            or s.scalar(
                select(Opportunity).where(
                    Opportunity.batch_id == batch.id, Opportunity.scope == result.deadline.scope
                )
            )
        )
        if not opportunity:
            opportunity = Opportunity(
                activity_id=activity.id,
                batch_id=batch.id,
                employer_id=employer.id,
                title=result.title or doc.title,
                source_id=doc.source_id,
                official_url=url,
                scope=result.deadline.scope,
            )
            s.add(opportunity)
            s.flush()
        if manual and record_id == opportunity.id:
            if s.scalar(
                select(Opportunity).where(
                    Opportunity.batch_id == batch.id,
                    Opportunity.scope == result.deadline.scope,
                    Opportunity.id != opportunity.id,
                )
            ):
                raise ValueError("同批次的目标适用对象已经存在，请选择已有记录")
            opportunity.scope = result.deadline.scope
        if not manual and opportunity.review_status in ("manual", "rejected"):
            previous_versions = list(
                s.scalars(
                    select(Document.id).where(
                        Document.opportunity_id == opportunity.id, Document.id != version.id
                    )
                )
            )
            version.opportunity_id = opportunity.id
            version.parse_state = "rejected" if opportunity.review_status == "rejected" else "manual_change"
            if opportunity.review_status == "manual" and previous_versions:
                opportunity.needs_review = True
                opportunity.review_reasons = ["原文已更新，保留原人工结论并等待重新核验"]
            return serialize(s, opportunity, cfg)
        old_claim = (
            s.get(Deadline, opportunity.latest_deadline_id) if opportunity.latest_deadline_id else None
        )
        # Conflicting independent sources must not silently overwrite a verified claim.
        if (
            old_claim
            and opportunity.official_url != url
            and result.action == "recruitment"
            and old_claim.value != result.deadline.value
        ):
            reasons.append("不同公告提供了不同截止，需人工确认其关系")
        reasons = list(dict.fromkeys(reasons))
        if manual and reasons:
            raise ValueError("；".join(reasons))
        opportunity.review_status = (
            "excluded" if excluded else "pending" if reasons else ("manual" if manual else "auto")
        )
        opportunity.needs_review = False
        opportunity.employer_id = employer.id
        if result.action == "recruitment":
            opportunity.title = result.title or doc.title
            opportunity.official_url = url
        opportunity.review_reasons = reasons
        opportunity.cohorts = result.cohorts
        opportunity.season = result.season
        opportunity.recruitment_type = result.recruitment_type
        opportunity.locations = result.locations
        opportunity.education = result.education
        opportunity.majors = result.majors
        opportunity.positions = [x.model_dump() for x in result.positions]
        if result.positions:
            s.execute(delete(JobPosting).where(JobPosting.opportunity_id == opportunity.id))
            for position in result.positions:
                s.add(JobPosting(opportunity_id=opportunity.id, **position.model_dump()))
        opportunity.apply_url = (
            result.apply_url if result.apply_url.startswith(("http://", "https://")) else ""
        )
        opportunity.extraction = result.model_dump(mode="json")
        opportunity.published_at = str(validation_doc.published_at) if validation_doc.published_at else None
        opportunity.last_checked = stamp()
        opportunity.updated_at = stamp()
        verification = "manual" if manual else "auto"
        claim = Deadline(
            opportunity_id=opportunity.id,
            document_id=version.id,
            **result.deadline.model_dump(mode="json"),
            verified_by=verification,
        )
        s.add(claim)
        s.flush()
        if (not reasons or not old_claim or target is None) and not (result.action == "close" and old_claim):
            opportunity.latest_deadline_id = claim.id
        if not reasons:
            opportunity.lifecycle = (
                "closed"
                if result.action == "close"
                else (
                    "open"
                    if result.deadline.value or result.deadline.kind in ("rolling", "long_term")
                    else "unknown"
                )
            )
        changed = old_claim and (old_claim.value != claim.value or old_claim.kind != claim.kind)
        event_kind = (
            result.action
            if result.action != "recruitment"
            else "deadline_changed"
            if changed
            else "document_updated"
            if old_claim
            else "new"
        )
        if changed or not s.scalar(
            select(ChangeEvent).where(
                ChangeEvent.opportunity_id == opportunity.id,
                ChangeEvent.document_id == version.id,
                ChangeEvent.kind == event_kind,
            )
        ):
            s.add(
                ChangeEvent(
                    opportunity_id=opportunity.id,
                    document_id=version.id,
                    kind=event_kind,
                    old_value=old_claim.value if old_claim else None,
                    new_value=claim.value,
                )
            )
            if old_claim:
                opportunity.sequence += 1
        version.opportunity_id = opportunity.id
        version.parse_state = "excluded" if excluded else "done" if not reasons else "review"
        if manual:
            s.add(
                ReviewRecord(
                    opportunity_id=opportunity.id,
                    document_id=version.id,
                    decision="approve",
                    payload=review.model_dump(mode="json")
                    if review
                    else {"extraction": result.model_dump(mode="json")},
                )
            )
            if review and review.use_snapshot:
                opportunity.last_checked = version.fetched_at
        s.flush()
        # Independent scoped deadlines become linked opportunities, not copies across cohorts.
        if result.scoped_deadlines and not reasons:
            for scoped in result.scoped_deadlines:
                child = s.scalar(
                    select(Opportunity).where(
                        Opportunity.batch_id == batch.id, Opportunity.scope == scoped.scope
                    )
                )
                if not child:
                    child = Opportunity(
                        activity_id=activity.id,
                        batch_id=batch.id,
                        employer_id=employer.id,
                        title=f"{opportunity.title} · {scoped.scope}",
                        source_id=doc.source_id,
                        official_url=url,
                        scope=scoped.scope,
                        cohorts=result.cohorts,
                        season=result.season,
                        recruitment_type=result.recruitment_type,
                        review_status=verification,
                        lifecycle="open",
                        locations=result.locations,
                        education=result.education,
                        majors=result.majors,
                        extraction=result.model_dump(mode="json"),
                    )
                    s.add(child)
                    s.flush()
                previous = s.get(Deadline, child.latest_deadline_id) if child.latest_deadline_id else None
                child_changed = previous and (previous.value != scoped.value or previous.kind != scoped.kind)
                child.review_status = verification
                child.extraction = result.model_dump(mode="json")
                child.official_url = url
                child.last_checked = stamp()
                child.updated_at = stamp()
                child.lifecycle = "open"
                scoped_claim = Deadline(
                    opportunity_id=child.id,
                    document_id=version.id,
                    **scoped.model_dump(mode="json"),
                    verified_by=verification,
                )
                s.add(scoped_claim)
                s.flush()
                child.latest_deadline_id = scoped_claim.id
                if not previous or child_changed:
                    s.add(
                        ChangeEvent(
                            opportunity_id=child.id,
                            document_id=version.id,
                            kind="deadline_changed" if child_changed else "new",
                            old_value=previous.value if previous else None,
                            new_value=scoped.value,
                        )
                    )
                    if child_changed:
                        child.sequence += 1
        return serialize(s, opportunity, cfg)


def serialize(s, opportunity: Opportunity, cfg: Settings, *, detail=False) -> dict:
    value = {column.name: getattr(opportunity, column.name) for column in Opportunity.__table__.columns}
    employer = s.get(Employer, opportunity.employer_id)
    claim = s.get(Deadline, opportunity.latest_deadline_id) if opportunity.latest_deadline_id else None
    personal = s.get(PersonalState, opportunity.id)
    batch = s.get(Batch, opportunity.batch_id)
    value.update(
        employer=employer.name,
        industry=employer.industry,
        org_type=employer.org_type,
        parent_id=employer.parent_id,
        employer_proof_url=employer.proof_url,
        batch=batch.name,
        deadline={c.name: getattr(claim, c.name) for c in Deadline.__table__.columns} if claim else None,
        personal={
            "saved": bool(personal and personal.saved),
            "applied": bool(personal and personal.applied),
            "notes": personal.notes if personal else "",
        },
        days_left=None,
        status=opportunity.lifecycle,
        data_stale=False,
    )
    now = local_now()
    if claim and claim.value:
        if claim.kind == "datetime":
            dt = datetime.fromisoformat(claim.value)
            value["days_left"] = (dt.date() - now.date()).days
            expired = dt < now
        else:
            day = date.fromisoformat(claim.value)
            value["days_left"] = (day - now.date()).days
            expired = day < now.date()
        if expired:
            value["status"] = "closed"
        if claim.kind == "datetime" and re.search(r"24\s*(?:[:：]00|点|时)", claim.quote):
            dt = datetime.fromisoformat(claim.value)
            if dt.hour == 0 and dt.minute == 0:
                from datetime import timedelta

                day = dt.date() - timedelta(days=1)
                value["deadline_display"] = f"{day.isoformat()} 24:00（当日结束）"
                value["days_left"] = (day - now.date()).days
    checked = datetime.fromisoformat(opportunity.last_checked)
    value["data_stale"] = (now - checked).total_seconds() > 48 * 3600
    value["data_stale"] = value["data_stale"] or opportunity.needs_review
    if detail:
        value["reviews"] = [
            {c.name: getattr(r, c.name) for c in ReviewRecord.__table__.columns}
            for r in s.scalars(
                select(ReviewRecord)
                .where(ReviewRecord.opportunity_id == opportunity.id)
                .order_by(ReviewRecord.created_at.desc())
            )
        ]
        value["jobs"] = [
            {c.name: getattr(job, c.name) for c in JobPosting.__table__.columns}
            for job in s.scalars(select(JobPosting).where(JobPosting.opportunity_id == opportunity.id))
        ]
        docs = documents_for(s, opportunity)
        # A child scope shares its parent document via the deadline relationship.
        if claim and all(x.id != claim.document_id for x in docs):
            attached = s.get(Document, claim.document_id)
            if attached:
                docs.append(attached)
        value["documents"] = [
            {c.name: getattr(d, c.name) for c in Document.__table__.columns if c.name != "snapshot_path"}
            for d in docs
        ]
        value["events"] = [
            {c.name: getattr(e, c.name) for c in ChangeEvent.__table__.columns}
            for e in s.scalars(
                select(ChangeEvent)
                .where(ChangeEvent.opportunity_id == opportunity.id)
                .order_by(ChangeEvent.created_at.desc())
            )
        ]
    return value
