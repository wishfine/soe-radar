"""G1/G2 follow-up: merge chronology and actual application semantics.

No network or personal DB writes; exit 1 means an expected guarantee failed.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from soe_radar.config import Settings
from soe_radar.coverage import coverage_report
from soe_radar.coverage_tasks import _verify_candidate, add_source_candidate, add_source_scope
from soe_radar.db import initialize
from soe_radar.identity import merge_alias_employers
from soe_radar.models import Employer, SourceCandidate, SourceEmployerScope

results = []


def emit(probe, expected, actual):
    row = {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False))


def setup(path):
    cfg = Settings.model_validate(
        {
            "data_dir": str(path),
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
    return cfg, initialize(cfg)


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    cfg, db = setup(root / "merge-order")
    canonical, alias = "示例单位有限公司", "示例单位有限公司[示例单位别称]"
    with db.session() as s:
        target = Employer(name=canonical, org_type="soe", proof_url="https://example.org/about")
        s.add_all([target, Employer(name=alias)])
        s.flush()
        target_id = target.id
    base = dict(source_id="group", cohorts=[2027], batch_scope="2027秋招")
    # The alias's confirmation is older than the canonical entity's withdrawal.
    add_source_scope(
        db, **base, employer_name=alias, evidence_url="https://example.org/old",
        quote="旧别名资料确认参与集团招聘", status="confirmed",
    )
    scope_id = add_source_scope(
        db, **base, employer_name=canonical, evidence_url="https://example.org/new",
        quote="规范名确认参与集团招聘", status="confirmed",
    )
    add_source_scope(
        db, **base, employer_name=canonical, evidence_url="https://example.org/new",
        quote="最新复核：原参与范围有误，撤回确认", status="needs_review",
    )
    with db.session() as s:
        assert s.get(SourceEmployerScope, scope_id).status == "needs_review"
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        row = s.get(SourceEmployerScope, scope_id)
        emit("older_alias_confirmation_does_not_override_later_withdrawal", "needs_review", row.status)
    item = next(row for row in coverage_report(db, cfg, cohort=2027)["items"] if row["id"] == target_id)
    emit("merge_does_not_restore_withdrawn_monitoring", [], [x["id"] for x in item["sources"]])
    db.engine.dispose()

    cfg, db = setup(root / "partial")
    with db.session() as s:
        s.add(Employer(name=canonical, org_type="soe", proof_url="https://example.org/about"))
    shared = dict(source_id="group", employer_name=canonical, evidence_url="https://example.org/current", batch_scope="统一校招")
    scope_id = add_source_scope(db, **shared, quote="确认两届", cohorts=[2027, 2028], status="confirmed")
    add_source_scope(db, **shared, quote="撤回2027", cohorts=[2027], status="needs_review")
    with db.session() as s:
        row = s.get(SourceEmployerScope, scope_id)
        emit("partial_withdrawal_preserves_other_confirmed_cohort", [2028], row.cohorts)
        emit("partial_withdrawal_keeps_remaining_scope_confirmed", "confirmed", row.status)
    db.engine.dispose()

    cases = {
        "visit_about_page": "示例集团2027校园招聘岗位介绍。了解集团概况请访问 https://example.org/about。此处为企业介绍，未提供申请路径。",
        "do_not_send_resume": "示例集团2027校园招聘岗位介绍。请勿将简历发送至 help@example.org，该邮箱仅供咨询，不接收简历。本页面介绍岗位要求。",
    }
    for case, document_text in cases.items():
        cfg, db = setup(root / case)
        with db.session() as s:
            employer = s.scalar(select(Employer))
        cid = add_source_candidate(
            db, employer_id=employer.id, employer_name=employer.name,
            url="https://example.org/jobs", identity_evidence="https://example.org/about",
            discovery_method="official_site_nav",
        )

        class FakeFetcher:
            def __init__(self, cfg):
                pass

            async def page(self, url, source):
                return SimpleNamespace(title="招聘", text="招聘栏目列表", via="http")

            def candidates(self, page, source):
                return [("https://example.org/notice", "2027校园招聘岗位介绍")]

            async def document(self, url, source, title):
                return SimpleNamespace(title=title, text=document_text, published_at=None, attachments=[])

            async def close(self):
                pass

        with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
            asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
        with db.session() as s:
            row = s.get(SourceCandidate, cid)
            emit(case + "_does_not_verify_application_path", "readable", row.status)
        db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
