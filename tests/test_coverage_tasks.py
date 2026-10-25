from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from soe_radar.config import Settings
from soe_radar.coverage import coverage_report
from soe_radar.coverage_tasks import (
    _reset_stale,
    add_source_candidate,
    add_source_scope,
    plan_tasks,
    run_tasks,
    task_rows,
)
from soe_radar.db import initialize
from soe_radar.models import CoverageTask, Employer


def task_settings(tmp_path):
    return Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "missing-registry",
                    "name": "待查名录",
                    "category": "local",
                    "region": "示例省",
                    "authority_url": "https://example.gov.cn/",
                }
            ],
            "sources": [
                {
                    "id": "group",
                    "name": "示例集团",
                    "employer": "示例集团有限公司",
                    "url": "https://example.org/jobs",
                    "domains": ["example.org"],
                }
            ],
        }
    )


def test_plan_tasks_is_idempotent_and_does_not_count_candidates(tmp_path):
    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    first = plan_tasks(db, cfg)
    second = plan_tasks(db, cfg)
    assert first["created"] >= 1
    assert second["created"] == 0
    assert second["existing"] >= first["created"]
    with db.session() as s:
        employer = s.scalar(select(Employer).where(Employer.name == "示例集团有限公司"))
        assert employer
    before = coverage_report(db, cfg)["summary"]["with_entry"]
    add_source_candidate(
        db,
        employer_id=employer.id,
        employer_name=employer.name,
        url="https://example.org/jobs",
        discovery_method="test",
    )
    assert coverage_report(db, cfg)["summary"]["with_entry"] == before
    third = plan_tasks(db, cfg)
    assert third["created"] >= 1
    assert any(row["action"] == "verify_candidate" for row in task_rows(db, status="pending"))


def test_run_tasks_success_and_failure_backoff(tmp_path):
    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    plan_tasks(db, cfg)

    async def fail(_db, _cfg, _task):
        raise RuntimeError("offline test failure")

    result = run_tasks(db, cfg, batch=1, executor=fail)
    import asyncio

    outcome = asyncio.run(result)
    assert outcome["processed"] == 1
    with db.session() as s:
        task = s.scalar(select(CoverageTask))
        assert task.attempts == 1
        assert task.status == "pending"
        assert task.due_at > datetime.now(UTC).isoformat()

    async def succeed(_db, _cfg, _task):
        return {"status": "done", "next_action": ""}

    with db.session() as s:
        task = s.scalar(select(CoverageTask))
        task.due_at = "2000-01-01T00:00:00+00:00"
    asyncio.run(run_tasks(db, cfg, batch=1, executor=succeed))
    with db.session() as s:
        task = s.scalar(select(CoverageTask))
        assert task.status == "done"


def test_stale_running_task_is_recovered(tmp_path):
    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    plan_tasks(db, cfg)
    with db.session() as s:
        task = s.scalar(select(CoverageTask))
        task.status = "running"
        task.updated_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _reset_stale(db)
    with db.session() as s:
        task = s.scalar(select(CoverageTask))
        assert task.status == "pending"
        assert "恢复" in task.last_error


def test_source_scope_is_explicit_and_deduplicated(tmp_path):
    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        s.add(Employer(name="示例子公司有限公司"))
    first = add_source_scope(
        db,
        source_id="group",
        employer_name="示例子公司有限公司",
        evidence_url="https://example.org/group",
        quote="示例子公司由集团统一招聘",
        cohorts=[2027],
        batch_scope="2027秋招",
    )
    second = add_source_scope(
        db,
        source_id="group",
        employer_name="示例子公司有限公司",
        evidence_url="https://example.org/group",
        quote="示例子公司由集团统一招聘",
        cohorts=[2027],
        batch_scope="2027秋招",
        status="confirmed",
    )
    assert first == second
    from soe_radar.models import SourceEmployerScope

    with db.session() as s:
        rows = list(s.scalars(select(SourceEmployerScope)))
        assert len(rows) == 1
        assert rows[0].status == "confirmed"
        assert rows[0].cohorts == [2027]
