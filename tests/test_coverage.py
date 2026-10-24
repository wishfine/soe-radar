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
    rows = {row["name"]: row for row in parse_registry(html, spec)}
    assert set(rows) == {"示例一集团有限公司", "示例二集团有限责任公司"}
    assert "示例二有限公司" in rows["示例二集团有限责任公司"]["aliases"]

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


def test_partial_page_failure_never_replaces_known_membership(tmp_path):
    import asyncio

    import httpx

    from soe_radar.coverage import coverage_report, refresh_registries, save_registry
    from soe_radar.fetching import FetchFailure
    from soe_radar.models import RegistryState

    cfg = Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "multi",
                    "name": "分页名录",
                    "category": "local",
                    "url": "https://example.gov.cn/list",
                    "page_urls": ["https://example.gov.cn/list_1"],
                    "domains": ["example.gov.cn"],
                    "min_entries": 1,
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
            {"url": spec.page_urls[0], "raw": second.encode()},
        ],
    )

    class MockFetcher:
        network_attempts = []

        async def get(self, url, *args, **kwargs):
            if url.endswith("_1"):
                raise FetchFailure("HTTP 503")
            return httpx.Response(200, text=first, request=httpx.Request("GET", url))

    asyncio.run(refresh_registries(db, cfg, fetcher=MockFetcher()))
    report = coverage_report(db, cfg)
    assert report["summary"]["registry_entities"] == 2
    with db.session() as s:
        state = s.get(RegistryState, spec.id)
        assert state.active_snapshot_id == saved["id"]
        assert state.status == "partial"


def test_bracket_alias_reuses_existing_employer_and_preserves_source_name(tmp_path):
    from sqlalchemy import select

    from soe_radar.coverage import save_registry
    from soe_radar.models import Employer

    cfg = Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "alias",
                    "name": "别名名录",
                    "category": "central",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                    "min_entries": 1,
                }
            ],
        }
    )
    db = initialize(cfg)
    with db.session() as s:
        s.add(Employer(name="中国旅游集团有限公司", aliases=["香港中旅（集团）有限公司"]))
    save_registry(
        db,
        cfg,
        cfg.registries[0],
        "<table><tr><td>1</td><td>中国旅游集团有限公司[香港中旅（集团）有限公司]</td></tr></table>",
    )
    with db.session() as s:
        rows = list(s.scalars(select(Employer).where(Employer.name.like("中国旅游集团有限公司%"))))
    assert len(rows) == 1
    assert "中国旅游集团有限公司[香港中旅（集团）有限公司]" in rows[0].aliases


def test_text_adapter_merges_links_and_plain_text_entries(tmp_path):
    from soe_radar.config import RegistryConfig
    from soe_radar.coverage import parse_registry

    spec = RegistryConfig(
        id="mixed",
        name="混合名录",
        category="local",
        url="https://example.gov.cn/list",
        domains=["example.gov.cn"],
        adapter="text",
        entry_selector="#list",
        min_entries=1,
    )
    rows = parse_registry(
        '<div id="list"><a href="https://one.example">示例一集团有限公司</a>'
        "<p>示例二集团有限公司</p><p>示例三集团有限公司</p></div>",
        spec,
    )
    assert [row["name"] for row in rows] == [
        "示例一集团有限公司",
        "示例三集团有限公司",
        "示例二集团有限公司",
    ]


def test_single_page_and_csv_content_hash_is_the_file_hash(tmp_path):
    import hashlib

    from soe_radar.coverage import import_registry_csv, save_registry
    from soe_radar.models import RegistrySnapshot

    cfg = Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "hash",
                    "name": "哈希名录",
                    "category": "local",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                    "min_entries": 1,
                }
            ],
        }
    )
    db = initialize(cfg)
    spec = cfg.registries[0]
    saved = save_registry(db, cfg, spec, "<table><tr><td>1</td><td>示例一有限公司</td></tr></table>")
    with db.session() as s:
        page = s.get(RegistrySnapshot, saved["id"]).provenance["pages"][0]
        assert page["sha256"] == hashlib.sha256((cfg.data_dir / page["path"]).read_bytes()).hexdigest()
    csv_saved = import_registry_csv(
        db,
        cfg,
        spec,
        "企业名称\n示例二有限公司\n".encode(),
        "https://example.gov.cn/list",
        confirmed=True,
    )
    with db.session() as s:
        page = s.get(RegistrySnapshot, csv_saved["id"]).provenance["pages"][0]
        assert page["sha256"] == hashlib.sha256((cfg.data_dir / page["path"]).read_bytes()).hexdigest()


