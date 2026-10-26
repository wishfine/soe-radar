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


def test_alias_merge_merges_same_period_tasks_without_unique_error(tmp_path):
    from soe_radar.coverage_tasks import _key
    from soe_radar.identity import merge_alias_employers

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        target = s.scalar(select(Employer).where(Employer.name == "示例集团有限公司"))
        duplicate = Employer(name="示例集团有限公司[示例集团别称]")
        s.add(duplicate)
        s.flush()
        for employer_id in (target.id, duplicate.id):
            s.add(
                CoverageTask(
                    task_key=_key("employer", employer_id, "discover_entry", "cohort:2027"),
                    scope_type="employer",
                    scope_id=employer_id,
                    action="discover_entry",
                    period_key="cohort:2027",
                    due_at="2000-01-01T00:00:00+00:00",
                )
            )
        target_id = target.id
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        tasks = list(s.scalars(select(CoverageTask).where(CoverageTask.scope_id == target_id)))
        assert len(tasks) == 1


def test_scope_merge_preserves_quote_and_unconfirmed_cohort(tmp_path):
    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import SourceEmployerScope

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        target = s.scalar(select(Employer).where(Employer.name == "示例集团有限公司"))
        duplicate = Employer(name="示例集团有限公司[示例集团别称]")
        s.add(duplicate)
        s.flush()
        target_id = target.id
    quote = "示例集团参加2027校园招聘，采用统一报名入口。"
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例集团有限公司",
        evidence_url="https://example.org/2027",
        quote=quote,
        cohorts=[2027],
        batch_scope="统一校招",
        status="confirmed",
    )
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例集团有限公司[示例集团别称]",
        evidence_url="https://example.org/2028-lead",
        quote="2028届参与范围尚待确认",
        cohorts=[2028],
        batch_scope="统一校招",
        status="needs_review",
    )
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        scope = s.scalar(select(SourceEmployerScope))
        assert scope.quote == quote
        assert scope.cohorts == [2027]
        assert len(scope.provenance) == 2
    report_cfg = cfg.model_copy(
        update={"sources": [cfg.sources[0].model_copy(update={"employer": "另一集团有限公司"})]}
    )
    item = next(
        row for row in coverage_report(db, report_cfg, cohort=2028)["items"] if row["id"] == target_id
    )
    assert item["sources"] == []


def test_application_not_announced_is_not_found(tmp_path):
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
            return [("https://example.org/notice", "2027校园招聘预告")]

        async def document(self, url, source, title):
            return SimpleNamespace(
                title=title,
                text="2027校园招聘预告。报名方式另行通知，暂未公布报名入口。",
                published_at=None,
                attachments=[],
            )

        async def close(self):
            pass

    with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
    with db.session() as s:
        candidate = s.get(SourceCandidate, cid)
        assert candidate.status == "readable"
        assert candidate.detail_result["verification"]["application"] == "needs_review"


def test_explicit_scope_downgrade_and_reconfirm_round_trip(tmp_path):
    from soe_radar.models import SourceEmployerScope

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        child = Employer(name="示例子公司有限公司", org_type="soe", proof_url="https://example.org/about")
        s.add(child)
        s.flush()
        child_id = child.id
    values = dict(
        source_id="group",
        employer_name="示例子公司有限公司",
        evidence_url="https://example.org/2027",
        cohorts=[2027],
        batch_scope="2027秋招",
    )
    scope_id = add_source_scope(db, **values, quote="确认范围", status="confirmed")
    add_source_scope(db, **values, quote="复核撤回", status="needs_review")
    with db.session() as s:
        scope = s.get(SourceEmployerScope, scope_id)
        assert scope.status == "needs_review"
        assert any(record["status"] == "confirmed" for record in scope.provenance)
    child = next(row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["id"] == child_id)
    assert child["sources"] == []
    add_source_scope(db, **values, quote="重新确认", status="confirmed")
    with db.session() as s:
        assert s.get(SourceEmployerScope, scope_id).status == "confirmed"
    child = next(row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["id"] == child_id)
    assert [source["id"] for source in child["sources"]] == ["group"]


