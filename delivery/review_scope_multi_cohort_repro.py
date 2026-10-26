"""Legacy merge must isolate every conflicted cohort, preserving valid ones."""

import json
import tempfile

from sqlalchemy import select

from soe_radar.config import Settings
from soe_radar.coverage import coverage_report
from soe_radar.coverage_tasks import add_source_scope, recompute_scope
from soe_radar.db import initialize
from soe_radar.identity import merge_alias_employers
from soe_radar.models import Employer, SourceEmployerScope

results = []


def emit(probe, expected, actual):
    row = {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False))


with tempfile.TemporaryDirectory() as folder:
    cfg = Settings.model_validate(
        {
            "data_dir": folder,
            "bundled_catalogue": False,
            "sources": [{"id": "group", "name": "示例集团", "employer": "示例集团有限公司", "url": "https://example.org/jobs", "domains": ["example.org"]}],
        }
    )
    db = initialize(cfg)
    canonical, alias = "示例单位有限公司", "示例单位有限公司[示例单位别称]"
    with db.session() as s:
        target = Employer(name=canonical, org_type="soe", proof_url="https://example.org/about")
        s.add_all([target, Employer(name=alias)])
        s.flush()
        target_id = target.id
    base = dict(source_id="group", batch_scope="统一校招")
    add_source_scope(db, **base, employer_name=alias, evidence_url="https://example.org/old", quote="旧别名确认三届", cohorts=[2027, 2028, 2029], status="confirmed")
    scope_id = add_source_scope(db, **base, employer_name=canonical, evidence_url="https://example.org/new", quote="规范名确认三届", cohorts=[2027, 2028, 2029], status="confirmed")
    add_source_scope(db, **base, employer_name=canonical, evidence_url="https://example.org/new", quote="撤回2027和2028，2029保持确认", cohorts=[2027, 2028], status="needs_review")
    with db.session() as s:
        for row in s.scalars(select(SourceEmployerScope)):
            row.provenance = [{k: v for k, v in record.items() if k != "decided_at"} for record in row.provenance]
            recompute_scope(row)
        assert s.get(SourceEmployerScope, scope_id).cohorts == [2029]
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        emit("all_conflicting_legacy_cohorts_are_excluded", [2029], s.get(SourceEmployerScope, scope_id).cohorts)
    wrong = []
    for cohort in (2027, 2028):
        row = next(x for x in coverage_report(db, cfg, cohort=cohort)["items"] if x["id"] == target_id)
        if row["sources"]:
            wrong.append(cohort)
    emit("neither_conflicting_cohort_is_monitored", [], wrong)
    row = next(x for x in coverage_report(db, cfg, cohort=2029)["items"] if x["id"] == target_id)
    emit("nonconflicting_cohort_remains_monitored", ["group"], [x["id"] for x in row["sources"]])
    db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
