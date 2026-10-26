from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from soe_radar.config import Settings
from soe_radar.coverage import coverage_report
from soe_radar.coverage_tasks import (
    _verify_candidate,
    add_source_candidate,
    add_source_scope,
    plan_tasks,
    recover_tasks,
    requeue_tasks,
    run_tasks,
    task_rows,
)
from soe_radar.db import initialize
from soe_radar.models import CoverageTask, Employer, SourceCandidate


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
    recover_tasks(db)
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


def test_due_blocked_task_is_retried_and_requeue_is_audited(tmp_path):
    import asyncio

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        task = CoverageTask(
            task_key="blocked-retry",
            scope_type="test",
            scope_id="blocked-retry",
            action="test",
            status="blocked",
            attempts=0,
            due_at="2000-01-01T00:00:00+00:00",
        )
        s.add(task)
        s.flush()
        task_id = task.id

    async def succeeds(*args):
        return {"status": "done"}

    assert asyncio.run(run_tasks(db, cfg, executor=succeeds))["processed"] == 1
    with db.session() as s:
        assert s.get(CoverageTask, task_id).status == "done"

    with db.session() as s:
        task = s.get(CoverageTask, task_id)
        task.status = "blocked"
        task.attempts = 5
        task.due_at = "2000-01-01T00:00:00+00:00"
    assert asyncio.run(run_tasks(db, cfg, executor=succeeds))["processed"] == 0
    requeue_tasks(db, ids=[task_id], note="test requeue")
    with db.session() as s:
        task = s.get(CoverageTask, task_id)
        assert task.status == "pending" and task.attempts == 0
        assert task.evidence["requeue_history"]


def test_lease_expiry_does_not_duplicate_task_execution(tmp_path):
    import asyncio
    from collections import Counter

    from sqlalchemy import text

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        first = CoverageTask(
            task_key="lease-first",
            scope_type="test",
            scope_id="lease-first",
            action="test",
            status="pending",
            due_at="2000-01-01T00:00:00+00:00",
        )
        second = CoverageTask(
            task_key="lease-second",
            scope_type="test",
            scope_id="lease-second",
            action="test",
            status="pending",
            due_at="2000-01-01T00:00:00+00:00",
        )
        s.add_all([first, second])
        s.flush()
        first_id, second_id = first.id, second.id
    calls = Counter()

    async def nested(_db, _cfg, task):
        calls[task.id] += 1
        return {"status": "done"}

    async def outer(_db, _cfg, task):
        calls[task.id] += 1
        if task.id == first_id:
            with db.session() as s:
                s.execute(
                    text("UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE name='coverage'")
                )
            await run_tasks(db, cfg, batch=1, executor=nested)
        return {"status": "done"}

    asyncio.run(run_tasks(db, cfg, batch=2, executor=outer))
    assert calls[second_id] == 1


def test_plan_priority_and_limit_apply_to_candidates(tmp_path):
    cfg = task_settings(tmp_path)
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
        assert list(s.scalars(select(CoverageTask))) == []


def test_verified_requires_application_and_attachment_stages(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        employer = s.scalar(select(Employer))
    cid = add_source_candidate(
        db,
        employer_id=employer.id,
        employer_name=employer.name,
        url="https://example.org/jobs",
        discovery_method="official_site_nav",
        identity_evidence="https://example.org/about",
    )

    class FakeFetcher:
        def __init__(self, cfg):
            pass

        async def page(self, url, source):
            return SimpleNamespace(title="招聘", text="列表", via="http")

        def candidates(self, page, source):
            return [("https://example.org/notice", "2027校园招聘公告")]

        async def document(self, url, source, title):
            return SimpleNamespace(
                title=title,
                text="报名方式和截止日期均详见附件。",
                published_at=None,
                attachments=[{"label": "报名须知.pdf", "status": "failed"}],
            )

        async def close(self):
            pass

    with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
    with db.session() as s:
        assert s.get(SourceCandidate, cid).status == "needs_review"


def test_alias_merge_migrates_candidate_and_scope_references(tmp_path):
    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import SourceEmployerScope

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        target = s.scalar(select(Employer).where(Employer.name == "示例集团有限公司"))
        duplicate = Employer(name="示例集团有限公司[示例集团别称]")
        s.add(duplicate)
        s.flush()
        duplicate_id, target_id = duplicate.id, target.id
    candidate_id = add_source_candidate(
        db,
        employer_id=duplicate_id,
        employer_name="示例集团有限公司[示例集团别称]",
        url="https://example.org/campus",
    )
    scope_id = add_source_scope(
        db,
        source_id="group",
        employer_name="示例集团有限公司[示例集团别称]",
        evidence_url="https://example.org/notice",
        quote="示例集团别称参加招聘",
        cohorts=[2027],
        batch_scope="2027秋招",
    )
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        assert s.get(SourceCandidate, candidate_id).employer_id == target_id
        assert s.get(SourceEmployerScope, scope_id).employer_id == target_id


def test_alias_merge_merges_duplicate_candidate_and_scope_rows(tmp_path):
    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import SourceEmployerScope

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        target = s.scalar(select(Employer).where(Employer.name == "示例集团有限公司"))
        duplicate = Employer(name="示例集团有限公司[示例集团别称]")
        s.add(duplicate)
        s.flush()
        duplicate_id, target_id = duplicate.id, target.id
    add_source_candidate(
        db,
        employer_id=target_id,
        employer_name="示例集团有限公司",
        url="https://example.org/campus",
    )
    add_source_candidate(
        db,
        employer_id=duplicate_id,
        employer_name="示例集团有限公司[示例集团别称]",
        url="https://example.org/campus",
    )
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例集团有限公司",
        evidence_url="https://example.org/notice",
        quote="目标集团参加招聘",
        cohorts=[2027],
        batch_scope="2027秋招",
    )
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例集团有限公司[示例集团别称]",
        evidence_url="https://example.org/notice",
        quote="别名集团参加招聘",
        cohorts=[2027],
        batch_scope="2027秋招",
    )
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        candidates = list(s.scalars(select(SourceCandidate)))
        scopes = list(s.scalars(select(SourceEmployerScope)))
        assert len(candidates) == 1 and candidates[0].employer_id == target_id
        assert len(scopes) == 1 and scopes[0].employer_id == target_id


