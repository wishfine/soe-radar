from datetime import date

import pytest

from soe_radar.extraction import extract_rules
from soe_radar.schemas import DocumentInput


def doc(text, published=date(2026, 9, 2)):
    return DocumentInput(
        url="https://www.boc.cn/notice",
        title="中国银行2027届校园招聘公告",
        text=text,
        published_at=published,
        employer_hint="中国银行股份有限公司",
    )


def test_opening_date_is_not_deadline_when_closing_date_is_elsewhere():
    from soe_radar.extraction import validate_extraction
    from soe_radar.schemas import Claim

    document = doc("面向2027届。投递时间：2026年10月16日开启网申，简历投递截止时间请关注公众号。")
    result = extract_rules(document)
    assert result.deadline.kind == "unannounced"
    result.deadline = Claim(
        kind="date",
        value="2026-10-16",
        quote="投递时间：2026年10月16日开启网申，简历投递截止时间请关注公众号",
    )
    result.review_reasons = []
    assert validate_extraction(result, document)


def test_explicit_subsidiary_in_title_overrides_group_hint():
    document = doc("面向2027届毕业生，报名截止2026年10月31日。").model_copy(
        update={"title": "示例科技有限公司2027届校园招聘", "employer_hint": "中国电信集团有限公司"}
    )
    assert extract_rules(document).employer == "示例科技有限公司"


@pytest.mark.parametrize(
    ("text", "value", "kind"),
    [
        ("报名时间：2026年9月1日至2026年10月31日。", "2026-10-31", "date"),
        ("报名截止时间为北京时间2026年10月9日24点。", "2026-10-10T00:00:00+08:00", "datetime"),
        ("报名截止时间：2026年10月9日17:30。", "2026-10-09T17:30:00+08:00", "datetime"),
        ("网上报名：9月2日—10月7日。", "2026-10-07", "date"),
        ("报名截止另行通知。毕业时间不晚于2027年7月31日。", None, "unannounced"),
        ("毕业生年龄计算截止时间为2027年7月31日。", None, "unannounced"),
        ("笔试时间：2026年10月25日。请尽快投递。", None, "unannounced"),
        ("简历投递长期有效。", None, "long_term"),
        ("岗位招满即止，请尽快投递。", None, "rolling"),
    ],
)
def test_application_deadline_semantics(text, value, kind):
    result = extract_rules(doc(text))
    assert result.deadline.value == value
    assert result.deadline.kind == kind
    if value:
        assert text[result.deadline.start : result.deadline.end] == result.deadline.quote


def test_cross_year_window_uses_publication_context():
    result = extract_rules(doc("报名时间：12月20日至次年1月10日。", date(2026, 12, 15)))
    assert result.deadline.value == "2027-01-10"


def test_old_notice_reparsed_does_not_use_machine_year():
    result = extract_rules(doc("网申截止：10月9日。", date(2023, 9, 2)))
    assert result.deadline.value == "2023-10-09"


def test_missing_year_and_publication_is_not_guessed():
    result = extract_rules(doc("报名截止：10月9日。", None))
    assert result.deadline.value is None
    assert result.review_reasons


def test_multi_cohorts_are_preserved():
    result = extract_rules(doc("面向2026、2027届毕业生。报名截止：2026年10月30日。"))
    assert result.cohorts == [2026, 2027]


def test_conflicting_application_dates_require_review():
    result = extract_rules(doc("报名截止：2026年10月10日。网申截止：2026年10月20日。"))
    assert result.deadline.kind == "conflict"
    assert result.review_reasons


def test_model_cannot_label_urgency_note_as_rolling_deadline():
    from soe_radar.extraction import validate_extraction
    from soe_radar.schemas import Claim

    document = doc("请尽快投递。")
    result = extract_rules(document)
    result.review_reasons = []
    result.deadline = Claim(kind="rolling", quote="尽快投递")
    assert validate_extraction(result, document)


def test_model_cannot_turn_an_opening_date_into_a_deadline():
    from soe_radar.extraction import validate_extraction
    from soe_radar.schemas import Claim

    document = doc("报名开始时间：2026年9月2日。")
    result = extract_rules(document)
    result.review_reasons = []
    result.deadline = Claim(kind="date", value="2026-09-02", quote="报名开始时间：2026年9月2日")
    assert validate_extraction(result, document)


def test_model_cannot_remove_qualification_context_from_evidence():
    from soe_radar.extraction import validate_extraction
    from soe_radar.schemas import Claim

    document = doc("年龄计算截止时间：2027年7月31日。")
    result = extract_rules(document)
    result.review_reasons = []
    result.deadline = Claim(kind="date", value="2027-07-31", quote="截止时间：2027年7月31日")
    assert validate_extraction(result, document)


def test_multiline_window_stops_before_next_section():
    text = "四、时间安排\n（一）\n统招\n网上报名\n9月2日—10月7日\n（二）校园宣讲\n9月5日—9月30日\n（三）全国统考\n10月25日"
    result = extract_rules(doc(text))
    assert result.deadline.value == "2026-10-07"
    assert "宣讲" not in result.deadline.quote


def test_model_cannot_hide_conflicting_deadlines_in_other_paragraphs():
    from soe_radar.extraction import validate_extraction
    from soe_radar.schemas import Claim

    document = doc("报名截止：2026年10月10日。网申截止：2026年10月20日。")
    result = extract_rules(document)
    result.review_reasons = []
    result.deadline = Claim(kind="date", value="2026-10-10", quote="报名截止：2026年10月10日")
    assert validate_extraction(result, document)


def test_model_cannot_change_graduation_cohort():
    from soe_radar.extraction import validate_extraction

    document = doc("面向2027届毕业生。报名截止2026年10月31日。")
    result = extract_rules(document)
    result.cohorts = [2028]
    assert validate_extraction(result, document)


def test_result_publication_is_not_an_open_campus_recruitment():
    from soe_radar.extraction import extract_rules
    from soe_radar.schemas import DocumentInput

    result = extract_rules(
        DocumentInput(
            url="https://example.gov.cn/notice",
            title="山西建设投资集团有限公司2026年度高校毕业生招聘结果公示",
            text="现将2026年度高校毕业生招聘结果公示如下。",
        )
    )
    assert result.recruitment_type == "notice"
