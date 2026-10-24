"""Offline second-review regressions; only temporary databases are written.

Run with: uv run python delivery/review_round2_repro.py
Exit 1 means at least one independently reproduced expectation failed.
"""

import json
import tempfile

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from soe_radar.config import Settings
from soe_radar.coverage import coverage_report, save_registry
from soe_radar.db import initialize
from soe_radar.identity import merge_alias_employers
from soe_radar.models import Employer, RegistrySnapshot, Relationship

results = []


def emit(probe, expected, actual):
    row = {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False))


with tempfile.TemporaryDirectory() as folder:
    cfg = Settings.model_validate(
        {
            "data_dir": folder,
            "registries": [
                {
                    "id": "demo",
                    "name": "多页名录",
                    "category": "local",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                }
            ],
        }
    )
    db = initialize(cfg)
    spec = cfg.registries[0]
    first = "<table><tr><td>1</td><td>前页企业有限公司</td></tr></table>"
    changed = first + "<table><tr><td>3</td><td>前页新增有限公司</td></tr></table>"
    second = "<table><tr><td>2</td><td>末页企业有限公司</td></tr></table>"

    def save(page_one):
        return save_registry(
            db,
            cfg,
            spec,
            page_one + second,
            pages=[
                {"url": spec.url, "raw": page_one.encode()},
                {"url": spec.url + "?page=2", "raw": second.encode()},
            ],
        )

    before = save(first)
    after = save(changed)
    with db.session() as session:
        snapshot = session.get(RegistrySnapshot, after["id"])
        actual = {
            "new_version": before["id"] != after["id"],
            "reported_count": after["count"],
            "active_count": coverage_report(db, cfg)["summary"]["registry_entities"],
            "stored_count": len(snapshot.entries),
        }
    emit(
        "changed_nonfinal_page_creates_updated_snapshot",
        {"new_version": True, "reported_count": 3, "active_count": 3, "stored_count": 3},
        actual,
    )
    db.engine.dispose()


def relationship(parent_id, child_id, status):
    return Relationship(
        parent_id=parent_id,
        child_id=child_id,
        relation_type="subsidiary",
        evidence_url="https://example.org/" + status,
        quote="示例母公司持有子公司全部股权",
        evidence_date="2026-01-01",
        status=status,
    )


for duplicate_edge in (False, True):
    with tempfile.TemporaryDirectory() as folder:
        db = initialize(Settings(data_dir=folder))
        with db.session() as session:
            parent = Employer(name="示例母公司有限公司")
            target = Employer(name="示例子公司有限公司")
            duplicate = Employer(name="示例子公司有限公司[旧名有限公司]")
            session.add_all([parent, target, duplicate])
            session.flush()
            duplicate.parent_id = parent.id
            session.add(relationship(parent.id, duplicate.id, "confirmed"))
            if duplicate_edge:
                session.add(relationship(parent.id, target.id, "needs_review"))
            target_id, parent_id = target.id, parent.id
        error = None
        try:
            merge_alias_employers(db, apply=True)
        except IntegrityError:
            error = "IntegrityError"
        with db.session() as session:
            target = session.get(Employer, target_id)
            edges = list(session.scalars(select(Relationship)))
            if duplicate_edge:
                emit("merge_existing_relationship_edges", "no IntegrityError", error or "no IntegrityError")
            else:
                emit(
                    "merge_preserves_confirmed_parent_pointer",
                    True,
                    target.parent_id == parent_id
                    and any(edge.child_id == target_id and edge.status == "confirmed" for edge in edges),
                )
        db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
