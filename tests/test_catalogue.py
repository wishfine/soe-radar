from sqlalchemy import select

from soe_radar.coverage import coverage_report, save_registry
from soe_radar.db import initialize
from soe_radar.models import Employer, RegistryState


def test_fresh_install_has_evidenced_enterprises_not_empty_province_slots(cfg):
    db = initialize(cfg)
    report = coverage_report(db, cfg)
    assert report["summary"]["registry_entities"] >= 200
    assert report["national_total"] is None
    central = next(r for r in report["registries"] if r["id"] == "central-sasac")
    assert central["count"] >= 99
    assert central["last_success"] is None
    assert central["provenance"]["acquisition"] == "official_index_excerpt"
    assert report["summary"]["with_entry"] < report["summary"]["registry_entities"]


def test_bundled_catalogue_never_overwrites_live_version_or_personal_review(cfg):
    db = initialize(cfg)
    spec = next(r for r in cfg.registries if r.id == "central-sasac").model_copy(update={"min_entries": 1})
    live = save_registry(db, cfg, spec, "<table><tr><td>1</td><td>合成示例有限公司</td></tr></table>")
    db.engine.dispose()
    db = initialize(cfg)
    with db.session() as s:
        assert s.get(RegistryState, spec.id).active_snapshot_id == live["id"]
        entity = s.scalar(select(Employer).where(Employer.name == "中国核工业集团有限公司"))
        assert entity.org_type == "unknown"


def test_link_directory_reader_keeps_full_entities_and_not_news_or_navigation(tmp_path):
    from soe_radar.config import RegistryConfig
    from soe_radar.coverage import parse_registry

    spec = RegistryConfig(
        id="local",
        name="目录",
        category="local",
        url="https://example.gov.cn/list",
        domains=["example.gov.cn"],
        adapter="links",
        entry_selector="#companies a",
        min_entries=2,
    )
    html = '<a>虚构公司</a><div id="companies"><a href="https://one.example">1.示例一集团有限公司</a><a href="/two">示例二有限责任公司</a><a href="/news">示例集团有限公司招聘公告</a><a href="/nav">监管企业</a></div>'
    assert [r["name"] for r in parse_registry(html, spec)] == ["示例一集团有限公司", "示例二有限责任公司"]


def test_default_catalogue_provenance_retains_scope_and_age(cfg):
    db = initialize(cfg)
    report = coverage_report(db, cfg)
    finance = next(r for r in report["registries"] if r["id"] == "central-finance")
    assert finance["provenance"]["published_at"] == "2021-02-20"
    assert finance["provenance"]["complete"] is False
    assert "历史" in finance["provenance"]["note"]
    assert len([r for r in cfg.registries if r.region not in ("全国", "北京", "天津", "上海", "重庆")]) >= 27


def test_bundle_is_idempotent_opt_out_and_survives_refresh_failure(cfg):
    from soe_radar.coverage import registry_failure
    from soe_radar.models import RegistrySnapshot

    db = initialize(cfg)
    first = coverage_report(db, cfg)
    with db.session() as s:
        snapshots = len(list(s.scalars(select(RegistrySnapshot))))
    registry_failure(db, "local-01", "网络受阻")
    db.engine.dispose()
    db = initialize(cfg)
    report = coverage_report(db, cfg)
    assert first["summary"]["registry_entities"] == report["summary"]["registry_entities"]
    row = next(r for r in report["registries"] if r["id"] == "local-01")
    assert row["status"] == "error" and row["last_error"] == "网络受阻"
    assert row["count"] == 15 and row["last_success"] is None
    with db.session() as s:
        assert len(list(s.scalars(select(RegistrySnapshot)))) == snapshots
    cfg.data_dir = cfg.data_dir.parent / "opt-out"
    cfg.bundled_catalogue = False
    assert coverage_report(initialize(cfg), cfg)["summary"]["registry_entities"] == 0


def test_links_reader_does_not_pick_up_unscoped_table_and_exclusions(cfg):
    from soe_radar.coverage import parse_registry

    spec = next(r for r in cfg.registries if r.id == "local-01").model_copy(
        update={
            "entry_selector": "#companies a",
            "min_entries": 1,
            "exclude_names": ["排除企业有限公司"],
        }
    )
    html = '<table><tr><td>1</td><td>无关企业有限公司</td></tr></table><div id="companies"><a>保留企业有限公司</a><a>排除企业有限公司</a></div>'
    assert [e["name"] for e in parse_registry(html, spec)] == ["保留企业有限公司"]


def test_catalogue_keeps_manually_imported_version_and_employer_fields(cfg):
    from soe_radar.coverage import import_registry_csv

    db = initialize(cfg)
    spec = next(r for r in cfg.registries if r.id == "local-01")
    saved = import_registry_csv(
        db,
        cfg,
        spec,
        "企业名称\n首钢集团有限公司\n".encode(),
        "https://gzw.beijing.gov.cn/manual",
        confirmed=True,
    )
    with db.session() as s:
        entity = s.scalar(select(Employer).where(Employer.name == "首钢集团有限公司"))
        entity.aliases = ["已人工确认的别名"]
        entity.proof_url = "https://gzw.beijing.gov.cn/human-proof"
        entity.verified_at = "2026-10-01T00:00:00+00:00"
    db.engine.dispose()
    db = initialize(cfg)
    with db.session() as s:
        assert s.get(RegistryState, spec.id).active_snapshot_id == saved["id"]
        entity = s.scalar(select(Employer).where(Employer.name == "首钢集团有限公司"))
        assert entity.aliases == ["已人工确认的别名"]
        assert entity.proof_url.endswith("/human-proof")
        assert entity.org_type == "soe" and entity.verified_at.startswith("2026-10-01")
