from datetime import date

from soe_radar.extraction import extract_rules
from soe_radar.fetching import extract_page
from soe_radar.schemas import DocumentInput


def document(text, **updates):
    return DocumentInput(
        url="https://www.boc.cn/review",
        title="中国银行2027届校园招聘",
        text=text,
        published_at=date(2026, 9, 3),
        employer_hint="中国银行股份有限公司",
        source_id="boc",
    ).model_copy(update=updates)


def test_clock_ranges_are_not_calendar_dates():
    result = extract_rules(document("报名截止尚未公布。双选会时间为9月25日14:00-17:00。咨询时间9:10-11:30。"))
    assert result.deadline.value is None
    assert not any("month must" in reason for reason in result.review_reasons)


def test_signature_and_next_paragraph_cannot_supply_application_deadline():
    result = extract_rules(document("请关注后续通知\n2026年9月29日\n扫码快速投递简历\n到岗时间请商议"))
    assert result.deadline.value is None


def test_line_wrapped_deadline_remains_readable_without_signature_contamination():
    result = extract_rules(document("报名截止\n10月21日24:00\n中国银行\n2026年9月29日"))
    assert result.deadline.value == "2026-10-22T00:00:00+08:00"


def test_heading_publication_does_not_match_head_title_or_deadline():
    page = extract_page(
        "https://example.org/a",
        "<head><title>示例2027校园招聘</title></head><body><nav>导航很长"
        + "导航" * 60
        + "</nav><h1>示例2027校园招聘</h1><p>2026-09-03</p><article>报名截止时间：2026年10月31日。欢迎报名。</article></body>",
    )
    assert page.published_at == date(2026, 9, 3)
    missing = extract_page(
        "https://example.org/a",
        "<h1>示例2027校园招聘</h1><article>报名截止时间：2026年10月31日。欢迎报名。</article>",
    )
    assert missing.published_at is None


def test_empty_image_src_is_not_an_attachment():
    page = extract_page("https://example.org/a", '<article>示例2027校园招聘<img src=""></article>')
    assert page.attachments == []


def test_notification_and_body_social_recruitment_are_not_campus():
    assert (
        extract_rules(document("校园招聘 社会招聘 导航", title="网站系统紧急升级公告")).recruitment_type
        == "notice"
    )
    assert (
        extract_rules(
            document(
                "本次为社会招聘，需具有三年工作经验。报名截止2026年10月31日。", title="示例公司总经理招聘公告"
            )
        ).recruitment_type
        == "social"
    )


def test_employer_suffix_and_unrelated_cities_are_cleaned():
    result = extract_rules(
        document(
            "公司总部位于北京。工作地点为苏州、上海。招聘人数20人。",
            title="示例科技有限公司公开招聘2027年应届高校毕业生公告",
            employer_hint="示例科技有限公司",
        )
    )
    assert result.employer == "示例科技有限公司"
    assert result.cohorts == [2027]
    assert result.locations == ["苏州", "上海"]


def test_genuine_conflict_requires_manual_explanation_and_keeps_evidence_check():
    from soe_radar.extraction import validate_extraction
    from soe_radar.schemas import Claim

    doc = document("岗位A报名截止2026年10月31日。岗位B报名截止2026年11月10日。")
    corrected = extract_rules(doc)
    corrected.review_reasons = []
    corrected.deadline = Claim(
        kind="date", value="2026-10-31", quote="岗位A报名截止2026年10月31日", scope="岗位A"
    )
    assert validate_extraction(corrected, doc)
    assert (
        validate_extraction(corrected, doc, manual=True, resolution_note="此记录只适用岗位A，岗位B独立核验")
        == []
    )
    corrected.deadline.quote = "不存在的报名截止2026年10月31日"
    assert validate_extraction(corrected, doc, manual=True, resolution_note="人工确认")


def test_social_application_channel_and_location_prefix_are_unambiguous():
    assert (
        extract_rules(
            document("请登录我行社会招聘网站报名。", title="中国银行高层次海外人才招聘公告")
        ).recruitment_type
        == "social"
    )
    assert extract_rules(document("工作地点均在北京。")).locations == ["北京"]


def test_subsidiary_brand_is_not_silently_replaced_by_parent_group():
    result = extract_rules(
        document(
            "面向2027届，报名截止2026年10月31日。",
            title="天翼数字生活2027届校园招聘",
            employer_hint="中国电信集团有限公司",
        )
    )
    assert result.employer == "天翼数字生活"
