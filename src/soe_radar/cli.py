from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import typer
from sqlalchemy import select

from .config import config_path, load_settings
from .db import initialize
from .extraction import extract_rules
from .fetching import Fetcher
from .models import Document, ProviderState, stamp
from .providers import Extractor, ModelUnavailable, permitted
from .schemas import DocumentInput
from .sync import execute_run, queue_run

app = typer.Typer(no_args_is_help=True, help="国央企校招截止时间雷达 · 本地个人版")
dataset_app = typer.Typer(help="生成真实公告待标注集、检查人工标注和解析指标")
app.add_typer(dataset_app, name="dataset")


def output(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def server_running(marker: Path) -> bool:
    if not marker.exists():
        return False
    try:
        os.kill(int(marker.read_text()), 0)
    except (ProcessLookupError, ValueError):
        return False
    return True


@app.callback()
def main(config: Path | None = typer.Option(None, "--config", help="个人 YAML 配置路径")):
    if config:
        os.environ["SOE_RADAR_CONFIG"] = str(config.resolve())


@app.command()
def init():
    """创建个人配置和数据库。已有数据和配置不会被覆盖。"""
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 仅填写个人覆盖项，未配置部分继承仓库默认值。\n{}\n")
    cfg = load_settings(path)
    db = initialize(cfg)
    db.engine.dispose()
    output(
        {
            "config": str(path),
            "database": str(cfg.data_dir / "radar.db"),
            "sources": len(cfg.sources),
            "next": "uv run soe-radar doctor；然后 uv run soe-radar serve",
            "allow_paid": cfg.llm.allow_paid,
        }
    )


@app.command()
def doctor(
    live: bool = typer.Option(False, help="实际读取来源列表，可能需要几分钟"),
    source: list[str] | None = typer.Option(None, help="只检查指定来源，可重复"),
    model: bool = typer.Option(False, help="显式发起一次配置允许的模型解析检查"),
    reset_provider: str | None = typer.Option(None, help="解除指定供应商暂停，更新凭证后使用"),
    network: bool = typer.Option(
        False, help="对指定来源分别检查 env/direct/已配置 proxy 路径，不改变系统 VPN"
    ),
):
    """检查依赖、配置和来源。默认不请求模型。"""
    cfg = load_settings()
    db = initialize(cfg)
    if reset_provider:
        with db.session() as s:
            record = s.get(ProviderState, reset_provider)
            if record:
                s.delete(record)
    result = {
        "python": __import__("sys").version.split()[0],
        "database": str(db.path),
        "allow_paid": cfg.llm.allow_paid,
        "monthly_budget_usd": cfg.llm.monthly_budget_usd,
        "codex_installed": bool(shutil.which("codex")),
        "providers": [
            {
                "name": p.name,
                "enabled": p.enabled,
                "protocol": p.protocol,
                "credential_present": not p.key_required or bool(os.environ.get(p.api_key_env)),
                "allowed_models": [m for m in p.models if permitted(p, m, cfg)],
            }
            for p in cfg.llm.providers
        ],
        "note": "免费费用声明来自个人配置与供应商文档，不会自动升级到收费模型；请勿开启供应商自动充值。",
    }
    from .network import network_context, proxy_url

    result["network"] = network_context(cfg)

    async def checks():
        fetcher = Fetcher(cfg)
        rows = []
        try:
            if network:
                for spec in cfg.sources:
                    if source and spec.id not in source:
                        continue
                    for mode in ["env", "direct"] + (["proxy"] if proxy_url(cfg) else []):
                        start = len(fetcher.network_attempts)
                        try:
                            response = await fetcher.get(
                                spec.url, spec.domains, network_mode=mode, source_id=spec.id
                            )
                            rows.append(
                                {
                                    "id": spec.id,
                                    "route": mode,
                                    "status": "http_readable",
                                    "http_status": response.status_code,
                                    "note": "仅代表 HTTP 响应，不代表校招覆盖",
                                    "attempts": fetcher.network_attempts[start:],
                                }
                            )
                        except Exception as exc:
                            from .fetching import FetchFailure

                            rows.append(
                                {
                                    "id": spec.id,
                                    "route": mode,
                                    "status": "error",
                                    "reason": str(exc)
                                    if isinstance(exc, FetchFailure)
                                    else type(exc).__name__,
                                    "attempts": fetcher.network_attempts[start:],
                                }
                            )
            elif live:
                for spec in cfg.sources:
                    if source and spec.id not in source:
                        continue
                    try:
                        page = await fetcher.page(spec.url, spec)
                        links = fetcher.candidates(page, spec)
                        rows.append(
                            {
                                "id": spec.id,
                                "status": "list_readable",
                                "candidate_links": len(links),
                                "note": "列表可读不等于所有详情已核验",
                                "attempts": [
                                    a for a in fetcher.network_attempts if a["source_id"] == spec.id
                                ][-20:],
                            }
                        )
                    except Exception as exc:
                        rows.append({"id": spec.id, "status": "error", "reason": str(exc)[:250]})
            if model:
                try:
                    sample = DocumentInput(
                        url="https://example.org/public-test",
                        title="公开模型连通性样例",
                        text="这是软件连通性检查的合成样例，不是真实招聘。请返回 recruitment_type unknown，其他未知留空。",
                    )
                    await Extractor(cfg, db).extract(sample)
                    result["model_check"] = "success (synthetic sample only)"
                except ModelUnavailable as exc:
                    result["model_check"] = str(exc)
        finally:
            await fetcher.close()
        return rows

    result["sources"] = (
        asyncio.run(checks())
        if live or model or network
        else [{"id": s.id, "url": s.url, "enabled": s.enabled, "status": "not_probed"} for s in cfg.sources]
    )
    output(result)
    db.engine.dispose()


@app.command("employer-merge-aliases")
def employer_merge_aliases(
    apply: bool = typer.Option(False, "--apply", help="实际合并；默认只预览"),
):
    """合并括号别名/全半角括号造成的重复企业身份，保留活动、关系和审核记录。"""
    from .identity import merge_alias_employers

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output({"plans": merge_alias_employers(db, apply=apply), "applied": apply})
    finally:
        db.engine.dispose()


@app.command("relationship-add")
def relationship_add(
    parent: str = typer.Option(..., help="母公司全称，必须已建档"),
    child: str = typer.Option(..., help="子公司全称，必须已建档"),
    evidence_url: str = typer.Option(..., help="官方关系证据URL"),
    quote: str = typer.Option(..., help="官方原文中说明隶属关系的片段"),
    evidence_date: str = typer.Option(..., help="证据发布日期 YYYY-MM-DD"),
    relation_type: str = typer.Option("subsidiary", help="关系类型"),
    status: str = typer.Option("confirmed", help="confirmed 或 needs_review"),
):
    """录入有原文证据的母子公司关系；缺证据时拒绝写入。"""
    from .relationships import add_relationship

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output(
            add_relationship(
                db,
                parent,
                child,
                relation_type=relation_type,
                evidence_url=evidence_url,
                quote=quote,
                evidence_date=evidence_date,
                status=status,
            )
        )
    finally:
        db.engine.dispose()


@app.command("delivery-export")
def delivery_export(
    out: Path = typer.Option(Path("delivery"), help="交付目录"),
):
    """生成可复核的底册清单、企业、关系、来源、缺口和证据哈希。"""
    from .delivery import export_delivery

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output(export_delivery(db, cfg, out))
    finally:
        db.engine.dispose()


@app.command("coverage")
def coverage(cohort: int | None = None):
    """列出已知企业、未接入名录、网络受阻与当届活动覆盖。全国分母未知时不造覆盖率。"""
    from .coverage import coverage_report

    cfg = load_settings()
    db = initialize(cfg)
    output(coverage_report(db, cfg, cohort=cohort))
    db.engine.dispose()


@app.command("coverage-plan")
def coverage_plan(
    priority: list[str] | None = typer.Option(None, "--priority", help="只规划指定优先级，可重复"),
    limit: int | None = typer.Option(None, help="限制本次新增任务数"),
):
    """幂等生成名录、入口和候选验证任务；不发起网络请求。"""
    from .coverage_tasks import plan_tasks

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output(plan_tasks(db, cfg, priorities=priority, limit=limit))
    finally:
        db.engine.dispose()


@app.command("coverage-run")
def coverage_run(
    batch: int = typer.Option(20, min=1, max=200, help="本次最多执行的任务数"),
    action: str | None = typer.Option(None, help="只执行指定动作，如 verify_candidate"),
):
    """按任务队列执行，复用租约和退避；单任务失败不关闭企业。"""
    from .coverage_tasks import run_tasks

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output(asyncio.run(run_tasks(db, cfg, batch=batch, action=action)))
    finally:
        db.engine.dispose()


@app.command("coverage-tasks")
def coverage_tasks(
    status: str | None = typer.Option(None, help="pending/running/done/blocked/needs_review"),
    limit: int = typer.Option(200, min=1, max=1000),
):
    """列出覆盖任务及下一步动作。"""
    from .coverage_tasks import task_rows

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output(task_rows(db, status=status, limit=limit))
    finally:
        db.engine.dispose()


@app.command("source-candidates")
def source_candidates(
    status: str | None = typer.Option(None, help="lead/identity_checked/verified/blocked/rejected"),
    limit: int = typer.Option(200, min=1, max=1000),
):
    """列出候选来源；候选默认不计入有效监测。"""
    from .coverage_tasks import candidate_rows

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output(candidate_rows(db, status=status, limit=limit))
    finally:
        db.engine.dispose()


@app.command("source-scope-add")
def source_scope_add(
    source_id: str = typer.Option(..., help="已配置来源 id"),
    employer: str = typer.Option(..., help="参与该来源的企业全称"),
    mode: str = typer.Option("group_umbrella", help="group_umbrella/aggregator/independent"),
    evidence_url: str = typer.Option(..., help="证明该企业由该来源覆盖的官方证据"),
    quote: str = typer.Option(..., help="官方原文片段"),
    cohorts: str = typer.Option("", help="届别，逗号分隔"),
    batch_scope: str = typer.Option("", help="批次或适用范围"),
    status: str = typer.Option("needs_review", help="needs_review/confirmed"),
):
    """登记共享入口与实际企业范围的映射，不自动继承整个集团。"""
    from .coverage_tasks import add_source_scope

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output(
            {
                "id": add_source_scope(
                    db,
                    source_id=source_id,
                    employer_name=employer,
                    mode=mode,
                    evidence_url=evidence_url,
                    quote=quote,
                    cohorts=[int(x) for x in cohorts.split(",") if x.strip().isdigit()],
                    batch_scope=batch_scope,
                    status=status,
                )
            }
        )
    finally:
        db.engine.dispose()


@app.command("registry-refresh")
def registry_refresh(ids: str | None = typer.Option(None, help="逗号分隔名录 id；默认刷新已配置入口")):
    """读取官方企业名录并保存版本；失败保留原底册，不删除企业。"""
    from .coverage import refresh_registries

    cfg = load_settings()
    selected = [x.strip() for x in ids.split(",") if x.strip()] if ids else None
    if selected is not None and (not selected or set(selected) - {x.id for x in cfg.registries}):
        raise typer.BadParameter("包含未知名录 id")
    db = initialize(cfg)
    output(asyncio.run(refresh_registries(db, cfg, selected)))
    db.engine.dispose()


@app.command("registry-repair-snapshots")
def registry_repair_snapshots(
    apply: bool = typer.Option(False, "--apply", help="实际重算并写入；默认只预览"),
):
    """修复历史名录快照的整页摘要和逐条页面绑定。"""
    from .coverage import repair_registry_snapshots

    cfg = load_settings()
    db = initialize(cfg)
    try:
        output({"plans": repair_registry_snapshots(db, apply=apply), "applied": apply})
    finally:
        db.engine.dispose()


@app.command("registry-import-html")
def registry_import_html(
    registry: str = typer.Option(..., "--registry", help="名录 id"),
    file: Path = typer.Option(..., "--file", help="已保存的官方页面 HTML"),
    proof_url: str = typer.Option(..., "--proof-url", help="页面官方 URL"),
    published_at: str = typer.Option("", help="页面可见发布日期 YYYY-MM-DD"),
    note: str = typer.Option("", help="取得方式、TLS/验证码等限制说明"),
    tls_verified: bool = typer.Option(
        False,
        "--tls-verified/--tls-unverified",
        help="人工保存时是否完成了证书校验；默认不提升企业性质核验等级",
    ),
    confirm_official: bool = typer.Option(False, "--confirm-official", help="确认页面来自官方域名"),
):
    """导入人工保存的官方 HTML；用于自动抓取被 TLS 链或超时阻断时留证。"""
    from .coverage import save_registry
    from .urls import on_domains, public_url

    if not confirm_official:
        raise typer.BadParameter("导入前需确认 HTML 来自官方域名")
    cfg = load_settings()
    spec = next((item for item in cfg.registries if item.id == registry), None)
    if not spec:
        raise typer.BadParameter("未知名录 id")
    proof_url = public_url(proof_url)
    if not spec.domains or not on_domains(proof_url, spec.domains):
        raise typer.BadParameter("依据 URL 不在该名录配置的官方域名范围")
    if not file.is_file() or file.stat().st_size > 20_000_000:
        raise typer.BadParameter("HTML 文件不存在或超过 20MB")
    raw = file.read_bytes()
    db = initialize(cfg)
    try:
        result = save_registry(
            db,
            cfg,
            spec,
            raw.decode("utf-8", "replace"),
            pages=[{"url": proof_url, "raw": raw}],
            status="imported",
            publication=published_at or None,
            provenance={
                "acquisition": "manual_official_html"
                if tls_verified
                else "manual_official_html_unverified_tls",
                "tls_verified": tls_verified,
                "scope": spec.scope,
                "complete": False,
                "note": note or "人工保存官方 HTML；未由程序直接请求。",
            },
        )
        output(result)
    finally:
        db.engine.dispose()


@app.command("registry-import")
def registry_import(
    path: Path,
    registry: str = typer.Option(..., "--registry"),
    proof_url: str = typer.Option(...),
    confirm_official: bool = typer.Option(False, help="明确确认已核对 CSV 来自官方底册"),
):
    """导入已核对的官方企业底册 CSV，保留文件与依据；不会计作招聘采集成功。"""
    from .coverage import import_registry_csv

    cfg = load_settings()
    spec = next((r for r in cfg.registries if r.id == registry), None)
    if not spec:
        raise typer.BadParameter("未知名录 id")
    db = initialize(cfg)
    try:
        with path.open("rb") as stream:
            content = stream.read(5_000_001)
        output(import_registry_csv(db, cfg, spec, content, proof_url, confirmed=confirm_official))
    except (ValueError, UnicodeError, OSError) as exc:
        raise typer.BadParameter(str(exc))
    finally:
        db.engine.dispose()


@app.command()
def sync(
    sources: str | None = typer.Option(None, help="逗号分隔来源 id，例如 boc,chnenergy"),
    discover: bool = typer.Option(False, help="使用已配置的 Tavily 补充发现"),
):
    """单次采集、续跑详情并尝试处理待解析公告。"""
    cfg = load_settings()
    selected = [x.strip() for x in sources.split(",") if x.strip()] if sources else None
    if selected is not None and (not selected or set(selected) - {s.id for s in cfg.sources}):
        raise typer.BadParameter("包含未知来源 id")
    db = initialize(cfg)
    run_id = queue_run(db, selected, discover)
    typer.echo(f"同步任务 {run_id}，单源失败不会关闭已有招聘。", err=True)
    result = asyncio.run(execute_run(db, cfg, run_id))
    output(result)
    db.engine.dispose()
    if result["status"] == "failed":
        raise typer.Exit(1)


@app.command()
def serve(
    port: int = 8000, schedule: bool = typer.Option(True, "--schedule/--no-schedule", help="启动后自动同步")
):
    """在 http://127.0.0.1:8000 启动个人工作台。"""
    import uvicorn

    from .web import create_app

    cfg = load_settings()
    marker = cfg.data_dir / "server.pid"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if server_running(marker):
        raise typer.BadParameter("此数据目录已有本地服务在运行")
    marker.write_text(str(os.getpid()))
    typer.echo(
        f"SOE Radar → http://127.0.0.1:{port} · 默认额外付费：{'允许' if cfg.llm.allow_paid else '禁止'}"
    )
    try:
        uvicorn.run(create_app(cfg, scheduling=schedule), host="127.0.0.1", port=port, workers=1)
    finally:
        if marker.exists() and marker.read_text() == str(os.getpid()):
            marker.unlink()


@app.command()
def backup(destination: Path):
    """备份数据库、原文快照及观察记录到本地 tar.gz，不包含凭证。"""
    cfg = load_settings()
    db = initialize(cfg)
    if destination.exists():
        raise typer.BadParameter("目标已存在，请使用新的备份文件名")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="soe-backup-") as tmp:
        path = Path(tmp) / "radar.db"
        with sqlite3.connect(db.path) as source, sqlite3.connect(path) as target:
            source.backup(target)
        with tarfile.open(destination, "w:gz") as archive:
            archive.add(path, arcname="radar.db")
            for name in ["snapshots", "attachments", "registries", "observations.jsonl"]:
                target = cfg.data_dir / name
                if target.exists():
                    archive.add(target, arcname=name)
    db.engine.dispose()
    output({"backup": str(destination.resolve())})


