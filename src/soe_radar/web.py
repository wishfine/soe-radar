from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings, load_settings
from .db import initialize
from .exports import csv_export, ics_export
from .extraction import extract_rules
from .fetching import Fetcher
from .imports import import_csv
from .models import (
    ChangeEvent,
    CrawlRun,
    Document,
    Employer,
    ModelCall,
    Opportunity,
    PersonalState,
    ProviderState,
    ReviewRecord,
    SourceState,
    stamp,
)
from .providers import permitted
from .schemas import ReviewInput, SourceSpec, StatePatch, SyncInput
from .store import ingest, serialize
from .sync import Scheduler, queue_run
from .urls import on_domains, public_url

ROOT = Path(__file__).parent


def archived_pending(row):
    from .schemas import local_now

    year = local_now().year
    return bool(
        row["cohorts"]
        and max(row["cohorts"]) < year
        or not row["cohorts"]
        and row["published_at"]
        and row["published_at"][:4] < str(year - 1)
    )


def query_rows(db, cfg, params, *, paginate=True):
    with db.session() as s:
        rows = [
            serialize(s, record, cfg)
            for record in s.scalars(select(Opportunity).where(Opportunity.merged_into_id.is_(None)))
        ]
        hidden_old = 0
        if params.get("review") == "pending":
            rows = [r for r in rows if r["review_status"] == "pending" or r["needs_review"]]
            if params.get("include_old") != "1":
                hidden_old = sum(archived_pending(r) for r in rows)
                rows = [r for r in rows if not archived_pending(r)]
        elif params.get("review") != "all":
            rows = [
                r
                for r in rows
                if r["review_status"] in ("auto", "manual")
                and (not r["needs_review"] or params.get("history") == "1")
            ]
        q = params.get("q", "").lower().strip()
        if q:
            rows = [
                r
                for r in rows
                if q in " ".join([r["title"], r["employer"], *r["majors"], *r["locations"]]).lower()
            ]
        for field, column in [("region", "locations"), ("education", "education"), ("major", "majors")]:
            if params.get(field):
                rows = [
                    r
                    for r in rows
                    if any(params[field] in x for x in r[column])
                    or (field == "region" and "全国" in r[column])
                ]
        for field in ["industry", "batch", "source_id"]:
            if params.get(field):
                rows = [r for r in rows if params[field] in r[field]]
        if params.get("employer"):
            employers = list(s.scalars(select(Employer)))
            selected = {e.id for e in employers if params["employer"] in e.name}
            while True:
                expanded = selected | {e.id for e in employers if e.parent_id in selected}
                if expanded == selected:
                    break
                selected = expanded
            rows = [r for r in rows if r["employer_id"] in selected]
        cohorts = (
            params.getlist("cohort")
            if hasattr(params, "getlist")
            else [params["cohort"]]
            if params.get("cohort")
            else []
        )
        if cohorts:
            try:
                selected = {int(y) for value in cohorts for y in str(value).split(",")}
            except ValueError:
                raise HTTPException(422, "届别必须为年份")
            rows = [r for r in rows if set(r["cohorts"]) & selected]
        if params.get("saved") in ("1", "true"):
            rows = [r for r in rows if r["personal"]["saved"]]
        if params.get("applied") in ("1", "true", "0", "false"):
            rows = [r for r in rows if r["personal"]["applied"] == (params["applied"] in ("1", "true"))]
        status = params.get("status", "")
        if status == "open":
            rows = [r for r in rows if r["status"] == "open"]
        elif status == "closed":
            rows = [r for r in rows if r["status"] == "closed"]
        elif (
            params.get("review") != "pending"
            and params.get("history") != "1"
            and params.get("saved") not in ("1", "true")
        ):
            rows = [r for r in rows if r["status"] == "open"]
        if params.get("days"):
            try:
                days = int(params["days"])
                if not 0 <= days <= 366:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "截止天数须为 0–366")
            rows = [r for r in rows if r["days_left"] is not None and 0 <= r["days_left"] <= days]
        if params.get("changed") == "1":
            recent = (datetime.fromisoformat(stamp()) - timedelta(days=7)).isoformat()
            changed = set(
                s.scalars(
                    select(ChangeEvent.opportunity_id).where(
                        ChangeEvent.kind.in_(["extend", "reopen", "deadline_changed"]),
                        ChangeEvent.created_at >= recent,
                    )
                )
            )
            rows = [r for r in rows if r["id"] in changed]
        if params.get("new") == "1":
            from .schemas import local_now

            cutoff = (local_now() - timedelta(days=7)).date().isoformat()
            today = local_now().date().isoformat()
            rows = [r for r in rows if r["published_at"] and cutoff <= r["published_at"] <= today]
        rows.sort(
            key=lambda r: (
                r["days_left"] is None,
                r["days_left"] if r["days_left"] is not None else 99999,
                -datetime.fromisoformat(r["first_seen"]).timestamp(),
            )
        )
        if params.get("review") == "pending":
            from .schemas import local_now

            target = local_now().year + 1
            rows.sort(
                key=lambda r: (
                    0 if target in r["cohorts"] else 1 if not r["cohorts"] else 2,
                    -int((r["published_at"] or r["first_seen"][:10]).replace("-", "")),
                    -datetime.fromisoformat(r["first_seen"]).timestamp(),
                )
            )
        elif params.get("history") == "1":
            rows.sort(
                key=lambda r: (r["deadline"] or {}).get("value") or r["published_at"] or r["first_seen"],
                reverse=True,
            )
        total = len(rows)
        try:
            page, size = (
                max(1, int(params.get("page", 1))),
                min(100, max(1, int(params.get("page_size", 30)))),
            )
        except ValueError:
            raise HTTPException(422, "分页参数必须为整数")
        return {
            "items": rows[(page - 1) * size : page * size] if paginate else rows,
            "total": total,
            "page": page,
            "page_size": size,
            "hidden_old": hidden_old,
        }


