from datetime import date

from sqlalchemy import func, select

from soe_radar.extraction import extract_rules
from soe_radar.models import Document, Opportunity, PersonalState
from soe_radar.schemas import DocumentInput
from soe_radar.store import ingest


def sample(cohort=2027, deadline="2026年10月31日", url="https://www.boc.cn/notice"):
    return DocumentInput(
        url=url,
        title=f"中国银行{cohort}届校园招聘公告",
        text=f"面向{cohort}届毕业生。报名截止：{deadline}。",
        published_at=date(cohort - 1, 9, 1),
        source_id="boc",
        employer_hint="中国银行股份有限公司",
    )


def test_idempotent_ingest_and_original_evidence(db, cfg):
    document = sample()
    first = ingest(db, cfg, document, extract_rules(document))
    second = ingest(db, cfg, document, extract_rules(document))
    assert first["id"] == second["id"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Opportunity)) == 1
        assert s.scalar(select(func.count()).select_from(Document)) == 1
        assert s.get(Opportunity, first["id"]).review_status == "auto"


def test_same_url_new_year_preserves_old_record_and_personal_state(db, cfg):
    old = sample()
    first = ingest(db, cfg, old, extract_rules(old))
    with db.session() as s:
        s.add(PersonalState(opportunity_id=first["id"], saved=True, applied=True))
    new = sample(2028, "2027年10月31日")
    second = ingest(db, cfg, new, extract_rules(new))
    assert first["id"] != second["id"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Opportunity)) == 2
        assert s.get(PersonalState, second["id"]) is None


def test_unofficial_import_never_auto_publishes(db, cfg):
    document = sample(url="https://example.org/notice")
    result = ingest(db, cfg, document, extract_rules(document))
    assert result["review_status"] == "pending"


def test_changed_document_preserves_versions(db, cfg):
    first = sample()
    old = ingest(db, cfg, first, extract_rules(first))
    new = sample(deadline="2026年11月10日")
    current = ingest(db, cfg, new, extract_rules(new))
    assert old["id"] == current["id"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 2
        assert s.get(Opportunity, current["id"]).sequence == 1


def test_parser_upgrade_revalidates_done_document_and_preserves_corrections(db, cfg):
    from soe_radar.models import ChangeEvent, Deadline

    document = sample()
    original = ingest(db, cfg, document, extract_rules(document))
    for wrong_date in ("2026-10-01", "2026-10-02"):
        with db.session() as s:
            version = s.scalar(select(Document))
            version.parser_version = "old-parser"
            opportunity = s.get(Opportunity, original["id"])
            s.get(Deadline, opportunity.latest_deadline_id).value = wrong_date
        corrected = ingest(db, cfg, document, extract_rules(document))
        assert corrected["deadline"]["value"] == "2026-10-31"
    with db.session() as s:
        corrections = s.scalars(select(ChangeEvent).where(ChangeEvent.kind == "deadline_changed")).all()
        assert len(corrections) == 2


def test_government_section_does_not_turn_institution_into_soe(db, cfg):
    document = sample().model_copy(
        update={
            "source_id": "sh-sasac",
            "url": "https://www.gzw.sh.gov.cn/test",
            "employer_hint": "示例事业单位",
            "title": "示例事业单位2027届校园招聘公告",
            "text": "本单位为事业单位，面向2027届毕业生，报名截止2026年10月31日。",
        }
    )
    result = ingest(db, cfg, document, extract_rules(document))
    assert result["review_status"] == "pending"
    assert result["org_type"] == "unknown"


def test_database_lease_excludes_other_workers_and_recovers(db):
    assert db.acquire("sync", "first")
    assert not db.acquire("sync", "second")
    db.release("sync", "first")
    assert db.acquire("sync", "second")


def test_pending_retries_do_not_duplicate_deadline_declarations(db, cfg):
    from soe_radar.models import Deadline

    document = sample(url="https://example.org/unverified")
    for _ in range(3):
        ingest(db, cfg, document, extract_rules(document))
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Deadline)) == 1


