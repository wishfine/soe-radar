from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from importlib.resources import files
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
        path.write_text(files("soe_radar").joinpath("defaults.yaml").read_text())
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

    async def checks():
        fetcher = Fetcher(cfg)
        rows = []
        try:
            if live:
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
        if live or model
        else [{"id": s.id, "url": s.url, "enabled": s.enabled, "status": "not_probed"} for s in cfg.sources]
    )
    output(result)
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
            for name in ["snapshots", "attachments", "observations.jsonl"]:
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
                ) and not member.name.startswith(("snapshots/", "attachments/")):
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
        for name in ["snapshots", "attachments", "observations.jsonl"]:
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


@dataset_app.command("export")
def dataset_export(destination: Path):
    """导出真实文档为待人工标注 JSONL，不自动填充真值。"""
    cfg = load_settings()
    db = initialize(cfg)
    if destination.exists():
        raise typer.BadParameter("文件已存在，请选择新路径")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with db.session() as s, destination.open("w") as out:
        for index, doc in enumerate(s.scalars(select(Document).order_by(Document.fetched_at))):
            record = {
                "document": {
                    "url": doc.url,
                    "title": doc.title,
                    "text": doc.text,
                    "published_at": doc.published_at,
                    "source_id": doc.source_id,
                },
                "sha256": doc.digest,
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
    held = [r for r in labeled if r.get("split") == "test"]
    dev = [r for r in labeled if r.get("split") == "dev"]
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
    ready = len(dev) >= 100 and len(held) >= 50
    output(
        {
            "scope": "rule_parser_only",
            "human_dev": len(dev),
            "human_holdout": len(held),
            "dataset_requirement_met": ready,
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
