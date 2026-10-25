import json

import pytest

from soe_radar.config import Settings
from soe_radar.coverage import save_registry
from soe_radar.db import initialize
from soe_radar.delivery import export_delivery
from soe_radar.relationships import add_relationship


def delivery_settings(tmp_path):
    return Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "central",
                    "name": "示例官方底册",
                    "category": "central_sasac",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                    "min_entries": 1,
                }
            ],
            "sources": [
                {
                    "id": "parent",
                    "name": "示例集团",
                    "employer": "示例集团有限公司",
                    "url": "https://example.org/jobs",
                    "domains": ["example.org"],
                }
            ],
        }
    )


def test_delivery_export_writes_evidence_and_separates_relationships(tmp_path):
    cfg = delivery_settings(tmp_path)
    db = initialize(cfg)
    save_registry(
        db,
        cfg,
        cfg.registries[0],
        "<table><tr><td>1</td><td>示例子公司有限公司</td></tr></table>",
    )
    add_relationship(
        db,
        "示例集团有限公司",
        "示例子公司有限公司",
        evidence_url="https://example.org/group",
        quote="示例子公司有限公司为示例集团有限公司所属企业。",
        evidence_date="2026-01-02",
    )
    summary = export_delivery(db, cfg, tmp_path / "delivery")
    assert summary["registry_entries"] == 1
    assert summary["relationships_confirmed"] == 1
    assert (tmp_path / "delivery" / "registry_manifest.csv").is_file()
    assert (tmp_path / "delivery" / "employers.jsonl").is_file()
    assert (tmp_path / "delivery" / "relationships.jsonl").is_file()
    assert (tmp_path / "delivery" / "recruitment_sources.csv").is_file()
    assert (tmp_path / "delivery" / "gaps.csv").is_file()
    assert (tmp_path / "delivery" / "evidence" / "SHA256SUMS").is_file()
    relationships = [
        json.loads(line) for line in (tmp_path / "delivery" / "relationships.jsonl").read_text().splitlines()
    ]
    assert relationships[0]["母公司"] == "示例集团有限公司"
    assert relationships[0]["原文片段"].startswith("示例子公司")
    employers = {
        row["原始名称"]: row
        for row in map(json.loads, (tmp_path / "delivery" / "employers.jsonl").read_text().splitlines())
    }
    assert employers["示例子公司有限公司"]["主管口径"] == "central_sasac"


def test_relationship_requires_evidence_and_rejects_cycles(tmp_path):
    cfg = delivery_settings(tmp_path)
    db = initialize(cfg)
    save_registry(
        db,
        cfg,
        cfg.registries[0],
        "<table><tr><td>1</td><td>示例子公司有限公司</td></tr></table>",
    )
    with pytest.raises(ValueError, match="证据"):
        add_relationship(
            db,
            "示例集团有限公司",
            "示例子公司有限公司",
            evidence_url="",
            quote="",
            evidence_date="",
        )
    add_relationship(
        db,
        "示例集团有限公司",
        "示例子公司有限公司",
        evidence_url="https://example.org/group",
        quote="所属企业",
        evidence_date="2026-01-02",
    )
    with pytest.raises(ValueError, match="循环"):
        add_relationship(
            db,
            "示例子公司有限公司",
            "示例集团有限公司",
            evidence_url="https://example.org/group",
            quote="错误方向",
            evidence_date="2026-01-02",
        )


def test_export_binds_each_employer_to_its_own_page(tmp_path):
    cfg = delivery_settings(tmp_path).model_copy(
        update={
            "registries": [
                delivery_settings(tmp_path)
                .registries[0]
                .model_copy(
                    update={
                        "page_urls": ["https://example.gov.cn/list_1"],
                        "min_entries": 1,
                    }
                )
            ]
        }
    )
    db = initialize(cfg)
    spec = cfg.registries[0]
    one = "<table><tr><td>1</td><td>示例一有限公司</td></tr></table>"
    two = "<table><tr><td>2</td><td>示例二有限公司</td></tr></table>"
    save_registry(
        db,
        cfg,
        spec,
        one + two,
        pages=[
            {"url": spec.url, "raw": one.encode()},
            {"url": spec.page_urls[0], "raw": two.encode()},
        ],
    )
    export_delivery(db, cfg, tmp_path / "delivery")
    rows = {
        row["原始名称"]: row
        for row in map(json.loads, (tmp_path / "delivery" / "employers.jsonl").read_text().splitlines())
    }
    for name in ("示例一有限公司", "示例二有限公司"):
        path = tmp_path / "delivery" / rows[name]["证据路径"]
        assert name in path.read_text(encoding="utf-8")


def test_needs_review_relationship_is_not_in_confirmed_file_and_confirmed_child_gets_entry_gap(tmp_path):
    cfg = delivery_settings(tmp_path)
    db = initialize(cfg)
    save_registry(
        db,
        cfg,
        cfg.registries[0],
        "<table><tr><td>1</td><td>示例子公司有限公司</td></tr></table>",
    )
    add_relationship(
        db,
        "示例集团有限公司",
        "示例子公司有限公司",
        evidence_url="https://example.org/group",
        quote="所属企业",
        evidence_date="2026-01-02",
        status="needs_review",
    )
    export_delivery(db, cfg, tmp_path / "delivery")
    assert (tmp_path / "delivery" / "relationships.jsonl").read_text().strip() == ""
    gaps = (tmp_path / "delivery" / "gaps.csv").read_text(encoding="utf-8-sig")
    assert "关系待复核" in gaps
    add_relationship(
        db,
        "示例集团有限公司",
        "示例子公司有限公司",
        evidence_url="https://example.org/group",
        quote="所属企业",
        evidence_date="2026-01-02",
        status="confirmed",
    )
    export_delivery(db, cfg, tmp_path / "delivery")
    gaps = (tmp_path / "delivery" / "gaps.csv").read_text(encoding="utf-8-sig")
    assert "入口未找到" in gaps
    assert "示例子公司有限公司" in gaps


def test_unresolved_page_binding_exports_blank_evidence_and_gap(tmp_path):
    from soe_radar.coverage import repair_registry_snapshots
    from soe_radar.models import Employer, RegistrySnapshot

    cfg = delivery_settings(tmp_path)
    db = initialize(cfg)
    saved = save_registry(
        db,
        cfg,
        cfg.registries[0],
        "<table><tr><td>1</td><td>示例一有限公司</td></tr></table>",
    )
    with db.session() as s:
        row = s.get(RegistrySnapshot, saved["id"])
        entries = [dict(entry) for entry in row.entries]
        entries[0].update(
            name="未能定位有限公司",
            source_name="未能定位有限公司",
            page_url="https://example.gov.cn/missing",
            page_sha256="missing-hash",
            page_path="registries/missing.html",
        )
        row.entries = entries
        s.add(Employer(name="未能定位有限公司", proof_url=cfg.registries[0].url))
    repair_registry_snapshots(db, apply=True)
    export_delivery(db, cfg, tmp_path / "delivery")
    rows = {
        row["原始名称"]: row
        for row in map(json.loads, (tmp_path / "delivery" / "employers.jsonl").read_text().splitlines())
    }
    row = rows["未能定位有限公司"]
    assert row["证据页URL"] == "" and row["证据路径"] == "" and row["证据SHA256"] == ""
    assert row["页绑定状态"] == "needs_review"
    gaps = (tmp_path / "delivery" / "gaps.csv").read_text(encoding="utf-8-sig")
    assert "证据页待复核" in gaps