def create_app(cfg: Settings | None = None, *, scheduling=True):
    cfg = cfg or load_settings()
    db = initialize(cfg)
    token_path = cfg.data_dir / "web-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
    token = token_path.read_text().strip()

    @asynccontextmanager
    async def lifespan(app):
        scheduler = Scheduler(db, cfg)
        scheduler.recover()
        task = asyncio.create_task(scheduler.loop()) if scheduling else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        db.engine.dispose()

    app = FastAPI(title="SOE Radar", version="0.1.0", lifespan=lifespan)
    app.state.db, app.state.cfg, app.state.token = db, cfg, token
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    @app.middleware("http")
    async def local_mutation_guard(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            supplied = request.headers.get("x-radar-token", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "操作校验失败，请刷新本地页面"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "只允许本地同源操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        with db.session() as s:
            employers = [
                {"name": x.name, "parent_id": x.parent_id}
                for x in s.scalars(select(Employer).order_by(Employer.name))
            ]
            cohorts = sorted({y for x in s.scalars(select(Opportunity)) for y in x.cohorts}, reverse=True)
        return templates.TemplateResponse(
            request, "index.html", {"token": token, "employers": employers, "cohorts": cohorts}
        )

    @app.get("/api/opportunities")
    async def listing(request: Request):
        return query_rows(db, cfg, request.query_params)

    @app.get("/api/opportunities/{opportunity_id}")
    async def detail(opportunity_id: str):
        with db.session() as s:
            row = s.get(Opportunity, opportunity_id)
            if not row:
                raise HTTPException(404, "未找到招聘记录")
            return serialize(s, row, cfg, detail=True)

    @app.patch("/api/opportunities/{opportunity_id}/state")
    async def personal(opportunity_id: str, patch: StatePatch):
        with db.session() as s:
            if not s.get(Opportunity, opportunity_id):
                raise HTTPException(404, "未找到招聘记录")
            state = s.get(PersonalState, opportunity_id) or PersonalState(opportunity_id=opportunity_id)
            for key, value in patch.model_dump(exclude_none=True).items():
                setattr(state, key, value)
            s.add(state)
        return {"ok": True}

    @app.post("/api/sync", status_code=202)
    async def sync(body: SyncInput):
        known = {x.id for x in cfg.sources}
        if body.source_ids is not None and (not body.source_ids or set(body.source_ids) - known):
            raise HTTPException(422, "来源列表为空或包含未知来源")
        return {"id": queue_run(db, body.source_ids, body.discover), "status": "queued"}

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        with db.session() as s:
            run = s.get(CrawlRun, run_id)
            if not run:
                raise HTTPException(404, "未找到同步任务")
            return {c.name: getattr(run, c.name) for c in CrawlRun.__table__.columns}

    @app.get("/api/runs")
    async def runs():
        with db.session() as s:
            return [
                {c.name: getattr(run, c.name) for c in CrawlRun.__table__.columns}
                for run in s.scalars(select(CrawlRun).order_by(CrawlRun.created_at.desc()).limit(20))
            ]

    @app.get("/api/health")
    async def health():
        with db.session() as s:
            states = {x.id: x for x in s.scalars(select(SourceState))}
            sources = []
            for source in cfg.sources:
                state = states.get(source.id)
                campus = list(
                    s.scalars(
                        select(Opportunity).where(
                            Opportunity.source_id == source.id,
                            Opportunity.recruitment_type == "campus",
                            Opportunity.review_status.in_(["auto", "manual", "pending"]),
                            Opportunity.merged_into_id.is_(None),
                        )
                    )
                )
                sources.append(
                    {
                        "id": source.id,
                        "name": source.name,
                        "url": source.url,
                        "enabled": source.enabled,
                        "status": "no_recruitment"
                        if state and state.status in ("success", "partial") and not campus
                        else state.status
                        if state
                        else "unverified",
                        "last_success": state.last_success if state else None,
                        "last_error": state.last_error if state else "",
                        "last_count": state.last_count if state else 0,
                        "failures": state.failures if state else 0,
                        "cursor": state.cursor if state else {},
                        "campus_count": len(campus),
                        "verified_count": sum(
                            x.review_status in ("auto", "manual") and not x.needs_review for x in campus
                        ),
                        "usable": bool(
                            source.enabled and state and state.status in ("success", "partial") and campus
                        ),
                    }
                )
            paused = {
                x.name: {"reason": x.reason, "until": x.paused_until}
                for x in s.scalars(select(ProviderState))
                if x.paused_until and x.paused_until > stamp()
            }
            providers = [
                {
                    "name": p.name,
                    "protocol": p.protocol,
                    "enabled": p.enabled,
                    "models": p.models,
                    "configured": not p.key_required or bool(__import__("os").environ.get(p.api_key_env)),
                    "allowed_models": [m for m in p.models if permitted(p, m, cfg)],
                    "pause": paused.get(p.name),
                }
                for p in cfg.llm.providers
            ]
            pending = s.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.parse_state.in_(["review", "pending_model"]))
            )
            calls = s.scalar(select(func.count()).select_from(ModelCall))
            cost = s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0)))
        return {
            "sources": sources,
            "providers": providers,
            "pending_documents": pending,
            "model_calls": calls,
            "reserved_cost_usd": cost,
            "allow_paid": cfg.llm.allow_paid,
            "schedule_hours": cfg.schedule_hours,
            "timezone": cfg.timezone,
        }

    @app.get("/api/stats")
    async def stats():
        all_rows = query_rows(db, cfg, {"review": "all", "history": "1"}, paginate=False)["items"]
        verified = [
            r
            for r in all_rows
            if r["review_status"] in ("auto", "manual") and not r["needs_review"] and r["status"] == "open"
        ]
        return {
            "active": len(verified),
            "due_soon": sum(r["days_left"] is not None and 0 <= r["days_left"] <= 7 for r in verified),
            "pending": sum(
                (r["review_status"] == "pending" or r["needs_review"]) and not archived_pending(r)
                for r in all_rows
            ),
            "archived_pending": sum(
                (r["review_status"] == "pending" or r["needs_review"]) and archived_pending(r)
                for r in all_rows
            ),
            "saved": sum(r["personal"]["saved"] for r in all_rows),
            "cohorts": sorted({y for r in all_rows for y in r["cohorts"]}, reverse=True),
            "industries": sorted({r["industry"] for r in all_rows}),
            "last_checked": max((r["last_checked"] for r in all_rows), default=None),
        }

    @app.post("/api/imports")
    async def imports(request: Request):
        form = await request.form(max_part_size=5_000_000)
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            raise HTTPException(422, "请选择 CSV 文件")
        try:
            mapping = json.loads(str(form.get("mapping") or "{}"))
            if not isinstance(mapping, dict):
                raise ValueError("字段映射必须是 JSON 对象")
            content = await upload.read(5_000_001)
            return import_csv(db, cfg, content, mapping)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/employers")
    async def employers():
        with db.session() as s:
            return [
                {"id": e.id, "name": e.name, "parent_id": e.parent_id}
                for e in s.scalars(select(Employer).order_by(Employer.name))
            ]

    @app.post("/api/reviews/{opportunity_id}")
    async def review(opportunity_id: str, body: ReviewInput):
        with db.session() as s:
            row = s.get(Opportunity, opportunity_id)
            if not row:
                raise HTTPException(404, "未找到招聘记录")
            if row.merged_into_id:
                row = s.get(Opportunity, row.merged_into_id)
            if body.decision == "reject":
                row.review_status = "rejected"
                row.needs_review = False
                s.add(
                    ReviewRecord(
                        opportunity_id=row.id, decision="reject", payload=body.model_dump(mode="json")
                    )
                )
                for doc in s.scalars(select(Document).where(Document.opportunity_id == row.id)):
                    doc.parse_state = "rejected"
                return {"id": row.id, "review_status": "rejected"}
            from .store import documents_for

            versions = documents_for(s, row)
            version = next((d for d in versions if not body.snapshot_id or d.id == body.snapshot_id), None)
            old_url, old_title = row.official_url, row.title
            if not version and body.use_snapshot:
                raise HTTPException(422, "缺少原文版本")
        url = body.official_url or old_url
        try:
            public_url(url)
            source = next((x for x in cfg.sources if on_domains(url, x.domains)), None)
            if not source:
                if not body.employer_proof_url:
                    raise ValueError("请填写企业性质证据链接")
                source = SourceSpec(id="manual", name="人工核验", url=url, domains=[urlsplit(url).hostname])
            if body.use_snapshot:
                if version.source_id == "import":
                    raise ValueError("导入表格不是官方快照，请先在线读取公告原文")
                if url != old_url:
                    raise ValueError("使用已保存快照时不能替换公告地址")
                from .maintenance import saved_input

                doc = saved_input(version, cfg)
            else:
                fetcher = Fetcher(cfg)
                try:
                    doc = await fetcher.document(url, source, old_title)
                finally:
                    await fetcher.close()
            extracted = body.extraction or extract_rules(doc)
            result = ingest(
                db,
                cfg,
                doc,
                extracted,
                manual=True,
                proof_url=body.employer_proof_url or "",
                target_id=body.target_opportunity_id,
                record_id=row.id,
                review=body,
            )
            if result["review_status"] == "pending":
                raise ValueError("；".join(result["review_reasons"]))
            if result["id"] != opportunity_id:
                with db.session() as s:
                    original = s.get(Opportunity, opportunity_id)
                    original.review_status = "resolved"
                    original.review_reasons = ["已关联核验记录：" + result["id"]]
            return result
        except Exception as exc:
            # Avoid forwarding network response bodies or credentials to the browser.
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else f"原文读取失败：{type(exc).__name__}。可勾选已保存版本进行快照核验。"
            )
            raise HTTPException(422, message)

    @app.get("/exports/deadlines.ics")
    async def calendar(request: Request):
        rows = query_rows(db, cfg, request.query_params, paginate=False)["items"]
        return Response(
            ics_export(rows),
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="soe-radar.ics"'},
        )

    @app.get("/exports/opportunities.csv")
    async def export_csv(request: Request):
        rows = query_rows(db, cfg, request.query_params, paginate=False)["items"]
        return Response(
            csv_export(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="soe-radar.csv"'},
        )

    return app
