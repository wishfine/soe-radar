"""Offline T2 follow-up: incorrect existing bindings and unresolved export isolation."""

import json
import tempfile
from pathlib import Path

from soe_radar.config import Settings
from soe_radar.coverage import repair_registry_snapshots, save_registry
from soe_radar.db import initialize
from soe_radar.delivery import export_delivery
from soe_radar.models import Employer, RegistrySnapshot

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
                    "name": "示例",
                    "category": "local",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                }
            ],
        }
    )
    db = initialize(cfg)
    spec = cfg.registries[0]
    first = "<table><tr><td>1</td><td>示例一有限公司</td></tr></table>"
    second = "<table><tr><td>2</td><td>示例二有限公司</td></tr></table>"
    saved = save_registry(
        db,
        cfg,
        spec,
        first + second,
        pages=[
            {"url": spec.url, "raw": first.encode()},
            {"url": spec.url + "?page=2", "raw": second.encode()},
        ],
    )
    with db.session() as session:
        snapshot = session.get(RegistrySnapshot, saved["id"])
        entries = [dict(x) for x in snapshot.entries]
        page = snapshot.provenance["pages"][0]
        bad = next(x for x in entries if x["name"] == "示例二有限公司")
        bad.update(page_url=page["url"], page_sha256=page["sha256"], page_path=page["path"])
        snapshot.entries = entries
    repair_registry_snapshots(db, apply=True)
    with db.session() as session:
        snapshot = session.get(RegistrySnapshot, saved["id"])
        row = next(x for x in snapshot.entries if x["name"] == "示例二有限公司")
        emit("known_hash_but_wrong_enterprise_page", spec.url + "?page=2", row["page_url"])
        entries = [dict(x) for x in snapshot.entries]
        bad = next(x for x in entries if x["name"] == "示例二有限公司")
        bad.update(
            name="未能定位有限公司",
            source_name="未能定位有限公司",
            page_url="https://example.gov.cn/missing",
            page_sha256="missing-hash",
            page_path="registries/missing.html",
        )
        snapshot.entries = entries
        session.add(Employer(name="未能定位有限公司", proof_url=spec.url))
    repair_registry_snapshots(db, apply=True)
    output = Path(folder) / "delivery"
    export_delivery(db, cfg, output)
    rows = [json.loads(line) for line in (output / "employers.jsonl").read_text().splitlines()]
    row = next(x for x in rows if x["原始名称"] == "未能定位有限公司")
    emit(
        "unresolved_binding_does_not_export_guessed_evidence",
        {"证据页URL": "", "证据路径": "", "证据SHA256": ""},
        {key: row[key] for key in ["证据页URL", "证据路径", "证据SHA256"]},
    )
    db.engine.dispose()

raise SystemExit(0 if all(row["passes"] for row in results) else 1)
