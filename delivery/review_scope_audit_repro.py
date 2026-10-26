"""Offline review: current scope status and application evidence roles.

Only temporary databases and mock pages. Exit 1 indicates unmet guarantees.
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
    cfg, db = setup(root / "downgrade")
    child_name = "示例子公司有限公司"
    with db.session() as s:
        child = Employer(name=child_name, org_type="soe", proof_url="https://example.org/about")
        s.add(child)
        s.flush()
        child_id = child.id
    values = dict(
        source_id="group",
        employer_name=child_name,
        evidence_url="https://example.org/2027",
        cohorts=[2027],
        batch_scope="2027秋招",
    )
    scope_id = add_source_scope(db, **values, quote="示例子公司参加集团2027统一校招", status="confirmed")
    child = next(x for x in coverage_report(db, cfg, cohort=2027)["items"] if x["id"] == child_id)
    assert [x["id"] for x in child["sources"]] == ["group"]
    assert scope_id == add_source_scope(
        db,
        **values,
        quote="复核发现原名单适用范围有误，撤回确认，等待人工复核",
        status="needs_review",
    )
    with db.session() as s:
        row = s.get(SourceEmployerScope, scope_id)
        emit("explicit_scope_downgrade_controls_current_status", "needs_review", row.status)
        assert any(x["status"] == "confirmed" for x in row.provenance)
    child = next(x for x in coverage_report(db, cfg, cohort=2027)["items"] if x["id"] == child_id)
    emit("downgraded_scope_is_excluded_from_monitoring", [], [x["id"] for x in child["sources"]])
    db.engine.dispose()

    texts = {
        "consultation_email": "示例集团2027校园招聘岗位信息。咨询邮箱：help@example.org（仅接受咨询，不接收简历）。岗位要求详述如下，欢迎了解企业及相关岗位。",
        "company_about_url": "示例集团2027校园招聘岗位介绍。集团概况：https://example.org/about。此页面展示岗位要求与公司介绍，没有提供申请路径。",
    }
    for case, document_text in texts.items():
        cfg, db = setup(root / case)
        with db.session() as s:
            employer = s.scalar(select(Employer))
        cid = add_source_candidate(
            db,
            employer_id=employer.id,
            employer_name=employer.name,
            url="https://example.org/jobs",
            identity_evidence="https://example.org/about",
            discovery_method="official_site_nav",
        )

        class FakeFetcher:
            def __init__(self, cfg):
                pass

            async def page(self, url, source):
                return SimpleNamespace(title="招聘栏目", text="校园招聘公告列表", via="http")

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
            emit(case + "_is_not_application_path", "needs_review", row.detail_result["verification"]["application"])
            emit(case + "_does_not_fully_verify_candidate", "readable", row.status)
        db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