def test_merge_alias_employers_preserves_opportunity_and_personal_state(tmp_path):
    from sqlalchemy import select

    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import Activity, Batch, Employer, Opportunity, PersonalState

    cfg = Settings.model_validate({"data_dir": str(tmp_path / "data")})
    db = initialize(cfg)
    with db.session() as s:
        target = Employer(name="中国旅游集团有限公司")
        duplicate = Employer(name="中国旅游集团有限公司[香港中旅（集团）有限公司]")
        s.add_all([target, duplicate])
        s.flush()
        activity = Activity(employer_id=duplicate.id, identity="test-activity", title="示例招聘")
        s.add(activity)
        s.flush()
        batch = Batch(activity_id=activity.id, name="正式批")
        s.add(batch)
        s.flush()
        opportunity = Opportunity(
            activity_id=activity.id,
            batch_id=batch.id,
            employer_id=duplicate.id,
            source_id="test",
            official_url="https://example.org/job",
            title="示例招聘",
        )
        s.add(opportunity)
        s.flush()
        s.add(PersonalState(opportunity_id=opportunity.id, saved=True, notes="保留"))
        opportunity_id = opportunity.id
        target_id = target.id
    plans = merge_alias_employers(db, apply=True)
    assert len(plans) == 1
    with db.session() as s:
        employers = list(s.scalars(select(Employer).where(Employer.name.like("中国旅游集团有限公司%"))))
        assert len(employers) == 1
        opportunity = s.get(Opportunity, opportunity_id)
        assert opportunity.employer_id == target_id
        state = s.get(PersonalState, opportunity_id)
        assert state.saved is True and state.notes == "保留"


def test_changed_nonfinal_page_creates_new_snapshot_with_all_entries(tmp_path):
    from soe_radar.coverage import coverage_report, save_registry
    from soe_radar.models import RegistrySnapshot

    cfg = Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "multi-change",
                    "name": "多页变更",
                    "category": "local",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                    "min_entries": 1,
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
    assert before["id"] != after["id"]
    assert after["count"] == 3
    assert coverage_report(db, cfg)["summary"]["registry_entities"] == 3
    with db.session() as s:
        assert len(s.get(RegistrySnapshot, after["id"]).entries) == 3


def test_merge_alias_employers_preserves_confirmed_parent_pointer(tmp_path):
    from sqlalchemy import select

    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import Employer, Relationship

    cfg = Settings.model_validate({"data_dir": str(tmp_path / "data")})
    db = initialize(cfg)
    with db.session() as s:
        parent = Employer(name="示例母公司有限公司")
        target = Employer(name="示例子公司有限公司")
        duplicate = Employer(name="示例子公司有限公司[旧名有限公司]")
        s.add_all([parent, target, duplicate])
        s.flush()
        duplicate.parent_id = parent.id
        s.add(
            Relationship(
                parent_id=parent.id,
                child_id=duplicate.id,
                relation_type="subsidiary",
                evidence_url="https://example.org/confirmed",
                quote="母公司持有子公司全部股权",
                evidence_date="2026-01-01",
                status="confirmed",
            )
        )
        target_id, parent_id = target.id, parent.id
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        target = s.get(Employer, target_id)
        assert target.parent_id == parent_id
        edges = list(s.scalars(select(Relationship).where(Relationship.child_id == target_id)))
        assert len(edges) == 1 and edges[0].status == "confirmed"


def test_merge_same_parent_relationships_keeps_audit_without_fk_error(tmp_path):
    from sqlalchemy import select

    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import Employer, Relationship

    cfg = Settings.model_validate({"data_dir": str(tmp_path / "data")})
    db = initialize(cfg)
    with db.session() as s:
        parent = Employer(name="示例母公司有限公司")
        target = Employer(name="示例子公司有限公司")
        duplicate = Employer(name="示例子公司有限公司[旧名有限公司]")
        s.add_all([parent, target, duplicate])
        s.flush()
        target.parent_id = parent.id
        duplicate.parent_id = parent.id
        for child, url, quote in [
            (target, "https://example.org/a", "证据甲"),
            (duplicate, "https://example.org/b", "证据乙"),
        ]:
            s.add(
                Relationship(
                    parent_id=parent.id,
                    child_id=child.id,
                    relation_type="subsidiary",
                    evidence_url=url,
                    quote=quote,
                    evidence_date="2026-01-01",
                    status="confirmed",
                )
            )
        target_id, parent_id = target.id, parent.id
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        target = s.get(Employer, target_id)
        assert target.parent_id == parent_id
        edges = list(s.scalars(select(Relationship).where(Relationship.child_id == target_id)))
        assert len(edges) == 1 and edges[0].status == "confirmed"
        assert {item["quote"] for item in edges[0].provenance} == {"证据甲", "证据乙"}


