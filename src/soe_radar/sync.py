from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from .config import Settings
from .db import Database
from .extraction import PARSER_VERSION, extract_rules, validate_extraction
from .fetching import Fetcher, FetchFailure
from .models import CrawlRun, Deadline, DiscoveryCall, Document, Lease, Opportunity, SourceState, stamp
from .providers import Extractor, ModelUnavailable
from .schemas import DocumentInput, SourceSpec, local_now
from .store import digest_document, ingest, serialize
from .urls import canonical, on_domains


class Pipeline:
    def __init__(self, cfg: Settings, db: Database, *, fetcher=None, extractor=None):
        self.cfg, self.db = cfg, db
        self.fetcher = fetcher or Fetcher(cfg)
        self.extractor = extractor or Extractor(cfg, db)

    async def close(self):
        await self.fetcher.close()

    async def process(self, doc: DocumentInput):
        digest = digest_document(doc)
        with self.db.session() as s:
            cached = s.scalar(
                select(Document).where(Document.url == canonical(doc.url), Document.digest == digest)
            )
            if (
                cached
                and cached.parser_version == PARSER_VERSION
                and cached.parse_state in ("done", "excluded")
            ):
                if cached.opportunity_id:
                    s.get(Opportunity, cached.opportunity_id).last_checked = stamp()
                for child in s.scalars(
                    select(Opportunity)
                    .join(Deadline, Opportunity.latest_deadline_id == Deadline.id)
                    .where(Deadline.document_id == cached.id)
                ):
                    child.last_checked = stamp()
                cached.fetched_at = stamp()
                return {"cached": True, "excluded": cached.parse_state == "excluded"}
        result = extract_rules(doc)
        if result.recruitment_type in ("social", "internal", "postdoc", "intern", "notice"):
            with self.db.session() as s:
                existing = s.scalar(
                    select(Document).where(Document.url == canonical(doc.url), Document.digest == digest)
                )
                if not existing:
                    s.add(
                        Document(
                            url=canonical(doc.url),
                            digest=digest,
                            source_id=doc.source_id,
                            title=doc.title,
                            text=doc.text,
                            published_at=str(doc.published_at) if doc.published_at else None,
                            attachments=doc.attachments,
                            extraction=result.model_dump(mode="json"),
                            parser_version=PARSER_VERSION,
                            parse_state="excluded",
                        )
                    )
                else:
                    existing.parse_state = "excluded"
                    existing.parser_version = PARSER_VERSION
                prior = (
                    s.scalars(
                        select(Opportunity)
                        .join(Document, Document.opportunity_id == Opportunity.id)
                        .where(Document.url == canonical(doc.url), Opportunity.title == doc.title)
                    )
                    .unique()
                    .all()
                )
                for opportunity in prior:
                    if opportunity.review_status != "manual":
                        opportunity.review_status = "excluded"
            return {"excluded": True}
        model_pending = False
        if validate_extraction(result, doc):
            try:
                enriched = await self.extractor.extract(doc)
                # Rules retain a known explicit deadline if a model omitted it.
                if not enriched.deadline.value and result.deadline.value:
                    enriched.deadline = result.deadline
                if not enriched.cohorts:
                    enriched.cohorts = result.cohorts
                if not enriched.employer:
                    enriched.employer = result.employer
                if not enriched.title:
                    enriched.title = doc.title
                result = enriched
            except ModelUnavailable as exc:
                model_pending = True
                result.review_reasons.append("模型待解析：" + str(exc))
        for attachment in doc.attachments:
            if attachment.get("status") in ("failed", "needs_ocr", "needs_review"):
                result.review_reasons.append("附件尚未完整解析：" + attachment.get("label", "附件"))
        saved = ingest(self.db, self.cfg, doc, result)
        if model_pending:
            with self.db.session() as s:
                version = s.scalar(
                    select(Document).where(Document.url == canonical(doc.url), Document.digest == digest)
                )
                version.parse_state = "pending_model"
        return saved

    def _due(self, url: str):
        from .models import Deadline, Opportunity

        with self.db.session() as s:
            latest = s.scalar(
                select(Document).where(Document.url == canonical(url)).order_by(Document.fetched_at.desc())
            )
            if not latest:
                return True
            if latest.parser_version != PARSER_VERSION:
                return True
            hours = 24
            opportunity = s.get(Opportunity, latest.opportunity_id) if latest.opportunity_id else None
            if opportunity and opportunity.latest_deadline_id:
                claim = s.get(Deadline, opportunity.latest_deadline_id)
                if claim and claim.value:
                    try:
                        remaining = (datetime.fromisoformat(claim.value).date() - local_now().date()).days
                        if 0 <= remaining <= 3:
                            hours = 6
                    except ValueError:
                        pass
            checked = opportunity.last_checked if opportunity else latest.fetched_at
            return (datetime.now(UTC) - datetime.fromisoformat(checked)).total_seconds() >= hours * 3600

    async def crawl(self, source: SourceSpec) -> dict:
        with self.db.session() as s:
            state = s.get(SourceState, source.id)
            state.last_attempt = stamp()
            cursor = dict(state.cursor or {})
        backlog = [(x[0], x[1]) for x in cursor.get("backlog", [])]
        pages = [source.url]
        if cursor.get("next_page") and cursor["next_page"] != source.url:
            pages.append(cursor["next_page"])
        visited = set()
        found = dict(backlog)
        errors = []
        next_page = None
        pagination_unknown = False
        list_valid = False
        for _ in range(self.cfg.source_pages):
            if not pages:
                break
            url = pages.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                page = await self.fetcher.page(url, source)
            except (FetchFailure, httpx.HTTPError, ValueError) as original_error:
                page = None
                if url == source.url:
                    for fallback in source.fallback_urls:
                        try:
                            page = await self.fetcher.page(fallback, source)
                            break
                        except (FetchFailure, httpx.HTTPError, ValueError):
                            continue
                if page is None:
                    reason = (
                        str(original_error)
                        if isinstance(original_error, FetchFailure)
                        else type(original_error).__name__
                    )
                    errors.append(reason + ("；备用入口也未取得有效内容" if source.fallback_urls else ""))
                    continue
            try:
                candidates = self.fetcher.candidates(page, source)
                if not candidates and page.via != "api":
                    raise FetchFailure("未发现有效公告链接，可能为动态页或来源改版")
                list_valid = list_valid or bool(candidates)
                found.update(candidates)
                pagination = [
                    (u, label)
                    for u, label in page.links
                    if re.fullmatch(r"\s*(?:下一页|下页|Next|next|>)\s*", label)
                    and on_domains(u, source.domains)
                    and u not in visited
                ]
                if pagination and not (url == source.url and cursor.get("next_page")):
                    next_page = canonical(pagination[0][0])
                    if next_page not in pages:
                        pages.append(next_page)
                else:
                    count = re.search(r"共\s*(\d+)\s*页", page.text)
                    pagination_unknown = bool(count and int(count.group(1)) > 1 and not page.pagination_known)
                    if url != source.url or len(visited) == 1:
                        next_page = None
            except (FetchFailure, httpx.HTTPError, ValueError) as exc:
                errors.append(str(exc) if isinstance(exc, FetchFailure) else type(exc).__name__)
        # Resume old backlog, but always inspect the front page for newly posted announcements.
        # Open announcements still need checks after they leave the first pages of a list.
        with self.db.session() as s:
            for opportunity in s.scalars(
                select(Opportunity).where(
                    Opportunity.source_id == source.id,
                    Opportunity.lifecycle == "open",
                    Opportunity.review_status.in_(["auto", "manual"]),
                )
            ):
                if serialize(s, opportunity, self.cfg)["status"] == "open":
                    found.setdefault(opportunity.official_url, opportunity.title)
        due = [(u, title) for u, title in found.items() if self._due(u)]
        chosen, remaining = due[: self.cfg.details_per_source], due[self.cfg.details_per_source :]
        outcomes = {"new_or_updated": 0, "pending": 0, "excluded": 0, "cached": 0}
        failures = []
        for url, title in chosen:
            try:
                doc = await self.fetcher.document(url, source, title)
                doc = self._link_metadata(doc)
                outcome = await self.process(doc)
                if outcome.get("excluded"):
                    outcomes["excluded"] += 1
                elif outcome.get("cached"):
                    outcomes["cached"] += 1
                else:
                    outcomes["new_or_updated"] += 1
                    outcomes["pending"] += int(outcome.get("review_status") == "pending")
            except Exception as exc:
                failures.append([url, title])
                errors.append(
                    f"{title[:40]}：{str(exc) if isinstance(exc, FetchFailure) else type(exc).__name__}"
                )
        remaining = remaining + [(u, t) for u, t in failures if (u, t) not in remaining]
        valid = bool(found) and ((list_valid and not chosen) or sum(outcomes.values()) > 0)
        partial = bool(remaining or pages or next_page or pagination_unknown or errors)
        status = "partial" if valid and partial else "success" if valid else "error"
        result = dict(
            source_id=source.id,
            status=status,
            discovered=len(found),
            processed=len(chosen),
            errors=errors[:12],
            pagination_complete=not bool(pages or next_page or pagination_unknown),
            **outcomes,
        )
        with self.db.session() as s:
            state = s.get(SourceState, source.id)
            state.status = status
            state.last_count = len(found)
            state.last_error = "；".join(errors[:4])
            state.cursor = {"backlog": remaining, "next_page": pages[0] if pages else next_page}
            if valid:
                state.last_success = stamp()
                state.failures = 0
                if state.peak_count and len(found) < state.peak_count * 0.25:
                    state.last_error = "公告数量显著下降，请核验来源；" + state.last_error
                state.peak_count = max(state.peak_count, len(found))
            else:
                state.failures += 1
        return result

    @staticmethod
    def _link_metadata(doc):
        # Extraction sees linked official references in the fetched text when present.
        return doc

    async def retry_pending(self, limit=10):
        with self.db.session() as s:
            versions = list(
                s.scalars(
                    select(Document)
                    .where(Document.parse_state == "pending_model", Document.parser_version == PARSER_VERSION)
                    .order_by(Document.fetched_at)
                    .limit(limit)
                )
            )
            documents = [
                DocumentInput.model_validate_json((self.cfg.data_dir / d.snapshot_path).read_text())
                if d.snapshot_path and (self.cfg.data_dir / d.snapshot_path).is_file()
                else DocumentInput(
                    url=d.url,
                    title=d.title,
                    text=d.text,
                    source_id=d.source_id,
                    published_at=datetime.fromisoformat(d.published_at).date() if d.published_at else None,
                    attachments=d.attachments,
                )
                for d in versions
            ]
        for doc in documents:
            source = next((x for x in self.cfg.sources if x.id == doc.source_id), None)
            if source and not doc.employer_hint:
                doc.employer_hint = source.employer
            await self.process(doc)

    async def discover(self):
        if not self.cfg.tavily_enabled or not os.environ.get("TAVILY_API_KEY"):
            return {"status": "disabled", "reason": "未启用搜索或未配置 TAVILY_API_KEY"}
        midnight = local_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat()
        query = "国企 央企 校园招聘 报名截止 延期"
        with self.db.session() as s:
            s.execute(__import__("sqlalchemy").text("BEGIN IMMEDIATE"))
            used = len(list(s.scalars(select(DiscoveryCall).where(DiscoveryCall.created_at >= midnight))))
            if used >= self.cfg.tavily_daily_limit:
                return {"status": "limited", "reason": "当日搜索免费配额已用完"}
            call = DiscoveryCall(query=query)
            s.add(call)
            s.flush()
            call_id = call.id
        async with httpx.AsyncClient(timeout=self.cfg.http_timeout) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": os.environ["TAVILY_API_KEY"],
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_domains": sorted({d for source in self.cfg.sources for d in source.domains}),
                    "auto_parameters": False,
                },
            )
        if response.status_code != 200:
            return {"status": "error", "reason": f"搜索 HTTP {response.status_code}"}
        count = 0
        for hit in response.json().get("results", []):
            url = hit.get("url", "")
            source = next((x for x in self.cfg.sources if x.enabled and on_domains(url, x.domains)), None)
            if source:
                try:
                    doc = await self.fetcher.document(url, source, hit.get("title", ""))
                    await self.process(doc)
                    count += 1
                except Exception:
                    continue
        with self.db.session() as s:
            s.get(DiscoveryCall, call_id).status = "success"
        return {"status": "success", "verified_pages": count}


