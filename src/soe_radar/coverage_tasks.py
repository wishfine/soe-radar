"""Idempotent coverage task planning and bounded execution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select, update

from .config import Settings
from .fetching import Fetcher
from .models import (
    CoverageTask,
    Employer,
    RegistrySnapshot,
    RegistryState,
    SourceCandidate,
    SourceEmployerScope,
    stamp,
)
from .schemas import SourceSpec, local_now
from .urls import public_url

ENTRY_SIGNAL = re.compile(r"招聘|校招|校园|人才|加入我们|毕业生|补录|报名")
REGISTRY_SIGNAL = re.compile(r"监管企业|企业名录|企业名单|所出资企业|出资企业|国企名录|企业目录")


def _key(*parts):
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def _priority_for_registry(spec):
    if spec.category.startswith("central"):
        return "P0"
    if spec.category in ("local", "corps"):
        return "P1"
    return "P2"


def _entry_period():
    return f"cohort:{local_now().year + 1}"


def _add_task(
    session,
    *,
    scope_type,
    scope_id,
    action,
    priority,
    period_key="",
    next_action="",
    evidence=None,
):
    key = _key(scope_type, scope_id, action, period_key)
    existing = session.scalar(select(CoverageTask).where(CoverageTask.task_key == key))
    if existing:
        if existing.status == "done":
            return False
        existing.priority = priority
        existing.next_action = next_action or existing.next_action
        if evidence:
            existing.evidence = {**(existing.evidence or {}), **evidence}
        return False
    session.add(
        CoverageTask(
            task_key=key,
            scope_type=scope_type,
            scope_id=scope_id,
            action=action,
            priority=priority,
            next_action=next_action,
            evidence=evidence or {},
            period_key=period_key,
            due_at=stamp(),
        )
    )
    return True


def _memberships(session):
    states = {row.id: row for row in session.scalars(select(RegistryState))}
    snapshots = {row.id: row for row in session.scalars(select(RegistrySnapshot))}
    names = {}
    for registry_id, state in states.items():
        snapshot = snapshots.get(state.active_snapshot_id)
        if not snapshot:
            continue
        for entry in snapshot.entries:
            names.setdefault(entry["name"], []).append(registry_id)
    return names


def plan_tasks(db, cfg: Settings, *, priorities=None, limit=None):
    """Create idempotent tasks from current registry, entry and candidate gaps."""
    priorities = set(priorities or [])
    created = 0
    existing = 0
    with db.session() as s:
        for spec in cfg.registries:
            if limit is not None and created >= limit:
                break
            priority = _priority_for_registry(spec)
            if priorities and priority not in priorities:
                continue
            state = s.get(RegistryState, spec.id)
            if spec.url:
                stale = not state or state.status in ("error", "partial", "unverified", "bundled")
                if stale:
                    if _add_task(
                        s,
                        scope_type="registry",
                        scope_id=spec.id,
                        action="refresh_registry",
                        priority=priority,
                        period_key="evergreen",
                        next_action="读取官方名录全部分页并保存证据",
                    ):
                        created += 1
                    else:
                        existing += 1
            else:
                if _add_task(
                    s,
                    scope_type="registry",
                    scope_id=spec.id,
                    action="discover_registry",
                    priority=priority,
                    period_key="evergreen",
                    next_action="从主管部门官网导航/站内搜索定位名录入口",
                    evidence={"authority_url": spec.authority_url, "scope": spec.scope},
                ):
                    created += 1
                else:
                    existing += 1

        memberships = _memberships(s)
        scoped_employers = set()
        source_ids = {source.id for source in cfg.sources if source.enabled}
        for scope in s.scalars(select(SourceEmployerScope).where(SourceEmployerScope.status == "confirmed")):
            if scope.source_id not in source_ids:
                continue
            if scope.mode not in ("group_umbrella", "independent"):
                continue
            if scope.cohorts and int(_entry_period().split(":")[1]) not in scope.cohorts:
                continue
            scoped_employers.add(scope.employer_id)
        official_sites = {}
        for snapshot in s.scalars(select(RegistrySnapshot)):
            for entry in snapshot.entries:
                site = entry.get("official_url") or entry.get("profile_url") or ""
                if site:
                    official_sites.setdefault(entry["name"], site)
        source_employers = set()
        for source in cfg.sources:
            if source.employer:
                source_employers.add(source.employer)
                source_employers.update(source.aliases or [])
        employers = list(s.scalars(select(Employer)))
        for employer in employers:
            if employer.name not in memberships and not employer.parent_id:
                continue
            if employer.id in scoped_employers:
                continue
            names = {employer.name, *(employer.aliases or [])}
            if names & source_employers:
                continue
            priority = "P1" if employer.parent_id or employer.name in official_sites else "P2"
            if priorities and priority not in priorities:
                continue
            if limit is not None and created >= limit:
                break
            if _add_task(
                s,
                scope_type="employer",
                scope_id=employer.id,
                action="discover_entry",
                priority=priority,
                period_key=_entry_period(),
                next_action="从企业官网导航或官方招聘栏目寻找入口",
                evidence={"employer": employer.name, "proof_url": employer.proof_url},
            ):
                created += 1
            else:
                existing += 1

        if limit is not None and created >= limit:
            return {"created": created, "existing": existing}
        candidates = list(
            s.scalars(
                select(SourceCandidate).where(
                    SourceCandidate.status.in_(("lead", "identity_checked", "readable"))
                )
            )
        )
        for candidate in candidates:
            if priorities and "P1" not in priorities:
                continue
            if limit is not None and created >= limit:
                break
            if _add_task(
                s,
                scope_type="source_candidate",
                scope_id=candidate.id,
                action="verify_candidate",
                priority="P1",
                period_key="candidate",
                next_action="验证列表、详情、报名入口和附件",
                evidence={"url": candidate.url, "employer": candidate.employer_name},
            ):
                created += 1
            else:
                existing += 1
    return {"created": created, "existing": existing}


def add_source_candidate(
    db,
    *,
    employer_id,
    employer_name,
    url,
    domain="",
    entry_type="unknown",
    discovery_method="",
    identity_evidence="",
    scope_employer_ids=None,
    status="lead",
    evidence_path="",
):
    url = public_url(url)
    domain = domain or (urlsplit(url).hostname or "")
    key = _key(employer_id or employer_name, url)
    with db.session() as s:
        existing = s.scalar(select(SourceCandidate).where(SourceCandidate.candidate_key == key))
        if existing:
            return existing.id
        candidate = SourceCandidate(
            candidate_key=key,
            employer_id=employer_id,
            employer_name=employer_name,
            url=url,
            domain=domain,
            entry_type=entry_type,
            discovery_method=discovery_method,
            identity_evidence=identity_evidence,
            scope_employer_ids=scope_employer_ids or [],
            status=status,
            evidence_path=evidence_path,
        )
        s.add(candidate)
        s.flush()
        return candidate.id


def scope_evidence(scope):
    records = list(scope.provenance or [])
    if not records and scope.evidence_url:
        records.append(
            {
                "url": scope.evidence_url,
                "quote": scope.quote,
                "cohorts": list(scope.cohorts or []),
                "batch_scope": scope.batch_scope,
                "status": scope.status,
                "decided_at": scope.verified_at or "",
                "verified_at": scope.verified_at,
            }
        )
    return records


def recompute_scope(scope):
    """Current review state is the latest timestamped decision per cohort."""
    records = scope_evidence(scope)
    records = sorted(
        enumerate(records),
        key=lambda item: (
            0 if not item[1].get("decided_at") else 1,
            item[1].get("decided_at") or "",
            item[0],
        ),
    )
    records = [record for _, record in records]
    decisions = {}
    latest_record = records[-1] if records else {}
    for record in records:
        for cohort in record.get("cohorts") or []:
            decisions[cohort] = record
    confirmed_cohorts = sorted(
        cohort for cohort, record in decisions.items() if record.get("status") == "confirmed"
    )
    confirmed_records = [record for record in records if record.get("status") == "confirmed"]
    if confirmed_cohorts:
        primary = next(
            record
            for record in reversed(records)
            if record.get("status") == "confirmed"
            and set(record.get("cohorts") or []) & set(confirmed_cohorts)
        )
        scope.status = "confirmed"
        scope.cohorts = confirmed_cohorts
    else:
        primary = latest_record
        scope.status = "needs_review"
        scope.cohorts = sorted({cohort for record in records for cohort in (record.get("cohorts") or [])})
    scope.provenance = records
    scope.quote = primary.get("quote", "")
    scope.evidence_url = primary.get("url", "")
    scope.verified_at = (
        primary.get("verified_at") if confirmed_records and scope.status == "confirmed" else None
    )


def add_source_scope(
    db,
    *,
    source_id,
    employer_name,
    mode="group_umbrella",
    evidence_url,
    quote,
    cohorts=None,
    batch_scope="",
    status="needs_review",
):
    public_url(evidence_url)
    with db.session() as s:
        employer = s.scalar(select(Employer).where(Employer.name == employer_name))
        if not employer:
            raise ValueError("企业未建档")
        existing = s.scalar(
            select(SourceEmployerScope).where(
                SourceEmployerScope.source_id == source_id,
                SourceEmployerScope.employer_id == employer.id,
                SourceEmployerScope.mode == mode,
                SourceEmployerScope.batch_scope == batch_scope,
            )
        )
        if not existing:
            existing = SourceEmployerScope(
                source_id=source_id,
                employer_id=employer.id,
                mode=mode,
                batch_scope=batch_scope,
            )
            s.add(existing)
        record = {
            "url": evidence_url,
            "quote": quote,
            "cohorts": cohorts or [],
            "batch_scope": batch_scope,
            "status": status,
            "decided_at": stamp(),
            "verified_at": stamp() if status == "confirmed" else None,
        }
        records = scope_evidence(existing)
        if record not in records:
            records.append(record)
        existing.provenance = records
        recompute_scope(existing)
        s.flush()
        return existing.id


def recover_tasks(db, *, ids=None, action=None, note=""):
    """Explicit recovery for interrupted running tasks; audited, not an age-only sweep."""
    with db.session() as s:
        query = select(CoverageTask).where(CoverageTask.status == "running")
        if ids:
            query = query.where(CoverageTask.id.in_(ids))
        if action:
            query = query.where(CoverageTask.action == action)
        rows = list(s.scalars(query))
        for task in rows:
            history = list((task.evidence or {}).get("recover_history", []))
            history.append({"at": stamp(), "note": note or "人工恢复中断任务"})
            task.evidence = {**(task.evidence or {}), "recover_history": history}
            task.status = "pending"
            task.owner = ""
            task.due_at = stamp()
            task.last_error = "人工恢复中断任务"
            task.updated_at = stamp()
        return [{"id": row.id, "action": row.action, "scope_id": row.scope_id} for row in rows]


async def _discover_registry(db, cfg, task):
    spec = next((row for row in cfg.registries if row.id == task.scope_id), None)
    if not spec or not spec.authority_url:
        return {"status": "needs_review", "next_action": "未配置主管部门入口，需人工补充"}
    fetcher = Fetcher(cfg)
    try:
        host = urlsplit(spec.authority_url).hostname or ""
        response = await fetcher.get(
            spec.authority_url,
            domains=[host] if host else None,
            network_mode=spec.network_mode,
        )
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(response.text, "html.parser")
        exact_labels = {
            "监管企业",
            "监管企业名录",
            "监管企业名单",
            "企业名录",
            "企业名单",
            "所出资企业",
            "出资企业",
            "国企名录",
            "企业目录",
            "所属企业",
        }
        links = []
        for anchor in soup.select("a[href]"):
            label = anchor.get_text(" ", strip=True)
            clean = re.sub(r"\s+", "", label)
            if clean not in exact_labels:
                continue
            links.append({"label": label, "url": urljoin(str(response.url), anchor.get("href", ""))})
        return {
            "status": "needs_review" if links else "blocked",
            "evidence": {
                "authority_url": spec.authority_url,
                "candidate_links": links[:50],
                "checked_at": stamp(),
            },
            "next_action": "人工核对候选名录入口并保存官方分页/附件证据"
            if links
            else "官网未发现名录入口，改用站内搜索或公开目录",
        }
    except Exception as exc:
        return {"status": "pending", "error": str(exc)[:300], "next_action": "网络失败，退避后重试"}
    finally:
        await fetcher.close()


async def _discover_entry(db, cfg, task):
    with db.session() as s:
        employer = s.get(Employer, task.scope_id)
        start_url = ""
        if employer:
            names = {employer.name, *(employer.aliases or [])}
            for snapshot in s.scalars(select(RegistrySnapshot)):
                for entry in snapshot.entries:
                    if entry.get("name") in names or entry.get("source_name") in names:
                        start_url = entry.get("official_url") or entry.get("profile_url") or ""
                        if start_url:
                            break
                if start_url:
                    break
            start_url = start_url or employer.proof_url
    if not employer:
        return {"status": "done", "next_action": "企业记录不存在，任务关闭"}
    registry_urls = {url for spec in cfg.registries for url in (spec.url, spec.authority_url) if url}
    if start_url in registry_urls:
        return {
            "status": "needs_review",
            "evidence": {
                "employer": employer.name,
                "checked_url": start_url,
                "reason": "仅有主管部门名录页，缺企业官网或官方档案页",
                "checked_at": stamp(),
            },
            "next_action": "人工补充企业官网或官方招聘入口后再验证",
        }
    if not start_url:
        return {"status": "needs_review", "next_action": "缺少企业官网或官方档案页，需人工补充"}
    fetcher = Fetcher(cfg)
    try:
        host = urlsplit(start_url).hostname or ""
        response = await fetcher.get(start_url, domains=[host] if host else None)
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(response.text, "html.parser")
        links = []
        for anchor in soup.select("a[href]"):
            label = anchor.get_text(" ", strip=True)
            if ENTRY_SIGNAL.search(label):
                target = urljoin(str(response.url), anchor.get("href", ""))
                if target.startswith(("http://", "https://")):
                    links.append({"label": label[:120], "url": target})
        created = []
        for link in links[:20]:
            try:
                candidate_id = add_source_candidate(
                    db,
                    employer_id=employer.id,
                    employer_name=employer.name,
                    url=link["url"],
                    discovery_method="official_site_nav",
                    identity_evidence=start_url,
                    status="lead",
                )
                created.append(candidate_id)
            except ValueError:
                continue
        return {
            "status": "needs_review" if links else "done",
            "evidence": {
                "checked_url": start_url,
                "candidate_links": links[:20],
                "created_candidates": created,
                "checked_at": stamp(),
            },
            "next_action": "验证候选入口的列表/详情/附件"
            if links
            else "官网未发现招聘入口，记录检查范围，不推断不招聘",
        }
    except Exception as exc:
        return {"status": "pending", "error": str(exc)[:300], "next_action": "网络失败，退避后重试"}
    finally:
        await fetcher.close()


async def _verify_candidate(db, cfg, task):
    with db.session() as s:
        candidate = s.get(SourceCandidate, task.scope_id)
    if not candidate:
        return {"status": "done", "next_action": "候选不存在，任务关闭"}
    source = SourceSpec(
        id="candidate:" + candidate.id,
        name=candidate.employer_name or candidate.domain,
        url=candidate.url,
        domains=[candidate.domain] if candidate.domain else [],
        policy="company",
    )
    fetcher = Fetcher(cfg)
    try:
        page = await fetcher.page(candidate.url, source)
        links = fetcher.candidates(page, source)
        detail = {}
        attachments = {}
        verification = {
            "identity": "official_site_nav" if candidate.identity_evidence else "needs_review",
            "list": "ok" if links else "missing",
            "detail": "missing",
            "application": "needs_review",
            "attachments": "not_checked",
        }
        if links:
            try:
                doc = await fetcher.document(links[0][0], source, links[0][1])
                text = doc.text or ""
                detail = {
                    "title": doc.title,
                    "text_len": len(text),
                    "published_at": str(doc.published_at or ""),
                }
                attachment_statuses = [item.get("status") for item in doc.attachments]
                attachments = {
                    "count": len(doc.attachments),
                    "statuses": attachment_statuses,
                }

                delimiters = "。；;\n！？"

                def sentence(match):
                    start_at = max(text.rfind(d, 0, match.start()) for d in delimiters) + 1
                    ends = [text.find(d, match.end()) for d in delimiters]
                    ends = [value for value in ends if value >= 0]
                    end_at = min(ends) if ends else len(text)
                    return text[start_at:end_at]

                def negative(context):
                    return bool(
                        re.search(
                            r"请勿|不要|不接受|不接收|禁止|勿将|仅供咨询|仅接受咨询|咨询邮箱|集团概况|公司介绍|关于我们|联系我们|未提供申请",
                            context,
                        )
                    )

                def future(context):
                    return bool(
                        re.search(
                            r"另行通知|暂未公布|尚未公布|待公布|请关注后续|仅为预告|预告|报名入口.{0,10}(?:暂未|未开放)|未开放报名",
                            context,
                        )
                    )

                email_pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
                url_pattern = r"https?://[^\s，。；;]+"
                action_email = None
                for candidate_match in re.finditer(email_pattern, text):
                    context = sentence(candidate_match)
                    if negative(context) or future(context):
                        continue
                    if re.search(
                        r"(?:简历|报名|申请|投递|材料)[^。；\n]{0,30}(?:发送|投递|发至|发送至|至)"
                        r"|(?:发送|投递|发至|发送至)[^。；\n]{0,20}(?:邮箱|简历)",
                        context,
                    ):
                        action_email = candidate_match
                        break
                action_link = None
                for candidate_match in re.finditer(url_pattern, text):
                    context = sentence(candidate_match)
                    if negative(context) or future(context):
                        continue
                    if re.search(
                        r"(?:通过|登录|访问|进入|点击|使用|提交|申请|报名|投递|网申)", context
                    ) and re.search(r"(?:报名|申请|投递|网申|提交)", context):
                        action_link = candidate_match
                        break
                action_system = None
                for candidate_match in re.finditer(
                    r"(?:网申系统|报名系统|招聘系统|报名链接|网申链接|报名地址|现场报名|扫码报名)",
                    text,
                ):
                    context = sentence(candidate_match)
                    if negative(context) or future(context):
                        continue
                    if re.search(r"(?:通过|登录|访问|进入|使用|前往|提交|报名)", context):
                        action_system = candidate_match
                        break
                if re.search(r"无需报名|不适用", text):
                    verification["application"] = "not_applicable"
                elif action_email or action_link or action_system:
                    verification["application"] = "found"
                    match = action_email or action_link or action_system
                    detail["application_quote"] = sentence(match).strip()
                    detail["application_target"] = match.group()
                elif re.search(
                    r"另行通知|暂未公布|尚未公布|待公布|请关注后续|仅为预告|预告|未开放报名", text
                ):
                    verification["application"] = "needs_review"
                    detail["application_reason"] = "公告为预告或报名方式尚未公布"
                elif re.search(email_pattern, text):
                    verification["application"] = "needs_review"
                    detail["application_reason"] = "邮箱未绑定明确、肯定的投递动作"
                elif re.search(url_pattern, text):
                    verification["application"] = "needs_review"
                    detail["application_reason"] = "链接未绑定报名/申请/投递动作"
                else:
                    verification["application"] = "needs_review"
                    detail["application_reason"] = "未发现可定位的报名入口/邮箱/地址"
                required_attachment_failed = bool(
                    re.search(r"附件|报名材料|详见", text)
                    and any(value in ("failed", "needs_ocr", "needs_review") for value in attachment_statuses)
                )
                verification["attachments"] = (
                    "failed"
                    if required_attachment_failed
                    else "present"
                    if attachment_statuses
                    else "not_present"
                )
                verification["required_attachment_failed"] = required_attachment_failed
                verification["detail"] = "ok"
            except Exception as exc:
                detail = {"error": str(exc)[:300]}
                verification["detail"] = "failed"
        verified = (
            verification["identity"] != "needs_review"
            and verification["list"] == "ok"
            and verification["detail"] == "ok"
            and verification["application"] in ("found", "not_applicable")
            and not verification.get("required_attachment_failed")
        )
        if verified:
            status = "verified"
        elif verification.get("required_attachment_failed"):
            status = "needs_review"
        elif links and verification["detail"] == "ok":
            status = "readable"
        else:
            status = "needs_review"
        verification["status"] = status
        detail["verification"] = verification
        folder = cfg.data_dir / "candidates"
        folder.mkdir(parents=True, exist_ok=True)
        evidence = folder / (candidate.id + ".json")
        evidence.write_text(
            json.dumps(
                {
                    "candidate_id": candidate.id,
                    "employer": candidate.employer_name,
                    "url": candidate.url,
                    "title": page.title,
                    "via": page.via,
                    "text_excerpt": page.text[:20000],
                    "links": links[:20],
                    "detail": detail,
                    "attachments": attachments,
                    "checked_at": stamp(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        with db.session() as s:
            row = s.get(SourceCandidate, candidate.id)
            row.list_result = {"links": len(links), "sample": links[:5], "via": page.via}
            row.detail_result = detail
            row.attachment_result = attachments
            row.status = status
            row.evidence_path = str(evidence.relative_to(cfg.data_dir))
            row.last_checked = stamp()
            row.updated_at = stamp()
        return {
            "status": "done",
            "next_action": (
                "候选完整验证通过但仍未启用；需人工确认官方身份并接入配置"
                if status == "verified"
                else "列表/详情可读但报名入口或必要附件未确认，保留 readable 待补证"
                if status == "readable"
                else "列表或详情未通过，保留候选待人工核对"
            ),
            "evidence": {
                "candidate_id": candidate.id,
                "list": len(links),
                "detail": detail,
                "verification": verification,
            },
        }
    except Exception as exc:
        with db.session() as s:
            row = s.get(SourceCandidate, candidate.id)
            row.status = "blocked"
            row.last_checked = stamp()
            row.updated_at = stamp()
        return {"status": "blocked", "error": str(exc)[:300], "next_action": "访问受阻，保留候选与失败原因"}
    finally:
        await fetcher.close()


def reverify_candidates(db, status="readable"):
    """Queue an explicit recheck for candidates downgraded from legacy verified."""
    with db.session() as s:
        candidates = list(s.scalars(select(SourceCandidate).where(SourceCandidate.status == status)))
        ids = [candidate.id for candidate in candidates]
        for candidate in candidates:
            candidate.status = "lead"
            candidate.updated_at = stamp()
        tasks = list(
            s.scalars(
                select(CoverageTask).where(
                    CoverageTask.action == "verify_candidate",
                    CoverageTask.scope_id.in_(ids) if ids else False,
                )
            )
        )
        for task in tasks:
            task.status = "pending"
            task.attempts = 0
            task.owner = ""
            task.due_at = stamp()
            task.last_error = ""
            task.updated_at = stamp()
            history = list((task.evidence or {}).get("requeue_history", []))
            history.append({"at": stamp(), "note": "readable 候选复查"})
            task.evidence = {**(task.evidence or {}), "requeue_history": history}
        return {"candidates": len(ids), "tasks": len(tasks)}


def reclassify_legacy_candidates(db):
    """Legacy verified rows lacked application/attachment stages; downgrade them for recheck."""
    changed = 0
    with db.session() as s:
        for candidate in s.scalars(select(SourceCandidate).where(SourceCandidate.status == "verified")):
            detail = dict(candidate.detail_result or {})
            verification = detail.get("verification") or {}
            if verification.get("status") == "verified":
                continue
            verification = {
                **verification,
                "status": "readable",
                "legacy": "旧 verified 未记录报名入口和必要附件分项，降为 readable 待复查",
            }
            detail["verification"] = verification
            candidate.detail_result = detail
            candidate.status = "readable"
            candidate.updated_at = stamp()
            changed += 1
    return changed


async def execute_task(db, cfg, task):
    if task.action == "refresh_registry":
        from .coverage import refresh_registries

        result = await refresh_registries(db, cfg, ids=[task.scope_id])
        item = result["registries"][0] if result["registries"] else {}
        if item.get("status") in ("success", "partial", "imported"):
            return {
                "status": "done" if item.get("status") == "success" else "needs_review",
                "evidence": item,
                "next_action": "" if item.get("status") == "success" else "核对失败分页并重试",
            }
        return {"status": "pending", "error": item.get("error", "名录读取失败"), "next_action": "退避后重试"}
    if task.action == "discover_registry":
        return await _discover_registry(db, cfg, task)
    if task.action == "discover_entry":
        return await _discover_entry(db, cfg, task)
    if task.action == "verify_candidate":
        return await _verify_candidate(db, cfg, task)
    return {"status": "blocked", "error": "未知任务动作", "next_action": "人工检查任务配置"}


async def run_tasks(db, cfg, *, batch=20, action=None, executor=None):
    from .models import uid

    owner = uid()
    if not db.acquire("coverage", owner, seconds=900):
        raise ValueError("已有覆盖任务持有租约")
    now = stamp()
    with db.session() as s:
        query = select(CoverageTask).where(
            CoverageTask.due_at <= now,
            (CoverageTask.status == "pending")
            | ((CoverageTask.status == "blocked") & (CoverageTask.attempts < 5)),
        )
        if action:
            query = query.where(CoverageTask.action == action)
        tasks = list(s.scalars(query.order_by(CoverageTask.priority, CoverageTask.created_at).limit(batch)))
    results = []
    try:
        for task in tasks:
            # Renew/extend before each task; if another process owns the lease, stop.
            if not db.acquire("coverage", owner, seconds=900):
                break
            with db.session() as s:
                claimed = s.execute(
                    update(CoverageTask)
                    .where(
                        CoverageTask.id == task.id,
                        CoverageTask.status.in_(("pending", "blocked")),
                        CoverageTask.attempts < 5,
                    )
                    .values(status="running", owner=owner, updated_at=stamp())
                )
                if claimed.rowcount != 1:
                    continue
            try:
                result = await (executor or execute_task)(db, cfg, task)
                status = result.get("status", "done")
                error = result.get("error", "")
            except Exception as exc:
                result = {"error": str(exc)[:300], "next_action": "异常中断，退避后重试"}
                status, error = "pending", str(exc)[:300]
            if not db.acquire("coverage", owner, seconds=900):
                # Ownership was lost; do not overwrite another runner's result.
                break
            with db.session() as s:
                values = {
                    "status": status,
                    "attempts": CoverageTask.attempts + 1,
                    "last_error": error,
                    "last_run_at": stamp(),
                    "updated_at": stamp(),
                    "owner": "",
                }
                if result.get("evidence"):
                    values["evidence"] = {**(task.evidence or {}), **result["evidence"]}
                if result.get("next_action"):
                    values["next_action"] = result["next_action"]
                if status in ("pending", "blocked"):
                    delay = min(2 ** max(task.attempts + 1, 1), 24)
                    values["due_at"] = (datetime.now(UTC) + timedelta(hours=delay)).isoformat()
                    if task.attempts + 1 >= 5:
                        values["status"] = "blocked"
                updated = s.execute(
                    update(CoverageTask)
                    .where(CoverageTask.id == task.id, CoverageTask.owner == owner)
                    .values(**values)
                )
                if updated.rowcount != 1:
                    continue
            results.append({"id": task.id, "action": task.action, "status": status, "error": error})
    finally:
        db.release("coverage", owner)
    return {"processed": len(results), "results": results}


def requeue_tasks(db, *, ids=None, action=None, note=""):
    """Explicit, audited requeue for blocked/manual tasks."""
    with db.session() as s:
        query = select(CoverageTask).where(CoverageTask.status.in_(("blocked", "needs_review")))
        if ids:
            query = query.where(CoverageTask.id.in_(ids))
        if action:
            query = query.where(CoverageTask.action == action)
        rows = list(s.scalars(query))
        for task in rows:
            history = list((task.evidence or {}).get("requeue_history", []))
            history.append({"at": stamp(), "note": note or "人工重排"})
            task.evidence = {**(task.evidence or {}), "requeue_history": history}
            task.status = "pending"
            task.attempts = 0
            task.owner = ""
            task.due_at = stamp()
            task.last_error = ""
            task.updated_at = stamp()
        return [{"id": row.id, "action": row.action, "scope_id": row.scope_id} for row in rows]


def task_rows(db, *, status=None, limit=200):
    with db.session() as s:
        query = select(CoverageTask).order_by(CoverageTask.priority, CoverageTask.created_at).limit(limit)
        if status:
            query = query.where(CoverageTask.status == status)
        return [
            {
                "id": row.id,
                "scope_type": row.scope_type,
                "scope_id": row.scope_id,
                "action": row.action,
                "priority": row.priority,
                "status": row.status,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "next_action": row.next_action,
                "due_at": row.due_at,
            }
            for row in s.scalars(query)
        ]


def candidate_rows(db, *, status=None, limit=200):
    with db.session() as s:
        query = select(SourceCandidate).order_by(SourceCandidate.created_at.desc()).limit(limit)
        if status:
            query = query.where(SourceCandidate.status == status)
        return [
            {
                "id": row.id,
                "employer": row.employer_name,
                "url": row.url,
                "entry_type": row.entry_type,
                "status": row.status,
                "last_checked": row.last_checked,
                "list_result": row.list_result,
                "detail_result": row.detail_result,
            }
            for row in s.scalars(query)
        ]
