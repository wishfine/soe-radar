"""Independent offline follow-up for CQ1/CQ4; temporary DBs only."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from soe_radar.config import Settings
from soe_radar.coverage import coverage_report
from soe_radar.coverage_tasks import _key, _verify_candidate, add_source_candidate, add_source_scope
from soe_radar.db import initialize
from soe_radar.identity import merge_alias_employers
from soe_radar.models import CoverageTask, Employer, SourceCandidate, SourceEmployerScope

results = []


def emit(probe, expected, actual):
    row = {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False))


def setup(folder):
    cfg = Settings.model_validate(
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
    db = initialize(cfg)
    with db.session() as s:
        target = s.scalar(select(Employer))
        duplicate = Employer(name="示例集团有限公司[示例集团别称]")
        s.add(duplicate)
        s.flush()
        return cfg, db, target.id, duplicate.id


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    cfg, db, target_id, duplicate_id = setup(root / "tasks")
    with db.session() as s:
        for employer_id in (target_id, duplicate_id):
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
    actual = "ok"
    try:
        merge_alias_employers(db, apply=True)
    except IntegrityError as exc:
        actual = str(exc.orig)
    emit("same_period_employer_tasks_merge_without_unique_error", "ok", actual)
    db.engine.dispose()

    cfg, db, target_id, duplicate_id = setup(root / "quotes")
    original = "示例集团参加2027校园招聘，采用统一报名入口。"
    for name in ("示例集团有限公司", "示例集团有限公司[示例集团别称]"):
        add_source_scope(
            db,
            source_id="group",
            employer_name=name,
            evidence_url="https://example.org/notice",
            quote=original,
            cohorts=[2027],
            batch_scope="秋招",
            status="confirmed",
        )
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        scope = s.scalar(select(SourceEmployerScope))
        emit("identical_scope_quotes_survive_merge", original, scope.quote)
    db.engine.dispose()

    cfg, db, target_id, duplicate_id = setup(root / "cohorts")
    add_source_scope(
        db,
        source_id="group",
        employer_name="示例集团有限公司",
        evidence_url="https://example.org/2027",
        quote="2027届由集团统一招聘",
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
        emit("unconfirmed_cohort_is_not_promoted_by_merge", [2027], scope.cohorts)
    # The group's exact own entry would mask the erroneous shared mapping for it;
    # inspect a separate SourceSpec whose exact employer is not the merged entity.
    report_cfg = cfg.model_copy(
        update={"sources": [cfg.sources[0].model_copy(update={"employer": "另一集团有限公司"})]}
    )
    item = next(row for row in coverage_report(db, report_cfg, cohort=2028)["items"] if row["id"] == target_id)
    emit("unconfirmed_2028_scope_does_not_count_as_entry", [], [row["id"] for row in item["sources"]])
    db.engine.dispose()

    cfg, db, target_id, duplicate_id = setup(root / "application")
    candidate_id = add_source_candidate(
        db,
        employer_id=target_id,
        employer_name="示例集团有限公司",
        url="https://example.org/jobs",
        identity_evidence="https://example.org/about",
        discovery_method="official_site_nav",
    )

    class FakeFetcher:
        def __init__(self, cfg):
            pass

        async def page(self, url, source):
            return SimpleNamespace(title="招聘栏目", text="示例集团校园招聘", via="http")

        def candidates(self, page, source):
            return [("https://example.org/notice", "2027校园招聘预告")]

        async def document(self, url, source, title):
            return SimpleNamespace(
                title=title,
                text="2027校园招聘预告。报名方式另行通知，暂未公布报名入口。请关注后续公告。",
                published_at=None,
                attachments=[],
            )

        async def close(self):
            pass

    with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
        asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=candidate_id)))
    with db.session() as s:
        candidate = s.get(SourceCandidate, candidate_id)
        emit("application_not_announced_remains_readable", "readable", candidate.status)
        emit(
            "application_not_announced_is_not_found",
            "needs_review",
            candidate.detail_result["verification"]["application"],
        )
    db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
