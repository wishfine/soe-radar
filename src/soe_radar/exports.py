from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta


def safe_cell(value) -> str:
    result = ";".join(map(str, value)) if isinstance(value, list) else str(value or "")
    return "'" + result if result.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else result


def csv_export(rows: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    fields = {
        "employer": "公司名称",
        "title": "招聘公告",
        "cohorts": "届次",
        "batch": "批次",
        "locations": "工作地点",
        "education": "学历要求",
        "majors": "专业要求",
        "industry": "行业分类",
        "deadline_value": "截止日期",
        "deadline_kind": "截止类型",
        "quote": "截止原文",
        "official_url": "公告链接",
        "apply_url": "投递链接",
        "review_status": "核验状态",
        "last_checked": "最后核验时间",
    }
    writer = csv.writer(stream)
    writer.writerow(fields.values())
    for row in rows:
        values = dict(
            row,
            deadline_value=(row.get("deadline") or {}).get("value"),
            deadline_kind=(row.get("deadline") or {}).get("kind"),
            quote=(row.get("deadline") or {}).get("quote"),
        )
        writer.writerow([safe_cell(values.get(field)) for field in fields])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r", "")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold(line: str) -> list[str]:
    chunks, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 73:
            chunks.append(current)
            current, size = " " + char, 1 + width
        else:
            current, size = current + char, size + width
    return chunks + [current]


def ics_export(rows: list[dict], *, now: datetime | None = None) -> bytes:
    now = now or datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SOE Radar//Recruitment deadlines//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:国央企校招截止时间",
    ]
    for row in rows:
        deadline = row.get("deadline") or {}
        if row.get("review_status") not in ("auto", "manual") or deadline.get("kind") not in (
            "date",
            "datetime",
        ):
            continue
        if row.get("status") == "closed" or row.get("data_stale"):
            continue
        lines += [
            "BEGIN:VEVENT",
            f"UID:{row['id']}@soe-radar.local",
            f"SEQUENCE:{row.get('sequence', 0)}",
            "DTSTAMP:" + now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
        ]
        if deadline["kind"] == "date":
            day = date.fromisoformat(deadline["value"])
            lines += [
                "DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"),
                "DTEND;VALUE=DATE:" + (day + timedelta(days=1)).strftime("%Y%m%d"),
            ]
        else:
            moment = datetime.fromisoformat(deadline["value"]).astimezone(UTC)
            lines += [
                "DTSTART:" + moment.strftime("%Y%m%dT%H%M%SZ"),
                "DTEND:" + (moment + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ"),
            ]
        lines += [
            "SUMMARY:" + escape_ics(row["employer"] + " · " + row["title"] + " 截止"),
            "DESCRIPTION:"
            + escape_ics(
                "截止原文："
                + deadline.get("quote", "")
                + "\n来源："
                + row["official_url"]
                + "\n导出为快照，延期后请重新导出。"
            ),
            "URL:" + escape_ics(row["official_url"]),
            "END:VEVENT",
        ]
    lines += ["END:VCALENDAR"]
    return ("\r\n".join(part for line in lines for part in fold(line)) + "\r\n").encode("utf-8")
