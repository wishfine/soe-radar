"""Upgrade and mixed-context probes; temporary DBs and mock pages only."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from soe_radar.config import Settings
from soe_radar.coverage_tasks import _verify_candidate, add_source_candidate, add_source_scope, recompute_scope
from soe_radar.db import initialize
from soe_radar.identity import merge_alias_employers
from soe_radar.models import Employer, SourceCandidate, SourceEmployerScope

results = []


def emit(probe, expected, actual):
    row = {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False))


def setup(path):
    cfg = Settings.model_validate({"data_dir": str(path), "bundled_catalogue": False})
    return cfg, initialize(cfg)


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    cfg, db = setup(root / "legacy")
    canonical, alias = "示例单位有限公司", "示例单位有限公司[示例单位别称]"
    with db.session() as s:
        s.add_all([Employer(name=canonical), Employer(name=alias)])
    base = dict(source_id="group", cohorts=[2027], batch_scope="2027秋招")
    add_source_scope(db, **base, employer_name=alias, evidence_url="https://example.org/old", quote="旧别名确认", status="confirmed")
    scope_id = add_source_scope(db, **base, employer_name=canonical, evidence_url="https://example.org/new", quote="规范名确认", status="confirmed")
    add_source_scope(db, **base, employer_name=canonical, evidence_url="https://example.org/new", quote="较晚复核撤回", status="needs_review")
    with db.session() as s:
        # This is the real 0011/pre-d951552 record shape: verified_at existed,
        # but decided_at was absent on both confirmations and withdrawals.
        for row in s.scalars(select(SourceEmployerScope)):
            row.provenance = [{k: v for k, v in record.items() if k != "decided_at"} for record in row.provenance]
            recompute_scope(row)
        assert s.get(SourceEmployerScope, scope_id).status == "needs_review"
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        emit("legacy_withdrawal_survives_alias_merge", "needs_review", s.get(SourceEmployerScope, scope_id).status)
    db.engine.dispose()

    cases = [
        ("unpublished_application", "2027校园招聘岗位介绍。报名入口尚未公布；请访问 https://example.org/jobs 查看招聘岗位；网申开放时间另行通知。", "readable"),
        ("mixed_emails", "咨询邮箱：help@example.org，仅供咨询，不接收简历。请将简历发送至 hr@example.org 完成报名。", "verified"),
    ]
    for case, document_text, expected in cases:
        cfg, db = setup(root / case)
        with db.session() as s:
            employer = Employer(name=canonical)
            s.add(employer)
            s.flush()
            employer_id = employer.id
        cid = add_source_candidate(db, employer_id=employer_id, employer_name=canonical, url="https://example.org/jobs", identity_evidence="https://example.org/about", discovery_method="official_site_nav")

        class FakeFetcher:
            def __init__(self, cfg):
                pass

            async def page(self, url, source):
                return SimpleNamespace(title="招聘", text="招聘栏目列表", via="http")

            def candidates(self, page, source):
                return [("https://example.org/notice", "2027校园招聘公告")]

            async def document(self, url, source, title):
                return SimpleNamespace(title=title, text=document_text, published_at=None, attachments=[])

            async def close(self):
                pass

        with patch("soe_radar.coverage_tasks.Fetcher", FakeFetcher):
            asyncio.run(_verify_candidate(db, cfg, SimpleNamespace(scope_id=cid)))
        with db.session() as s:
            row = s.get(SourceCandidate, cid)
            emit(case, expected, row.status)
        db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
