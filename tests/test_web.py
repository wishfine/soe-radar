from datetime import date

import pytest
from fastapi.testclient import TestClient

from soe_radar.extraction import extract_rules
from soe_radar.schemas import DocumentInput
from soe_radar.store import ingest
from soe_radar.web import create_app


@pytest.fixture
def client(cfg):
    app = create_app(cfg, scheduling=False)
    with TestClient(app) as client:
        yield client


def add_record(client, year=2035, status="auto"):
    app = client.app
    doc = DocumentInput(
        url=f"https://www.boc.cn/{year}/test",
        title=f"中国银行{year}届校园招聘公告",
        text=f"面向{year}届。工作地点北京、上海。报名截止：{year}年12月31日。",
        employer_hint="中国银行股份有限公司",
        published_at=date(year, 9, 1),
        source_id="boc",
    )
    extraction = extract_rules(doc)
    if status == "pending":
        extraction.review_reasons.append("人工标记待核验")
    return ingest(app.state.db, app.state.cfg, doc, extraction)


def auth(client):
    return {"X-Radar-Token": client.app.state.token}


def test_home_and_empty_install_work_without_model_credentials(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "SOE RADAR" in response.text
    assert client.get("/api/opportunities").json()["total"] == 0
    assert client.get("/api/health").json()["allow_paid"] is False


def test_coverage_gaps_are_visible_and_national_denominator_is_unknown(client):
    result = client.get("/api/coverage?status=missing_entry").json()
    assert result["national_total"] is None
    assert result["national_coverage_rate"] is None
    assert len(result["registries"]) >= 35
    assert len([r for r in result["registries"] if r["category"] == "local"]) == 31
    assert all(r["count"] is None for r in result["registries"])
    assert all(r["status"] == "missing_entry" for r in result["items"])
    assert client.post("/api/registries/central-sasac/refresh").status_code == 403
    assert client.get("/exports/coverage.csv").status_code == 200
    assert "企业名称" in client.get("/exports/coverage.csv").text


def test_coverage_pagination_and_cohort_count_exclude_past_announcements(client):
    add_record(client, year=2035)
    current = client.get("/api/coverage?cohort=2035&page_size=1").json()
    old = client.get("/api/coverage?cohort=2036").json()
    assert current["summary"]["with_campus"] == 1
    assert old["summary"]["with_campus"] == 0
    assert len(current["items"]) == 1


def test_registry_csv_import_requires_local_auth_and_explicit_provenance(client):
    url = "/api/registries/local-01/imports"
    files = {"file": ("registry.csv", "企业名称\n合成示例企业有限公司\n".encode(), "text/csv")}
    data = {"proof_url": "https://example.gov.cn/enterprises"}
    assert client.post(url, files=files, data=data).status_code == 403
    assert client.post(url, files=files, data=data, headers=auth(client)).status_code == 422
    data["confirmed"] = "on"
    response = client.post(url, files=files, data=data, headers=auth(client))
    assert response.status_code == 200
    result = client.get("/api/coverage?registry=local-01").json()
    assert result["total"] == 1
    assert result["items"][0]["status"] == "missing_entry"
    assert result["national_total"] is None
    registry = next(r for r in result["registries"] if r["id"] == "local-01")
    assert registry["status"] == "imported"
    assert registry["url"] == data["proof_url"]
    assert not registry["refreshable"]


def test_filters_and_personal_state_persist(client):
    record = add_record(client)
    assert client.get("/api/opportunities?cohort=2035&region=北京").json()["total"] == 1
    assert client.get("/api/opportunities?cohort=2036").json()["total"] == 0
    response = client.patch(
        "/api/opportunities/" + record["id"] + "/state",
        json={"saved": True, "applied": True, "notes": "已投，等待反馈"},
        headers=auth(client),
    )
    assert response.status_code == 200
    row = client.get("/api/opportunities/" + record["id"]).json()
    assert row["personal"] == {"saved": True, "applied": True, "notes": "已投，等待反馈"}
    assert client.get("/api/opportunities?saved=1").json()["total"] == 1
    assert client.get("/api/opportunities?applied=0").json()["total"] == 0


def test_pending_never_appears_in_default_or_calendar(client):
    add_record(client, status="pending")
    assert client.get("/api/opportunities").json()["total"] == 0
    assert client.get("/api/opportunities?review=pending").json()["total"] == 1
    assert "BEGIN:VEVENT" not in client.get("/exports/deadlines.ics?review=pending").text


def test_date_only_calendar_is_all_day_and_csv_has_bom(client):
    row = add_record(client)
    calendar = client.get("/exports/deadlines.ics").text
    assert "DTSTART;VALUE=DATE:20351231" in calendar
    assert "DTEND;VALUE=DATE:20360101" in calendar
    assert row["id"] + "@soe-radar.local" in calendar
    assert client.get("/exports/opportunities.csv").content.startswith(b"\xef\xbb\xbf")


def test_local_writes_require_same_origin_token(client):
    assert client.post("/api/sync", json={}).status_code == 403
    assert (
        client.post(
            "/api/sync", json={}, headers={**auth(client), "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/sync", json={"source_ids": ["unknown"]}, headers=auth(client)).status_code == 422
    response = client.post("/api/sync", json={"source_ids": ["boc"]}, headers=auth(client))
    assert response.status_code == 202
    assert client.get("/api/runs/" + response.json()["id"]).json()["status"] == "queued"


def test_import_mapping_is_unverified_even_for_official_link(client):
    csv = "企业,招聘,官网,截止\n中国银行,中国银行2035届校园招聘,https://www.boc.cn/2035/test,2035-12-31\n"
    response = client.post(
        "/api/imports",
        files={"file": ("test.csv", csv.encode(), "text/csv")},
        data={"mapping": '{"employer":"企业","title":"招聘","url":"官网","deadline":"截止"}'},
        headers=auth(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    assert client.get("/api/opportunities").json()["total"] == 0
    assert client.get("/api/opportunities?review=pending").json()["total"] == 1


def test_detail_has_original_evidence_and_versions(client):
    row = add_record(client)
    detail = client.get("/api/opportunities/" + row["id"]).json()
    assert detail["documents"]
    assert detail["deadline"]["quote"] in detail["documents"][0]["text"]
    assert detail["events"][0]["kind"] == "new"


def test_reject_pending_record(client):
    row = add_record(client, status="pending")
    response = client.post("/api/reviews/" + row["id"], json={"decision": "reject"}, headers=auth(client))
    assert response.status_code == 200
    assert client.get("/api/opportunities?review=pending").json()["total"] == 0


def test_snapshot_review_records_attachment_transcription_and_year_basis(client):
    from soe_radar.schemas import Claim

    doc = DocumentInput(
        url="https://www.boc.cn/poster",
        title="中国银行2035届校园招聘",
        text="招聘公告请见海报。",
        source_id="boc",
        employer_hint="中国银行股份有限公司",
        attachments=[{"url": "https://www.boc.cn/poster.png", "status": "needs_review"}],
    )
    result = extract_rules(doc)
    row = ingest(client.app.state.db, client.app.state.cfg, doc, result)
    result.deadline = Claim(kind="date", value="2035-10-31", quote="面向2035届，报名截止10月31日")
    result.review_reasons = []
    response = client.post(
        "/api/reviews/" + row["id"],
        json={
            "decision": "approve",
            "use_snapshot": True,
            "published_at": "2035-09-01",
            "publication_evidence": "海报右上角注明2035年9月发布",
            "extraction": result.model_dump(mode="json"),
            "evidence": [
                {
                    "attachment_url": "https://www.boc.cn/poster.png",
                    "location": "海报下方报名时间",
                    "quote": result.deadline.quote,
                }
            ],
            "resolution_note": "已逐字核对海报日期与届别",
        },
        headers=auth(client),
    )
    assert response.status_code == 200, response.text
    detail = client.get("/api/opportunities/" + row["id"]).json()
    assert detail["review_status"] == "manual"
    assert detail["documents"][0]["text"] == doc.text
    assert detail["reviews"][0]["payload"]["evidence"][0]["location"] == "海报下方报名时间"


def test_reject_and_manual_decisions_survive_new_document_versions(client):
    row = add_record(client, status="pending")
    client.post("/api/reviews/" + row["id"], json={"decision": "reject"}, headers=auth(client))
    import asyncio

    from sqlalchemy import select

    from soe_radar.models import Document, Opportunity
    from soe_radar.sync import Pipeline

    with client.app.state.db.session() as s:
        stored = s.scalar(select(Document).where(Document.opportunity_id == row["id"]))
        doc = DocumentInput(
            url=stored.url,
            title=stored.title,
            text=stored.text + "欢迎报名。",
            published_at=stored.published_at,
            source_id=stored.source_id,
            employer_hint="中国银行股份有限公司",
        )

    async def process():
        pipe = Pipeline(client.app.state.cfg, client.app.state.db)
        await pipe.process(doc)
        await pipe.close()

    asyncio.run(process())
    with client.app.state.db.session() as s:
        assert s.get(Opportunity, row["id"]).review_status == "rejected"


def test_manual_review_refetches_official_evidence_and_preserves_personal_state(client, monkeypatch):
    from sqlalchemy import select

    from soe_radar.fetching import Fetcher
    from soe_radar.models import Document

    record = add_record(client, status="pending")
    client.patch("/api/opportunities/" + record["id"] + "/state", json={"saved": True}, headers=auth(client))
    with client.app.state.db.session() as s:
        version = s.scalar(select(Document))
        document = DocumentInput(
            url=version.url,
            title=version.title,
            text=version.text,
            published_at=date(2035, 9, 1),
            source_id="boc",
            employer_hint="中国银行股份有限公司",
        )
    fetched = []

    async def read(self, url, source, title_hint=""):
        fetched.append(url)
        return document

    monkeypatch.setattr(Fetcher, "document", read)
    response = client.post(
        "/api/reviews/" + record["id"],
        json={"decision": "approve", "extraction": extract_rules(document).model_dump(mode="json")},
        headers=auth(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["review_status"] == "manual"
    assert response.json()["personal"]["saved"]
    assert fetched == [document.url]


def test_group_filter_includes_evidence_configured_subsidiaries(client):
    from soe_radar.config import EmployerConfig
    from soe_radar.db import seed

    cfg, db = client.app.state.cfg, client.app.state.db
    cfg.employers = [
        EmployerConfig(
            name="示例集团子公司", parent="中国银行股份有限公司", proof_url="https://www.boc.cn/aboutboc/"
        )
    ]
    with db.session() as s:
        seed(s, cfg)
    document = DocumentInput(
        url="https://www.boc.cn/subsidiary",
        title="示例集团子公司2035届校园招聘",
        text="面向2035届毕业生，报名截止2035年12月31日。",
        employer_hint="示例集团子公司",
        source_id="boc",
    )
    result = ingest(db, cfg, document, extract_rules(document))
    assert result["review_status"] == "auto"
    rows = client.get("/api/opportunities", params={"employer": "中国银行股份有限公司"}).json()["items"]
    assert any(x["id"] == result["id"] for x in rows)


def test_bad_query_and_formula_export(client):
    row = add_record(client)
    assert client.get("/api/opportunities?cohort=bad").status_code == 422
    assert client.get("/api/opportunities?days=-1").status_code == 422
    from soe_radar.exports import csv_export

    row["title"] = '=HYPERLINK("https://evil.example")'
    assert "'=HYPERLINK" in csv_export([row]).decode("utf-8-sig")


def test_manually_confirmed_record_is_flagged_for_review_when_source_changes(client):
    record = add_record(client)
    from sqlalchemy import select

    from soe_radar.models import Document, Opportunity

    with client.app.state.db.session() as s:
        s.get(Opportunity, record["id"]).review_status = "manual"
        version = s.scalar(select(Document))
        doc = DocumentInput(
            url=version.url,
            title=version.title,
            text=version.text.replace("12月31日", "11月30日"),
            published_at=version.published_at,
            source_id="boc",
            employer_hint="中国银行股份有限公司",
        )
    result = ingest(client.app.state.db, client.app.state.cfg, doc, extract_rules(doc))
    assert result["review_status"] == "manual" and result["needs_review"]
    assert result["deadline"]["value"] == "2035-12-31"
    assert client.get("/api/opportunities").json()["total"] == 0
    assert client.get("/api/opportunities?review=pending").json()["total"] == 1
