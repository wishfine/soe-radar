"""Offline review probes; only temporary databases, no network/personal data writes.

Exit 1 means one or more expected guarantees are not met.
"""

import asyncio
import json
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from soe_radar.config import Settings
from soe_radar.coverage import coverage_report
from soe_radar.coverage_tasks import (
    _verify_candidate,
    add_source_candidate,
    add_source_scope,
    plan_tasks,
    run_tasks,
)
from soe_radar.db import initialize
from soe_radar.identity import merge_alias_employers
from soe_radar.models import CoverageTask, Employer, SourceCandidate, SourceState, stamp

results = []


def emit(probe, expected, actual):
    row = {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False))


def config(folder):
    return Settings.model_validate(
        {
            "data_dir": str(folder),
            "bundled_catalogue": False,
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


def add_task(db, key, status="pending"):
    with db.session() as s:
        row = CoverageTask(
            task_key=key,
            scope_type="test",
            scope_id=key,
            action="test",
            status=status,
            due_at="2000-01-01T00:00:00+00:00",
        )
        s.add(row)
        s.flush()
        return row.id


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)

    cfg = config(root / "retry")
    db = initialize(cfg)
    add_task(db, "retry", "blocked")

    async def succeeds(*args):
        return {"status": "done"}

    outcome = asyncio.run(run_tasks(db, cfg, executor=succeeds))
    emit("due_blocked_task_can_retry", 1, outcome["processed"])
    db.engine.dispose()

    cfg = config(root / "verify")
    db = initialize(cfg)
    with db.session() as s:
        employer = s.scalar(select(Employer))
    cid = add_source_candidate(
        db,
        employer_id=employer.id,
        employer_name=employer.name,
        url="https://example.org/jobs",
    )

    class FakeFetcher:
        def __init__(self, cfg):
            pass

        async def page(self, url, source):
            return SimpleNamespace(title="招聘栏目", text="示例企业招聘栏目", via="http")

        def candidates(self, page, source):
            return [("https://example.org/notice", "2027校园招聘公告")]

        async def document(self, url, source, title):
            return SimpleNamespace(
                title=title,
                text="招聘正文：报名方式和截止日期均详见附件。" * 5,
                published_at=None,
                attachments=[{"label": "报名须知.pdf", "status": "failed"}],
            )

        async def close(self):
            pass

    with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
    with db.session() as s:
        row = s.get(SourceCandidate, cid)
        emit("missing_application_and_required_attachment_do_not_verify", "needs_review", row.status)
    db.engine.dispose()

    cfg = config(root / "scope")
    db = initialize(cfg)
    child_name = "示例子公司有限公司"
    with db.session() as s:
        s.add(Employer(name=child_name, org_type="soe", proof_url="https://example.org/about"))
        state = s.get(SourceState, "group")
        state.status = "success"
        state.last_success = stamp()
    add_source_scope(
        db,
        source_id="group",
        employer_name=child_name,
        evidence_url="https://example.org/notice",
        quote="示例子公司有限公司参加本集团2027校园招聘",
        cohorts=[2027],
        batch_scope="2027秋招",
        status="confirmed",
    )
    report = coverage_report(db, cfg, cohort=2027)
    child = next(row for row in report["items"] if row["name"] == child_name)
    emit("confirmed_scope_is_used_by_coverage_report", ["group"], [x["id"] for x in child["sources"]])
    with db.session() as s:
        s.get(SourceState, "group").status = "error"
    child = next(row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["name"] == child_name)
    emit("shared_source_failure_reaches_participant", "blocked", child["status"])
    db.engine.dispose()

    for reference in ("candidate", "scope"):
        cfg = config(root / ("merge-" + reference))
        db = initialize(cfg)
        with db.session() as s:
            duplicate = Employer(name="示例集团有限公司[示例集团别称]")
            s.add(duplicate)
            s.flush()
            duplicate_id = duplicate.id
        if reference == "candidate":
            add_source_candidate(
                db,
                employer_id=duplicate_id,
                employer_name=duplicate.name,
                url="https://example.org/campus",
            )
        else:
            add_source_scope(
                db,
                source_id="group",
                employer_name=duplicate.name,
                evidence_url="https://example.org/notice",
                quote="示例集团别称参加招聘",
            )
        actual = "ok"
        try:
            merge_alias_employers(db, apply=True)
        except IntegrityError:
            actual = "IntegrityError: FOREIGN KEY constraint failed"
        emit("alias_merge_preserves_new_" + reference + "_references", "ok", actual)
        db.engine.dispose()

    cfg = config(root / "lease")
    db = initialize(cfg)
    first, second = add_task(db, "first"), add_task(db, "second")
    calls = Counter()

    async def nested_executor(_db, _cfg, task):
        calls[task.id] += 1
        return {"status": "done"}

    async def outer_executor(_db, _cfg, task):
        calls[task.id] += 1
        if task.id == first:
            # Simulate crossing the 900-second lease lifetime without waiting.
            with db.session() as s:
                s.execute(text("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE name='coverage'"))
            await run_tasks(db, cfg, batch=1, executor=nested_executor)
        return {"status": "done"}

    asyncio.run(run_tasks(db, cfg, batch=2, executor=outer_executor))
    emit("second_task_runs_once_after_lease_expiry", 1, calls[second])
    db.engine.dispose()

    cfg = config(root / "priority")
    cfg = cfg.model_copy(update={"registries": []})
    db = initialize(cfg)
    with db.session() as s:
        employer = s.scalar(select(Employer))
    add_source_candidate(
        db,
        employer_id=employer.id,
        employer_name=employer.name,
        url="https://example.org/pending",
    )
    plan_tasks(db, cfg, priorities=["P0"], limit=0)
    with db.session() as s:
        rows = list(s.scalars(select(CoverageTask)))
        emit("P0_plan_with_zero_limit_creates_no_P1_tasks", [], [row.priority for row in rows])
    db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
