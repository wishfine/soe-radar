"""Offline review: explicit relationship downgrade and persistent snapshot repair.

Only temporary databases are written; a failed expectation exits 1.
"""

import json
import tempfile

from sqlalchemy import select

from soe_radar.config import Settings
from soe_radar.coverage import repair_registry_snapshots, save_registry
from soe_radar.db import initialize
from soe_radar.models import Employer, RegistrySnapshot, Relationship
from soe_radar.relationships import add_relationship

results = []


def emit(probe, expected, actual):
    row = {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False))


with tempfile.TemporaryDirectory() as folder:
    db = initialize(Settings(data_dir=folder))
    with db.session() as session:
        session.add_all([Employer(name="示例母公司有限公司"), Employer(name="示例子公司有限公司")])
    evidence = {
        "evidence_url": "https://example.org/about",
        "quote": "母公司全资持有子公司",
        "evidence_date": "2026-01-01",
    }
    add_relationship(db, "示例母公司有限公司", "示例子公司有限公司", **evidence)
    add_relationship(db, "示例母公司有限公司", "示例子公司有限公司", status="needs_review", **evidence)
    with db.session() as session:
        relation = session.scalar(select(Relationship))
        child = session.get(Employer, relation.child_id)
        emit(
            "explicit_relationship_downgrade",
            {"status": "needs_review", "parent_cleared": True},
            {"status": relation.status, "parent_cleared": child.parent_id is None},
        )
    db.engine.dispose()

with tempfile.TemporaryDirectory() as folder:
    cfg = Settings.model_validate(
        {
            "data_dir": folder,
            "registries": [
                {
                    "id": "demo",
                    "name": "示例",
                    "category": "local",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                }
            ],
        }
    )
    db = initialize(cfg)
    saved = save_registry(
        db, cfg, cfg.registries[0], "<table><tr><td>1</td><td>示例一有限公司</td></tr></table>"
    )
    with db.session() as session:
        row = session.get(RegistrySnapshot, saved["id"])
        entries = [dict(entry) for entry in row.entries]
        entries[0]["page_path"] = "registries/broken.html"
        row.entries = entries
        row.digest = "legacy-digest"
    repair_registry_snapshots(db, apply=True)
    with db.session() as session:
        row = session.get(RegistrySnapshot, saved["id"])
        emit(
            "repaired_page_binding_persists_after_commit",
            row.provenance["pages"][0]["path"],
            row.entries[0]["page_path"],
        )
    db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