def test_application_requires_action_bound_to_target(tmp_path):
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
        text = ""

        def __init__(self, cfg):
            pass

        async def page(self, url, source):
            return SimpleNamespace(title="招聘", text="列表", via="http")

        def candidates(self, page, source):
            return [("https://example.org/notice", "2027校园招聘公告")]

        async def document(self, url, source, title):
            return SimpleNamespace(title=title, text=self.text, published_at=None, attachments=[])

        async def close(self):
            pass

    for text, expected in [
        ("咨询邮箱：help@example.org（仅接受咨询，不接收简历）。", "needs_review"),
        ("集团概况：https://example.org/about。没有提供申请路径。", "needs_review"),
        ("请勿将简历发送至 help@example.org，该邮箱仅供咨询，不接收简历。", "needs_review"),
        ("了解集团概况请访问 https://example.org/about，此处未提供申请路径。", "needs_review"),
        ("请将简历发送至 hr@example.org 完成报名。", "found"),
        ("请通过 https://apply.example.org 提交申请。", "found"),
    ]:
        FakeFetcher.text = text
        with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
            asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
        with db.session() as s:
            row = s.get(SourceCandidate, cid)
            assert row.detail_result["verification"]["application"] == expected


def test_scope_merge_uses_decision_timestamp_not_input_order(tmp_path):
    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import SourceEmployerScope

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    canonical = "示例单位有限公司"
    alias = "示例单位有限公司[示例单位别称]"
    with db.session() as s:
        target = Employer(name=canonical, org_type="soe", proof_url="https://example.org/about")
        s.add_all([target, Employer(name=alias)])
        s.flush()
        target_id = target.id
    base = dict(source_id="group", cohorts=[2027], batch_scope="2027秋招")
    add_source_scope(
        db,
        **base,
        employer_name=alias,
        evidence_url="https://example.org/old",
        quote="旧别名确认",
        status="confirmed",
    )
    scope_id = add_source_scope(
        db,
        **base,
        employer_name=canonical,
        evidence_url="https://example.org/new",
        quote="规范名确认",
        status="confirmed",
    )
    add_source_scope(
        db,
        **base,
        employer_name=canonical,
        evidence_url="https://example.org/new",
        quote="最新复核撤回",
        status="needs_review",
    )
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        row = s.get(SourceEmployerScope, scope_id)
        assert row.status == "needs_review"
    child = next(row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["id"] == target_id)
    assert child["sources"] == []


def test_legacy_scope_without_decided_at_conflicts_as_needs_review(tmp_path):
    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import SourceEmployerScope

    cfg = task_settings(tmp_path)
    db = initialize(cfg)
    canonical = "示例单位有限公司"
    alias = "示例单位有限公司[示例单位别称]"
    with db.session() as s:
        target = Employer(name=canonical, org_type="soe", proof_url="https://example.org/about")
        s.add_all([target, Employer(name=alias)])
        s.flush()
        target_id = target.id
    base = dict(source_id="group", cohorts=[2027], batch_scope="2027秋招")
    add_source_scope(
        db,
        **base,
        employer_name=alias,
        evidence_url="https://example.org/old",
        quote="旧别名确认",
        status="confirmed",
    )
    scope_id = add_source_scope(
        db,
        **base,
        employer_name=canonical,
        evidence_url="https://example.org/new",
        quote="规范名确认",
        status="confirmed",
    )
    add_source_scope(
        db,
        **base,
        employer_name=canonical,
        evidence_url="https://example.org/new",
        quote="较晚撤回",
        status="needs_review",
    )
    with db.session() as s:
        for row in s.scalars(select(SourceEmployerScope)):
            row.provenance = [
                {key: value for key, value in record.items() if key != "decided_at"}
                for record in row.provenance
            ]
            from soe_radar.coverage_tasks import recompute_scope

            recompute_scope(row)
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        assert s.get(SourceEmployerScope, scope_id).status == "needs_review"
    child = next(row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["id"] == target_id)
    assert child["sources"] == []


def test_application_sentence_context_and_mixed_emails(tmp_path):
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
        text = ""

        def __init__(self, cfg):
            pass

        async def page(self, url, source):
            return SimpleNamespace(title="招聘", text="列表", via="http")

        def candidates(self, page, source):
            return [("https://example.org/notice", "2027校园招聘公告")]

        async def document(self, url, source, title):
            return SimpleNamespace(title=title, text=self.text, published_at=None, attachments=[])

        async def close(self):
            pass

    FakeFetcher.text = (
        "2027校园招聘岗位介绍。报名入口尚未公布；请访问 https://example.org/jobs 查看招聘岗位；"
        "网申开放时间另行通知。"
    )
    with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
    with db.session() as s:
        assert s.get(SourceCandidate, cid).status == "readable"

    FakeFetcher.text = (
        "咨询邮箱：help@example.org，仅供咨询，不接收简历。请将简历发送至 hr@example.org 完成报名。"
    )
    with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
    with db.session() as s:
        row = s.get(SourceCandidate, cid)
        assert row.status == "verified"
        assert row.detail_result["verification"]["application"] == "found"
        assert row.detail_result["application_target"] == "hr@example.org"
