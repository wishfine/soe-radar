import pytest
from sqlalchemy import select

from soe_radar.config import Settings
from soe_radar.db import initialize
from soe_radar.models import Employer, SourceState


def registry_settings(tmp_path):
    return Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "central",
                    "name": "示例官方底册",
                    "category": "central",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                }
            ],
            "sources": [
                {
                    "id": "parent",
                    "name": "集团",
                    "employer": "示例集团有限公司",
                    "url": "https://example.org/jobs",
                    "domains": ["example.org"],
                }
            ],
        }
    )


def test_registry_model_does_not_silently_discard_the_coverage_universe(tmp_path):
    cfg = registry_settings(tmp_path)
    assert len(getattr(cfg, "registries", [])) == 1


def test_registry_versions_keep_removed_enterprises_and_failed_refresh(tmp_path):
    from soe_radar.coverage import coverage_report, registry_failure, save_registry

    cfg = registry_settings(tmp_path)
    db = initialize(cfg)
    spec = cfg.registries[0]
    html = "<table><tr><td>1</td><td>示例集团有限公司</td></tr><tr><td>2</td><td>独立子公司有限公司</td></tr></table>"
    first = save_registry(db, cfg, spec, html)
    assert save_registry(db, cfg, spec, html)["id"] == first["id"]
    save_registry(db, cfg, spec, html.replace("<tr><td>2</td><td>独立子公司有限公司</td></tr>", ""))
    registry_failure(db, spec.id, "网络异常")
    report = coverage_report(db, cfg)
    assert report["registries"][0]["count"] == 1
    assert report["registries"][0]["status"] == "error"
    assert report["national_total"] is None
    with db.session() as s:
        assert s.scalar(select(Employer).where(Employer.name == "独立子公司有限公司"))


def test_parent_monitoring_never_counts_as_subsidiary_coverage(tmp_path):
    from soe_radar.coverage import coverage_report, save_registry

    cfg = registry_settings(tmp_path)
    db = initialize(cfg)
    save_registry(
        db,
        cfg,
        cfg.registries[0],
        "<table><tr><td>1</td><td>示例集团有限公司</td></tr><tr><td>2</td><td>独立子公司有限公司</td></tr></table>",
    )
    with db.session() as s:
        s.get(SourceState, "parent").status = "error"
    rows = {r["name"]: r for r in coverage_report(db, cfg)["items"]}
    assert rows["示例集团有限公司"]["status"] == "blocked"
    assert rows["独立子公司有限公司"]["status"] == "missing_entry"
    assert rows["示例集团有限公司"]["campus_count"] == 0


def test_registry_error_page_and_ambiguous_names_are_not_a_valid_list(tmp_path):
    from soe_radar.coverage import save_registry

    cfg = registry_settings(tmp_path)
    db = initialize(cfg)
    with pytest.raises(ValueError):
        save_registry(db, cfg, cfg.registries[0], "<h1>Access denied</h1>")


def test_manual_csv_retains_provenance_and_cannot_claim_national_completeness(tmp_path):
    from soe_radar.coverage import coverage_report, import_registry_csv

    cfg = registry_settings(tmp_path)
    db = initialize(cfg)
    spec = cfg.registries[0]
    data = "企业名称,官网地址\n示例集团有限公司,https://example.org\n".encode()
    with pytest.raises(ValueError, match="确认"):
        import_registry_csv(db, cfg, spec, data, spec.url)
    with pytest.raises(ValueError, match="官方域名"):
        import_registry_csv(db, cfg, spec, data, "https://evil.example/list", confirmed=True)
    result = import_registry_csv(db, cfg, spec, data, spec.url, confirmed=True)
    report = coverage_report(db, cfg)
    assert result["count"] == 1
    assert report["registries"][0]["status"] == "imported"
    assert report["national_total"] is None
    assert report["summary"]["registry_entities"] == 1


def test_orphan_parser_guesses_are_not_counted_as_companies(tmp_path):
    from soe_radar.coverage import coverage_report

    cfg = registry_settings(tmp_path)
    db = initialize(cfg)
    with db.session() as s:
        s.add(Employer(name="示例集团总行社会公开招聘"))
    assert "示例集团总行社会公开招聘" not in [r["name"] for r in coverage_report(db, cfg)["items"]]


def test_two_column_and_multi_page_registry_parsing(tmp_path):
    from soe_radar.config import RegistryConfig
    from soe_radar.coverage import coverage_report, parse_registry, save_registry

    spec = RegistryConfig(
        id="pairs",
        name="双列名录",
        category="central",
        url="https://example.gov.cn/list",
        domains=["example.gov.cn"],
        adapter="table",
        min_entries=3,
    )
    html = (
        "<table><tr><td>序号</td><td>企业名称</td><td></td><td>序号</td><td>企业名称</td></tr>"
        "<tr><td>1</td><td>示例一集团有限公司</td><td></td><td>3</td><td>示例三有限责任公司</td></tr>"
        "<tr><td>2</td><td>示例二集团有限公司</td><td></td><td></td><td></td></tr></table>"
    )
    entries = parse_registry(html, spec)
    assert [row["name"] for row in entries] == [
        "示例一集团有限公司",
        "示例三有限责任公司",
        "示例二集团有限公司",
    ]

    cfg = Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [spec.model_dump()],
        }
    )
    db = initialize(cfg)
    pages = [
        {"url": spec.url, "raw": html.encode()},
        {
            "url": "https://example.gov.cn/list_1",
            "raw": "<table><tr><td>4</td><td>示例四有限公司</td></tr></table>".encode(),
        },
    ]
    save_registry(db, cfg, cfg.registries[0], "\n".join(p["raw"].decode() for p in pages), pages=pages)
    report = coverage_report(db, cfg)
    row = report["registries"][0]
    assert row["count"] == 4
    assert row["provenance"]["pagination_complete"] is True
    assert len(row["provenance"]["pages"]) == 2


def test_text_directory_keeps_aliases_and_separates_profile_links(tmp_path):
    from soe_radar.config import RegistryConfig
    from soe_radar.coverage import parse_registry

    spec = RegistryConfig(
        id="text",
        name="文本名录",
        category="local",
        url="https://example.gov.cn/list",
        domains=["example.gov.cn"],
        adapter="text",
        entry_selector=".mainContent",
        min_entries=2,
    )
    html = (
        '<div class="mainContent">监管企业名录 示例一集团有限公司 '
        "示例二集团有限责任公司（示例二有限公司）</div>"
    )
    assert [row["name"] for row in parse_registry(html, spec)] == [
        "示例一集团有限公司",
        "示例二集团有限责任公司（示例二有限公司）",
    ]

    links = RegistryConfig(
        id="links",
        name="链接名录",
        category="local",
        url="https://example.gov.cn/list",
        domains=["example.gov.cn"],
        adapter="links",
        entry_selector="#companies a",
        min_entries=2,
    )
    html = (
        '<div id="companies"><a href="https://one.example/">示例一集团有限公司</a>'
        '<a href="/profile">示例二有限责任公司</a></div>'
    )
    rows = {row["name"]: row for row in parse_registry(html, links)}
    assert rows["示例一集团有限公司"]["official_url"] == "https://one.example/"
    assert rows["示例二有限责任公司"]["official_url"] == ""
    assert rows["示例二有限责任公司"]["profile_url"].endswith("/profile")
