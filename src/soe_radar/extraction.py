from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .schemas import Claim, DeadlineKind, DocumentInput, ExtractionResult, Position

PARSER_VERSION = "2026-10-v12"
DATE_RE = re.compile(
    r"(?<!\d)(?<!\d:)(?<!\d：)(?:(20\d{2})\s*[年./\-]\s*)?(\d{1,2})\s*[月./\-]\s*(\d{1,2})(?![\d:：])\s*日?"
)
APPLY = re.compile(r"报名|网申|投递|简历.{0,5}(?:接收|接收)|申请(?:截止|时间|期限)|截止(?:日期|时间)")
NEGATIVE = re.compile(r"年龄|毕业时间|毕业证|学位证|取得学历|资格计算|笔试|面试|宣讲|公示|录用通知")
POSITIVE = re.compile(r"(?:报名|网申|投递|申请).{0,8}(?:截止|截至|时间|期限)|截止.{0,12}(?:报名|投递|网申)")
RANGE = re.compile(r"至|到|—|~|～|－|\s-\s")
CITIES = [
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "成都",
    "武汉",
    "西安",
    "合肥",
    "天津",
    "重庆",
    "长沙",
    "济南",
    "郑州",
    "福州",
    "厦门",
    "沈阳",
    "大连",
    "哈尔滨",
    "呼和浩特",
    "兰州",
    "太原",
    "石家庄",
    "南昌",
    "贵阳",
    "昆明",
    "南宁",
    "海口",
    "乌鲁木齐",
    "全国",
    "海外",
]


def _dated(clause: str, published: date | None) -> tuple[list[tuple[date, re.Match]], str]:
    matches = list(DATE_RE.finditer(clause))
    matches = [
        m
        for m in matches
        if not (
            m.group(1) is None
            and not re.search("月|日", m.group())
            and re.match(r"[个人名岁项]", clause[m.end() :])
        )
    ]
    found = []
    last = None
    basis = ""
    for m in matches:
        y, month, day = m.groups()
        month, day = int(month), int(day)
        if y:
            year = int(y)
        elif last:
            year = last.year + int(month < last.month)
            basis = "日期区间起点年份"
        elif published:
            year = published.year
            if "次年" in clause[: m.start()] or (published.month >= 10 and month <= 3):
                year += 1
            basis = f"公告发布日期 {published.isoformat()}"
        else:
            raise ValueError("日期缺少年份且无公告发布上下文")
        try:
            value = date(year, month, day)
        except ValueError:
            raise ValueError(f"无效日期：{month}月{day}日") from None
        if not y and published and abs((value - published).days) > 200:
            raise ValueError("无年份日期与公告发布日相距过远")
        found.append((value, m))
        last = value
    return found, basis


def parse_claim(clause: str, published: date | None, offset: int = 0) -> Claim:
    dates, basis = _dated(clause, published)
    if not dates:
        return Claim()
    if len(dates) > 1 and not RANGE.search(clause[dates[0][1].end() : dates[-1][1].start()]):
        raise ValueError("同一段存在多个日期且适用关系不清")
    day, match = dates[-1]
    tail = clause[match.end() :]
    tm = re.match(r"\s*(?:北京时间\s*)?(\d{1,2})(?:[:：](\d{2})|[时点](?:(\d{1,2})分?)?)", tail)
    kind, value = DeadlineKind.DATE, day.isoformat()
    if tm:
        hour, m1, m2 = tm.groups()
        minute = int(m1 or m2 or 0)
        hour = int(hour)
        if hour == 24 and minute == 0:
            dt = datetime.combine(day + timedelta(days=1), datetime.min.time())
        else:
            try:
                dt = datetime(day.year, day.month, day.day, hour, minute)
            except ValueError:
                raise ValueError(f"无效截止时刻：{hour}时{minute}分") from None
        kind, value = DeadlineKind.DATETIME, dt.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()
    return Claim(
        kind=kind, value=value, quote=clause, start=offset, end=offset + len(clause), year_basis=basis
    )


def opening_only(clause: str) -> bool:
    matches = list(DATE_RE.finditer(clause))
    if len(matches) != 1:
        return False
    marker = matches[0]
    following = clause[marker.end() : marker.end() + 12]
    preceding = clause[max(0, marker.start() - 15) : marker.start()]
    return bool(
        re.match(r"\s*(?:起|开启|启动|开始)", following)
        or re.search(r"(?:开始|开启|启动)(?:报名|网申|投递|时间)?[：:\s]*$", preceding)
    )