def test_extension_is_linked_and_early_close_keeps_original_deadline(db, cfg):
    from soe_radar.models import ChangeEvent

    first = sample()
    original = ingest(db, cfg, first, extract_rules(first))
    extension = first.model_copy(
        update={
            "url": "https://www.boc.cn/extension",
            "title": "关于中国银行2027届校园招聘延长报名时间的公告",
            "text": "报名截止延长至2026年11月10日。",
        }
    )
    updated = ingest(db, cfg, extension, extract_rules(extension))
    assert updated["id"] == original["id"]
    assert updated["deadline"]["value"] == "2026-11-10"
    close = first.model_copy(
        update={
            "url": "https://www.boc.cn/early-close",
            "title": "关于中国银行2027届校园招聘提前结束报名的公告",
            "text": "本批次校园招聘提前结束报名。",
        }
    )
    closed = ingest(db, cfg, close, extract_rules(close))
    assert closed["id"] == original["id"]
    assert closed["status"] == "closed"
    assert closed["deadline"]["value"] == "2026-11-10"
    with db.session() as s:
        kinds = list(s.scalars(select(ChangeEvent.kind).where(ChangeEvent.opportunity_id == original["id"])))
        assert set(kinds) == {"new", "extend", "close"}


def test_partial_extension_never_changes_whole_campaign(db, cfg):
    first = sample()
    original = ingest(db, cfg, first, extract_rules(first))
    extension = first.model_copy(
        update={
            "url": "https://www.boc.cn/partial-extension",
            "title": "关于中国银行2027届校园招聘部分岗位延长报名的公告",
            "text": "部分岗位报名截止延长至2026年11月10日。",
        }
    )
    pending = ingest(db, cfg, extension, extract_rules(extension))
    assert pending["review_status"] == "pending"
    with db.session() as s:
        from soe_radar.models import Deadline

        row = s.get(Opportunity, original["id"])
        assert s.get(Deadline, row.latest_deadline_id).value == "2026-10-31"


def test_ambiguous_event_cannot_attach_across_cohorts(db, cfg):
    original = sample()
    first = ingest(db, cfg, original, extract_rules(original))
    event = sample(2028, "2027年11月1日", url="https://www.boc.cn/other-year")
    extraction = extract_rules(event)
    extraction.action = "extend"
    import pytest

    with pytest.raises(ValueError, match="届别"):
        ingest(db, cfg, event, extraction, manual=True, target_id=first["id"])


def test_job_details_have_separate_storage(db, cfg):
    from soe_radar.models import JobPosting
    from soe_radar.schemas import Position

    document = sample()
    extraction = extract_rules(document)
    extraction.positions = [
        Position(title="软件工程师", locations=["苏州"]),
        Position(title="电气工程师", locations=["广州"]),
    ]
    saved = ingest(db, cfg, document, extraction)
    with db.session() as s:
        assert len(list(s.scalars(select(JobPosting).where(JobPosting.opportunity_id == saved["id"])))) == 2


def test_scoped_deadline_updates_preserve_child_state_and_calendar_sequence(db, cfg):
    from soe_radar.models import ChangeEvent
    from soe_radar.schemas import Claim

    def extraction(document, second):
        result = extract_rules(document)
        result.deadline = Claim()
        result.review_reasons = []
        result.scoped_deadlines = [
            Claim(kind="date", value="2035-10-31", quote="岗位A报名截止2035年10月31日", scope="岗位A"),
            Claim(
                kind="date",
                value=second,
                quote="岗位B报名截止" + second.replace("-", "年", 1).replace("-", "月") + "日",
                scope="岗位B",
            ),
        ]
        return result

    document = sample().model_copy(
        update={"text": "面向2027届。岗位A报名截止2035年10月31日。岗位B报名截止2035年11月1日。"}
    )
    first = extraction(document, "2035-11-01")
    first.scoped_deadlines[1].quote = "岗位B报名截止2035年11月1日"
    ingest(db, cfg, document, first)
    with db.session() as s:
        child = s.scalar(select(Opportunity).where(Opportunity.scope == "岗位B"))
        child_id = child.id
        s.add(PersonalState(opportunity_id=child.id, saved=True, applied=True))
    updated = document.model_copy(update={"text": document.text.replace("11月1日", "11月10日")})
    ingest(db, cfg, updated, extraction(updated, "2035-11-10"))
    with db.session() as s:
        child = s.get(Opportunity, child_id)
        assert child.sequence == 1
        assert s.get(PersonalState, child_id).applied
        assert (
            s.scalar(
                select(func.count()).select_from(ChangeEvent).where(ChangeEvent.opportunity_id == child_id)
            )
            == 2
        )