@app.command()
def restore(archive_path: Path, force: bool = typer.Option(False, help="替换现有数据库，先自动备份")):
    """恢复个人备份；本地服务运行时禁止替换。"""
    cfg = load_settings()
    marker = cfg.data_dir / "server.pid"
    if server_running(marker):
        raise typer.BadParameter("请先停止本地服务再恢复")
    if (cfg.data_dir / "radar.db").exists():
        with sqlite3.connect(cfg.data_dir / "radar.db") as connection:
            active = connection.execute("SELECT name FROM leases WHERE expires_at > ?", (stamp(),)).fetchall()
        if active:
            raise typer.BadParameter("同步或模型任务仍在运行，请停止任务并等待租约释放后再恢复")
        if not force:
            raise typer.BadParameter("数据库已存在；确认替换时使用 --force，程序将先自动备份")
        backup(cfg.data_dir / f"before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz")
    with tempfile.TemporaryDirectory(prefix="soe-restore-") as tmp:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.name not in (
                    "radar.db",
                    "observations.jsonl",
                    "snapshots",
                    "attachments",
                    "registries",
                ) and not member.name.startswith(("snapshots/", "attachments/", "registries/")):
                    raise typer.BadParameter("备份含非预期文件")
                if (
                    member.issym()
                    or member.islnk()
                    or ".." in Path(member.name).parts
                    or Path(member.name).is_absolute()
                ):
                    raise typer.BadParameter("备份含不安全路径")
            archive.extractall(tmp, filter="data")
        restored = Path(tmp) / "radar.db"
        if not restored.exists():
            raise typer.BadParameter("备份缺少 radar.db")
        with sqlite3.connect(restored) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise typer.BadParameter("数据库完整性检查失败")
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        for suffix in ["-wal", "-shm"]:
            Path(str(cfg.data_dir / "radar.db") + suffix).unlink(missing_ok=True)
        shutil.copy2(restored, cfg.data_dir / "radar.db")
        for name in ["snapshots", "attachments", "registries", "observations.jsonl"]:
            source = Path(tmp) / name
            if source.is_dir():
                shutil.copytree(source, cfg.data_dir / name, dirs_exist_ok=True)
            elif source.exists():
                shutil.copy2(source, cfg.data_dir / name)
    db = initialize(cfg)
    db.engine.dispose()
    output({"restored": str(cfg.data_dir)})