def extract_rules(document: DocumentInput) -> ExtractionResult:
    title, text = document.title, document.text
    combined = title + "\n" + text
    result = ExtractionResult(title=title, employer=document.employer_hint)
    subject = (
        re.split(r"20\d{2}|校园招聘|高校毕业生|招聘公告", title)[0].strip(" -—｜：").removeprefix("关于")
    )
    if 2 < len(subject) < 70 and re.search(r"公司|集团|银行|研究院|中心|科技", subject):
        entity = re.match(r"(.+?(?:股份有限公司|有限责任公司|有限公司))", subject)
        branch = subject[entity.end() :] if entity else ""
        keep_branch = bool(
            branch
            and re.fullmatch(
                r".{0,35}(?:分公司|公司|分行|分中心|中心|研究院|研究所|事业部|分部|总部)", branch
            )
            and not re.search(r"招聘|岗位|启动|开启|公示|拟录用", branch)
        )
        subject = (
            subject
            if keep_branch
            else entity.group(1)
            if entity
            else re.split(r"公开招聘|招聘|解决方案部|总经理岗位", subject)[0]
        )
        hint = document.employer_hint
        result.employer = (
            hint
            if hint and (subject == hint or subject.replace("省", "") == hint.replace("省", ""))
            else subject
        )
    elif (
        2 < len(subject) < 70
        and re.search(r"校园|校招|应届|毕业生", title)
        and subject not in document.employer_hint
    ):
        result.employer = subject
    if re.search(
        r"拟录用|拟接收|拟聘|情况公示|结果公示|招聘结果|录用结果|面试成绩|笔试成绩|笔试通知|面试通知|系统.{0,5}(?:升级|维护)|网站.{0,5}(?:升级|维护)|停机|暂停服务",
        title,
    ):
        result.recruitment_type = "notice"
    elif re.search(r"内部招聘|系统内招聘|竞争上岗", title):
        result.recruitment_type = "internal"
    elif re.search(r"社会招聘|社招", title):
        result.recruitment_type = "social"
    elif "博士后" in title:
        result.recruitment_type = "postdoc"
    elif re.search(r"实习", title):
        result.recruitment_type = "intern"
    elif re.search(r"(?:本次|本公告|招聘类型)[^。\n]{0,12}社会招聘|面向社会.{0,8}招聘", text[:2000]) or (
        not re.search(r"校园|毕业生|应届", title) and re.search(r"社会招聘(?:网站|岗位|渠道)", text)
    ):
        result.recruitment_type = "social"
    elif re.search(r"校园|校招|毕业生|应届|20\d{2}届", title):
        result.recruitment_type = "campus"
    elif re.search(r"应届毕业生|校园招聘|高校毕业生", text[:1600]):
        result.recruitment_type = "campus"
    elif document.structured.get("recruitment_type") == "campus":
        result.recruitment_type = "campus"
    for series in re.findall(r"20\d{2}(?:\s*[、,，/和及至\-]\s*20\d{2})*\s*届", combined):
        years = [int(x) for x in re.findall(r"20\d{2}", series)]
        if "至" in series or "-" in series:
            if len(years) == 2 and 0 <= years[-1] - years[0] <= 5:
                years = list(range(years[0], years[-1] + 1))
        result.cohorts.extend(years)
    if not result.cohorts:
        result.cohorts = [
            int(x)
            for x in re.findall(
                r"(20\d{2})(?:年(?:度)?)?(?:全球)?(?:应届)?(?:校园招聘|高校毕业生|大学毕业生|秋季校园招聘|春季校园招聘)",
                combined,
            )
        ]
    result.cohorts = sorted(set(result.cohorts))
    for word in ["提前批", "补录", "补招", "第一批", "第二批", "第三批"]:
        if word in title:
            result.batch = word
            break
    result.season = "春招" if "春" in title else "秋招" if "秋" in title else ""
    for raw_location in re.findall(r"(?:工作地点|工作城市|招聘地点)[为是：:\s]*([^\n。；]{2,100})", text):
        for value in re.split(r"[、,，;/|]", raw_location):
            value = value.strip()
            value = re.sub(r"^(?:均在|位于|为|是)", "", value)
            if (
                value
                and len(value) <= 12
                and not re.search(r"详见|不限|岗位|条件|户口|户籍|生源|招聘|择优|解决", value)
                and value not in result.locations
            ):
                result.locations.append(value)
    result.education = [x for x in ["专科", "本科", "硕士", "博士"] if x in text]
    if document.structured.get("location"):
        result.locations = [
            x.strip() for x in re.split(r"[、,，;/|]", document.structured["location"]) if x.strip()
        ]
    if document.structured.get("education"):
        result.education = [document.structured["education"]]
    result.majors = [
        x
        for x in [
            "计算机",
            "软件工程",
            "人工智能",
            "电气工程",
            "机械工程",
            "经济学",
            "金融",
            "法学",
            "土木工程",
            "通信工程",
            "自动化",
            "化学工程",
            "材料科学",
            "会计",
        ]
        if x in text
    ]
    for title_line in re.findall(r"(?:职位名称|岗位名称|招聘岗位)[：:]\s*([^\n。]{2,100})", text):
        for position in re.split(r"[、,，;/]", title_line):
            if 2 <= len(position.strip()) <= 30 and not re.search(r"招聘|详见|如下", position):
                result.positions.append(
                    Position(
                        title=position.strip(),
                        locations=result.locations,
                        majors=result.majors,
                        education=result.education,
                    )
                )
    if not result.employer:
        prefix = re.split(r"20\d{2}|校园招聘|高校毕业生|招聘公告", title)[0].strip(" -—｜：")
        if 2 < len(prefix) < 70:
            result.employer = prefix
    if "尽快投递" in text:
        result.urgency_note = "尽快投递"
    if re.search(r"延长|延期", title):
        result.action = "extend"
    elif re.search(r"提前(?:结束|关闭|截止)|终止招聘|取消招聘", title):
        result.action = "close"
    elif re.search(r"重新(?:开放|启动)|恢复报名", title):
        result.action = "reopen"
    if result.action != "recruitment":
        result.target_title = title
        if re.search(r"部分岗位|部分单位|部分职位", combined[:600]):
            result.review_reasons.append("变更仅适用于部分对象，需确认岗位范围")
    claims = []
    clauses = list(re.finditer(r"[^。；;！？\n]+", text))
    for segment in clauses:
        raw = segment.group()
        # Keep only date continuations after a deadline heading, not signatures or footers.
        if APPLY.search(raw):
            tail = text[segment.end() :]
            for line in tail.splitlines()[1:]:
                ending = bool(re.search(r"[。；;！？]$", line))
                line = line.rstrip("。；;！？")
                if len(raw) > 250 or not re.fullmatch(
                    r"\s*(?:(?:自|至|截至|即日起)[：:]?)?[\d\s年月日时点分北京时间至到—～~:：./()（）\-]+\s*",
                    line,
                ):
                    break
                raw += "\n" + line
                if ending:
                    break
        # A deadline heading may be on the preceding line; retain a short bounded window only.
        for marker in APPLY.finditer(raw):
            begin = max(0, marker.start() - 15)
            clause = raw[begin : marker.end() + 220].strip()
            local = raw.find(clause, begin)
            # Do not accidentally read a later exam/qualification paragraph as an application deadline.
            relative_end = max(0, marker.end() - begin)
            cut = re.search(
                r"\n\s*(?:[（(]?[一二三四五六七八九十]+[）)、.、]|\d+[.、])", clause[relative_end:]
            )
            if cut:
                clause = clause[: relative_end + cut.start()].rstrip()
            if NEGATIVE.search(clause) and not POSITIVE.search(clause):
                continue
            if re.search(r"另行通知|尚未公布", clause):
                continue
            if opening_only(clause):
                continue
            if re.search(r"年龄|毕业时间|毕业证|学位证|资格计算", clause):
                continue
            if not re.search(r"截止|截至|结束|之前|前|至|到|—|～|~", clause):
                continue
            try:
                claim = parse_claim(clause, document.published_at, segment.start() + local)
                if claim.value and claim.value not in [x.value for x in claims]:
                    # Without a deadline/window signal a date may just be the opening date.
                    if len(list(DATE_RE.finditer(clause))) == 1 and not re.search(
                        r"截止|截至|之前|前|至|到|结束", clause
                    ):
                        continue
                    claims.append(claim)
            except (ValueError, OverflowError) as exc:
                result.review_reasons.append(str(exc))
    if len(claims) == 1:
        result.deadline = claims[0]
        try:
            dates, _ = _dated(claims[0].quote, document.published_at)
            if len(dates) >= 2:
                result.application_start = dates[0][0].isoformat()
        except ValueError:
            pass
    elif len(claims) > 1:
        # An extension explicitly states the new deadline; keep ambiguous old/new pairs for review.
        result.deadline = Claim(kind=DeadlineKind.CONFLICT)
        result.review_reasons.append("存在多个报名截止日期，需确认批次或岗位范围")
    else:
        for expression, kind in [
            (r"招满即止|额满即止", DeadlineKind.ROLLING),
            (r"长期(?:有效|招聘)|常年招聘", DeadlineKind.LONG_TERM),
        ]:
            match = re.search(expression, text)
            if match:
                result.deadline = Claim(kind=kind, quote=match.group(), start=match.start(), end=match.end())
                break
    if result.deadline.kind == DeadlineKind.UNANNOUNCED and result.action != "close":
        result.review_reasons.append("投递截止未公布或尚未可靠提取")
    result.review_reasons = list(dict.fromkeys(result.review_reasons))
    apply_link = re.search(r"(?:报名网址|投递网址|网申地址|报名地址)[：:\s]*(https?://[^\s<>，。；]+)", text)
    if apply_link:
        result.apply_url = apply_link.group(1)
    return result