def test_confirmed_scope_participates_in_coverage_and_blocked_propagation(tmp_path):
    from soe_radar.models import SourceState

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        s.add(Employer(name="示例子公司有限公司", org_type="soe", proof_url="https://example.org/about"))
        state = s.get(SourceState, "group")
        state.status = "success"
        state.last_success = datetime.now(UTC).isoformat()
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例子公司有限公司",
        evidence_url="https://example.org/notice",
        quote="示例子公司参加集团2027校园招聘",
        cohorts=[2027],
        batch_scope="2027秋招",
        status="confirmed",
    )
    report = coverage_report(db, cfg, cohort=2027)
    child = next(row for row in report["items"] if row["name"] == "示例子公司有限公司")
    assert [source["id"] for source in child["sources"]] == ["group"]
    assert child["status"] != "missing_entry"
    with db.session() as s:
        s.get(SourceState, "group").status = "error"
    child = next(
        row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["name"] == "示例子公司有限公司"
    )
    assert child["status"] == "blocked"


def test_unconfirmed_or_aggregator_scope_does_not_count_as_monitoring(tmp_path):
    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        s.add(Employer(name="示例子公司有限公司", org_type="soe", proof_url="https://example.org/about"))
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例子公司有限公司",
        evidence_url="https://example.org/notice",
        quote="待确认",
        cohorts=[2027],
        batch_scope="2027秋招",
        status="needs_review",
    )
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例子公司有限公司",
        mode="aggregator",
        evidence_url="https://example.org/notice",
        quote="聚合发现",
        cohorts=[2027],
        batch_scope="2027秋招",
        status="confirmed",
    )
    child = next(
        row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["name"] == "示例子公司有限公司"
    )
    assert child["sources"] == []
    assert child["status"] == "missing_entry"


def test_plan_limit_is_global_across_registry_employer_and_candidate(tmp_path):
    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    result = plan_tasks(db, cfg, priorities=["P1"], limit=1)
    assert result["created"] == 1
    with db.session() as s:
        assert len(list(s.scalars(select(CoverageTask)))) == 1


def test_verify_stages_distinguish_readable_and_verified(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        employer = s.scalar(select(Employer))
    cid = add_source_candidate(
        db,
        employer_id=employer.id,
        employer_name=employer.name,
        url="https://example.org/jobs",
        discovery_method="official_site_nav",
        identity_evidence="https://example.org/about",
    )

    class FakeFetcher:
        def __init__(self, cfg):
            pass

        async def page(self, url, source):
            return SimpleNamespace(title="招聘", text="列表", via="http")

        def candidates(self, page, source):
            return [("https://example.org/notice", "2027校园招聘公告")]

        async def document(self, url, source, title):
            return SimpleNamespace(
                title=title,
                text="请通过网申系统报名，截止时间以系统为准。",
                published_at=None,
                attachments=[],
            )

        async def close(self):
            pass

    with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
    with db.session() as s:
        assert s.get(SourceCandidate, cid).status == "verified"

    class ReadableFetcher(FakeFetcher):
        async def document(self, url, source, title):
            return SimpleNamespace(title=title, text="招聘正文", published_at=None, attachments=[])

    with patch("soe_radar.coverage_tasks.Fetcher", ReadableFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
    with db.session() as s:
        assert s.get(SourceCandidate, cid).status == "readable"