def test_merge_different_confirmed_parents_keeps_one_and_marks_other_review(tmp_path):
    from sqlalchemy import select

    from soe_radar.identity import merge_alias_employers
    from soe_radar.models import Employer, Relationship

    cfg = Settings.model_validate({"data_dir": str(tmp_path / "data")})
    db = initialize(cfg)
    with db.session() as s:
        first_parent = Employer(name="示例母公司甲有限公司")
        second_parent = Employer(name="示例母公司乙有限公司")
        target = Employer(name="示例子公司有限公司")
        duplicate = Employer(name="示例子公司有限公司[旧名有限公司]")
        s.add_all([first_parent, second_parent, target, duplicate])
        s.flush()
        target.parent_id = first_parent.id
        duplicate.parent_id = second_parent.id
        for parent, child in [(first_parent, target), (second_parent, duplicate)]:
            s.add(
                Relationship(
                    parent_id=parent.id,
                    child_id=child.id,
                    relation_type="subsidiary",
                    evidence_url="https://example.org/" + parent.name,
                    quote="证据",
                    evidence_date="2026-01-01",
                    status="confirmed",
                )
            )
        target_id, first_id = target.id, first_parent.id
    merge_alias_employers(db, apply=True)
    with db.session() as s:
        target = s.get(Employer, target_id)
        assert target.parent_id == first_id
        edges = list(s.scalars(select(Relationship).where(Relationship.child_id == target_id)))
        statuses = {edge.parent_id: edge.status for edge in edges}
        assert statuses[first_id] == "confirmed"
        assert any(status == "needs_review" for status in statuses.values())


def test_explicit_relationship_downgrade_and_reconfirm_round_trip(tmp_path):
    from sqlalchemy import select

    from soe_radar.models import Employer, Relationship
    from soe_radar.relationships import add_relationship

    db = initialize(Settings.model_validate({"data_dir": str(tmp_path / "data")}))
    with db.session() as s:
        s.add_all([Employer(name="示例母公司有限公司"), Employer(name="示例子公司有限公司")])
    evidence = {
        "evidence_url": "https://example.org/about",
        "quote": "母公司全资持有子公司",
        "evidence_date": "2026-01-01",
    }
    add_relationship(db, "示例母公司有限公司", "示例子公司有限公司", **evidence)
    add_relationship(db, "示例母公司有限公司", "示例子公司有限公司", status="needs_review", **evidence)
    with db.session() as s:
        relation = s.scalar(select(Relationship))
        child = s.get(Employer, relation.child_id)
        assert relation.status == "needs_review"
        assert child.parent_id is None
        assert len(relation.provenance) == 2
    add_relationship(db, "示例母公司有限公司", "示例子公司有限公司", **evidence)
    with db.session() as s:
        relation = s.scalar(select(Relationship))
        child = s.get(Employer, relation.child_id)
        assert relation.status == "confirmed"
        assert child.parent_id == relation.parent_id
    with pytest.raises(ValueError, match="状态"):
        add_relationship(
            db,
            "示例母公司有限公司",
            "示例子公司有限公司",
            status="unknown",
            **evidence,
        )


def test_snapshot_repair_persists_page_bindings_and_is_idempotent(tmp_path):
    from soe_radar.coverage import repair_registry_snapshots, save_registry
    from soe_radar.models import RegistrySnapshot

    cfg = Settings.model_validate(
        {
            "data_dir": str(tmp_path / "data"),
            "registries": [
                {
                    "id": "repair",
                    "name": "修复",
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
    with db.session() as s:
        row = s.get(RegistrySnapshot, saved["id"])
        entries = [dict(entry) for entry in row.entries]
        entries[0]["page_path"] = "registries/broken.html"
        row.entries = entries
        row.digest = "legacy-digest"
    repair_registry_snapshots(db, apply=True)
    with db.session() as s:
        row = s.get(RegistrySnapshot, saved["id"])
        assert row.entries[0]["page_path"] == row.provenance["pages"][0]["path"]
        assert row.digest != "legacy-digest"
    assert repair_registry_snapshots(db, apply=False) == []