def queue_run(db: Database, source_ids=None, discover=False) -> str:
    with db.session() as s:
        run = CrawlRun(source_ids=source_ids, discover=discover)
        s.add(run)
        s.flush()
        return run.id


async def execute_run(db: Database, cfg: Settings, run_id: str, *, pipeline=None):
    if not db.acquire("sync", run_id):
        return {"status": "queued", "id": run_id}
    stopped = asyncio.Event()

    async def heartbeat():
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), 30)
            except TimeoutError:
                db.acquire("sync", run_id)

    renewal = asyncio.create_task(heartbeat())
    worker = pipeline or Pipeline(cfg, db)
    try:
        with db.session() as s:
            run = s.get(CrawlRun, run_id)
            run.status, run.started_at = "running", stamp()
            selected, discover = run.source_ids, run.discover
        results = []
        await worker.retry_pending()
        for source in cfg.sources:
            if source.enabled and (selected is None or source.id in selected):
                try:
                    results.append(await worker.crawl(source))
                except Exception as exc:
                    results.append(
                        {"source_id": source.id, "status": "error", "errors": [type(exc).__name__]}
                    )
                with db.session() as s:
                    s.get(CrawlRun, run_id).summary = {"sources": list(results)}
        discovery = await worker.discover() if discover else {"status": "not_requested"}
        status = "partial" if any(x["status"] != "success" for x in results) else "success"
        summary = {"sources": results, "discovery": discovery}
        with db.session() as s:
            run = s.get(CrawlRun, run_id)
            run.status, run.finished_at, run.summary = status, stamp(), summary
        with (cfg.data_dir / "observations.jsonl").open("a") as output:
            output.write(
                json.dumps(
                    {"run_id": run_id, "finished_at": stamp(), "status": status, **summary},
                    ensure_ascii=False,
                )
                + "\n"
            )
        return {"id": run_id, "status": status, **summary}
    except asyncio.CancelledError:
        with db.session() as s:
            s.get(CrawlRun, run_id).status = "queued"
        raise
    except Exception as exc:
        with db.session() as s:
            run = s.get(CrawlRun, run_id)
            run.status, run.finished_at, run.summary = "failed", stamp(), {"error": type(exc).__name__}
        return {"id": run_id, "status": "failed", "error": type(exc).__name__}
    finally:
        stopped.set()
        await renewal
        await worker.close()
        db.release("sync", run_id)


class Scheduler:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg

    def recover(self):
        with self.db.session() as s:
            lease = s.get(Lease, "sync")
            if not lease or lease.expires_at < stamp():
                for run in s.scalars(select(CrawlRun).where(CrawlRun.status == "running")):
                    run.status = "queued"

    async def loop(self):
        while True:
            self.recover()
            with self.db.session() as s:
                queued = s.scalar(
                    select(CrawlRun).where(CrawlRun.status == "queued").order_by(CrawlRun.created_at)
                )
                latest = s.scalar(
                    select(CrawlRun)
                    .where(CrawlRun.finished_at.is_not(None))
                    .order_by(CrawlRun.finished_at.desc())
                )
                due = (
                    not latest
                    or (datetime.now(UTC) - datetime.fromisoformat(latest.finished_at)).total_seconds()
                    >= self.cfg.schedule_hours * 3600
                )
                run_id = queued.id if queued else None
            if not run_id and due:
                run_id = queue_run(self.db)
            if run_id:
                await execute_run(self.db, self.cfg, run_id)
            await asyncio.sleep(20)
