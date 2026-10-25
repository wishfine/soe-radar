"""Idempotent coverage task planning and bounded execution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select

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
        source_employers = set()
        for source in cfg.sources:
            if source.employer:
                source_employers.add(source.employer)
                source_employers.update(source.aliases or [])
        employers = list(s.scalars(select(Employer)))
        for employer in employers:
            if employer.name not in memberships and not employer.parent_id:
                continue
            names = {employer.name, *(employer.aliases or [])}
            if names & source_employers:
                continue
            priority = "P1" if employer.parent_id else "P2"
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

        candidates = list(
            s.scalars(select(SourceCandidate).where(SourceCandidate.status.in_(("lead", "identity_checked"))))
        )
        for candidate in candidates:
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
        existing.evidence_url = evidence_url
        existing.quote = quote
        existing.cohorts = cohorts or []
        existing.status = status
        if status == "confirmed":
            existing.verified_at = stamp()
        s.flush()
        return existing.id


def _reset_stale(db):
    cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with db.session() as s:
        for task in s.scalars(
            select(CoverageTask).where(CoverageTask.status == "running", CoverageTask.updated_at < cutoff)
        ):
            task.status = "pending"
            task.last_error = "上次执行中断，已恢复为待办"
            task.updated_at = stamp()


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
        links = []
        for anchor in soup.select("a[href]"):
            label = anchor.get_text(" ", strip=True)
            if REGISTRY_SIGNAL.search(label):
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
    if not employer:
        return {"status": "done", "next_action": "企业记录不存在，任务关闭"}
    start_url = employer.proof_url
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
        if links:
            try:
                doc = await fetcher.document(links[0][0], source, links[0][1])
                detail = {
                    "title": doc.title,
                    "text_len": len(doc.text),
                    "published_at": str(doc.published_at or ""),
                }
                attachments = {
                    "count": len(doc.attachments),
                    "statuses": [item.get("status") for item in doc.attachments],
                }
            except Exception as exc:
                detail = {"error": str(exc)[:300]}
        status = "verified" if links and detail and not detail.get("error") else "needs_review"
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
            "next_action": "候选已验证但仍未启用；需人工确认官方身份并接入配置"
            if status == "verified"
            else "列表或详情未通过，保留候选待人工核对",
            "evidence": {"candidate_id": candidate.id, "list": len(links), "detail": detail},
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


async def run_tasks(db, cfg, *, batch=20, executor=None):
    from .models import uid

    owner = uid()
    if not db.acquire("coverage", owner, seconds=900):
        raise ValueError("已有覆盖任务持有租约")
    _reset_stale(db)
    now = stamp()
    with db.session() as s:
        tasks = list(
            s.scalars(
                select(CoverageTask)
                .where(CoverageTask.status == "pending", CoverageTask.due_at <= now)
                .order_by(CoverageTask.priority, CoverageTask.created_at)
                .limit(batch)
            )
        )
    results = []
    try:
        for task in tasks:
            with db.session() as s:
                row = s.get(CoverageTask, task.id)
                row.status = "running"
                row.updated_at = stamp()
            try:
                result = await (executor or execute_task)(db, cfg, task)
                status = result.get("status", "done")
                error = result.get("error", "")
            except Exception as exc:
                result = {"error": str(exc)[:300], "next_action": "异常中断，退避后重试"}
                status, error = "pending", str(exc)[:300]
            with db.session() as s:
                row = s.get(CoverageTask, task.id)
                row.status = status
                row.attempts += 1
                row.last_error = error
                row.last_run_at = stamp()
                row.updated_at = stamp()
                if result.get("evidence"):
                    row.evidence = {**(row.evidence or {}), **result["evidence"]}
                if result.get("next_action"):
                    row.next_action = result["next_action"]
                if status in ("pending", "blocked"):
                    delay = min(2 ** max(row.attempts, 1), 24)
                    row.due_at = (datetime.now(UTC) + timedelta(hours=delay)).isoformat()
                    if row.attempts >= 5:
                        row.status = "blocked"
            results.append({"id": task.id, "action": task.action, "status": status, "error": error})
    finally:
        db.release("coverage", owner)
    return {"processed": len(results), "results": results}


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