@app.command()
def observe():
    """汇总真实运行记录；不会把短时测试当成七天观察。"""
    cfg = load_settings()
    path = cfg.data_dir / "observations.jsonl"
    records = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    timestamps = sorted(datetime.fromisoformat(x["finished_at"]) for x in records)
    from zoneinfo import ZoneInfo

    days = {x.astimezone(ZoneInfo(cfg.timezone)).date().isoformat() for x in timestamps}
    elapsed = (timestamps[-1] - timestamps[0]).total_seconds() / 86400 if timestamps else 0
    output(
        {
            "completed_runs": len(records),
            "observed_dates": sorted(days),
            "elapsed_days": round(elapsed, 3),
            "seven_day_observation_complete": len(days) >= 7 and elapsed >= 6,
            "runs_with_issues": sum(x["status"] != "success" for x in records),
            "note": "达到天数不代表全部来源稳定，还需检查逐源异常及日期错误。",
        }
    )


@app.command("repair")
def repair_data(
    apply_changes: bool = typer.Option(False, "--apply", help="备份后合并同届重复记录并离线重解析"),
):
    """默认预览，保留所有原文、人工决定、备注和被合并记录。"""
    from .maintenance import repair, reparse

    cfg = load_settings()
    if server_running(cfg.data_dir / "server.pid"):
        raise typer.BadParameter("请先停止本地服务再修复")
    if apply_changes:
        backup(
            cfg.data_dir.parent
            / "backups"
            / ("before-repair-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".tar.gz")
        )
    db = initialize(cfg)
    from uuid import uuid4

    owner = "repair-" + uuid4().hex
    locked = []
    try:
        if apply_changes:
            for name in ("sync", "model"):
                if not db.acquire(name, owner, 600):
                    raise typer.BadParameter("同步或模型任务正在运行，请等待租约释放后重试")
                locked.append(name)
        result = repair(db, cfg, apply=apply_changes)
        if apply_changes:
            result["reparse"] = asyncio.run(reparse(db, cfg))
            result["second_pass"] = repair(db, cfg, apply=True)
            (cfg.data_dir / "repair-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        output(result)
    finally:
        for name in locked:
            db.release(name, owner)
        db.engine.dispose()


@dataset_app.command("export")
def dataset_export(destination: Path):
    """导出真实文档为待人工标注 JSONL，不自动填充真值。"""
    cfg = load_settings()
    db = initialize(cfg)
    if destination.exists():
        raise typer.BadParameter("文件已存在，请选择新路径")
    destination.parent.mkdir(parents=True, exist_ok=True)
    from .maintenance import label_group, saved_input

    with db.session() as s, destination.open("w") as out:
        groups = {}
        activity_groups = {}
        for doc in s.scalars(select(Document).order_by(Document.fetched_at.desc())):
            value = saved_input(doc, cfg)
            group = label_group(value)
            if doc.opportunity_id:
                from .models import Opportunity

                opportunity = s.get(Opportunity, doc.opportunity_id)
                if opportunity:
                    group = activity_groups.setdefault(opportunity.activity_id, group)
            groups.setdefault(group, (doc, value))
        for index, (group, (doc, value)) in enumerate(sorted(groups.items())):
            record = {
                "document": value.model_dump(mode="json"),
                "sha256": doc.digest,
                "group_id": group,
                "split": "dev" if index < 100 else "test",
                "annotation_type": "unlabeled",
                "annotator": "",
                "expected": None,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    db.engine.dispose()
    output({"path": str(destination), "note": "需人工核对原文后填写 expected；不得把模型输出标为人工真值。"})


@dataset_app.command("evaluate")
def evaluate(path: Path):
    """仅评估已人工标注的留出集，另报数据集是否达到 100/50 要求。"""
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    labeled = [
        r for r in records if r.get("annotation_type") == "human" and r.get("annotator") and r.get("expected")
    ]
    from .maintenance import label_group

    groups = {}
    duplicates = 0
    leakage = set()
    for row in labeled:
        group = row.get("group_id") or label_group(DocumentInput.model_validate(row["document"]))
        if group in groups:
            duplicates += 1
            if groups[group].get("split") != row.get("split"):
                leakage.add(group)
            continue
        groups[group] = row
    held = [r for g, r in groups.items() if r.get("split") == "test" and g not in leakage]
    dev = [r for g, r in groups.items() if r.get("split") == "dev" and g not in leakage]
    predicted, correct, clear, recalled = 0, 0, 0, 0
    for row in held:
        doc = DocumentInput.model_validate(row["document"])
        result = extract_rules(doc).deadline
        expected = row["expected"]
        explicit = result.kind in ("date", "datetime") and result.value is not None
        matches = result.value == expected.get("value") and result.kind == expected.get("kind")
        predicted += int(explicit)
        correct += int(explicit and matches)
        if expected.get("clear", True) and expected.get("value"):
            clear += 1
            recalled += int(matches)
    precision = correct / predicted if predicted else None
    recall = recalled / clear if clear else None
    ready = len(dev) >= 100 and len(held) >= 50 and not leakage and not duplicates
    output(
        {
            "scope": "rule_parser_only",
            "human_dev": len(dev),
            "human_holdout": len(held),
            "dataset_requirement_met": ready,
            "duplicate_annotations": duplicates,
            "leaked_groups": len(leakage),
            "precision": precision,
            "clear_recall": recall,
            "abstention_rate": (1 - predicted / len(held)) if held else None,
            "target_met": bool(
                ready
                and precision is not None
                and precision >= 0.98
                and recall is not None
                and recall >= 0.85
            ),
            "note": "这是规则解析指标；不代表模型解析或端到端自动发布已达到相同指标。",
        }
    )


if __name__ == "__main__":
    app()