def validate_extraction(
    result: ExtractionResult, document: DocumentInput, *, manual=False, evidence=(), resolution_note=""
) -> list[str]:
    """Evidence, not model confidence, decides whether a claim can be published."""
    reasons = list(result.review_reasons)
    raw_text = document.text
    if manual:
        from .urls import canonical

        attachments = {canonical(a["url"]) for a in document.attachments if a.get("url")}
        transcribed = []
        for entry in evidence:
            if canonical(entry.attachment_url) not in attachments:
                reasons.append("人工转录未绑定这份文档的附件")
            else:
                transcribed.append(entry.quote)
        if transcribed:
            document = document.model_copy(update={"text": document.text + "\n" + "\n".join(transcribed)})
    if result.recruitment_type != "campus":
        reasons.append("未确认为校园招聘")
    original = extract_rules(document)
    original_type = original.recruitment_type
    if original_type in ("internal", "social", "intern", "postdoc", "notice"):
        reasons.append("原公告属于非目标招聘类型")
    if not result.employer:
        reasons.append("招聘主体不明确")
    if not result.cohorts:
        reasons.append("毕业届别不明确")
    elif (
        original.cohorts and set(result.cohorts) != set(original.cohorts) and not (manual and resolution_note)
    ):
        reasons.append("毕业届别与原文明确表述不一致")
    elif any(str(year) not in document.title + document.text for year in result.cohorts) and not (
        manual and resolution_note
    ):
        reasons.append("毕业届别无法在原文中定位")
    if (
        original.deadline.kind == DeadlineKind.CONFLICT
        and not (manual and resolution_note)
        and not result.scoped_deadlines
        and not (
            result.action == "extend" and re.search(r"延长.{0,10}至|延期至|调整为", result.deadline.quote)
        )
    ):
        reasons.append("原文存在多项截止声明，不能省略冲突或适用范围")
    if (
        any("变更仅适用" in reason for reason in original.review_reasons)
        and result.deadline.scope == "本批次"
    ):
        reasons.append("部分岗位的变更不能套用整批招聘")
    if (
        result.deadline.kind == DeadlineKind.UNANNOUNCED
        and not manual
        and not result.scoped_deadlines
        and result.action != "close"
    ):
        reasons.append("截止未公布，需核验后再进入可投列表")
    if result.action == "close" and not re.search(
        r"提前(?:结束|关闭|截止)|终止招聘|取消招聘", document.title
    ):
        reasons.append("缺少明确的关闭通知标题")
    for claim in [result.deadline, *result.scoped_deadlines]:
        if claim.kind == DeadlineKind.CONFLICT:
            reasons.append("日期冲突")
        if claim.kind in (DeadlineKind.DATE, DeadlineKind.DATETIME):
            start = document.text.find(claim.quote) if claim.quote else -1
            if start < 0:
                reasons.append("日期证据无法在原文中定位")
                continue
            claim.start, claim.end = start, start + len(claim.quote)
            if claim.quote not in raw_text:
                if not any(e.quote == claim.quote and e.scope == claim.scope for e in evidence):
                    reasons.append("人工转录的适用范围与截止声明不一致")
                claim.start = claim.end = -1
            context = re.split(r"[。；;\n]", document.text[max(0, start - 40) : start])[-1] + claim.quote
            if (
                not re.search(r"报名|网申|投递|申请|简历.{0,5}接收", context)
                or re.search(r"年龄|毕业时间|毕业证|学位证|笔试|面试|宣讲", context)
                or not re.search(r"截止|截至|结束|之前|前|至|到|—|～|~", claim.quote)
                or opening_only(claim.quote)
            ):
                reasons.append("日期证据不属于明确报名语义")
                continue
            try:
                evidence = parse_claim(claim.quote, document.published_at, start)
                if evidence.value != claim.value or evidence.kind != claim.kind:
                    reasons.append("日期值与原文证据不一致")
            except ValueError:
                reasons.append("原文日期无法校验")
        elif claim.kind in (DeadlineKind.ROLLING, DeadlineKind.LONG_TERM):
            if not claim.quote or claim.quote not in document.text:
                reasons.append("招聘期限类型缺少原文证据")
            pattern = (
                r"招满即止|额满即止" if claim.kind == DeadlineKind.ROLLING else r"长期(?:有效|招聘)|常年招聘"
            )
            if not re.search(pattern, claim.quote):
                reasons.append("招聘期限类型与原文语义不一致")
            if claim.value:
                reasons.append("非明确截止类型不能携带日期")
        elif claim.value:
            reasons.append("未公布或冲突的截止不能携带确定日期")
    if result.scoped_deadlines and any(x.scope == "本批次" for x in result.scoped_deadlines):
        reasons.append("岗位独立截止缺少明确适用对象")
    return list(dict.fromkeys(reasons))
