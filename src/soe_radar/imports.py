from __future__ import annotations

import csv
import io
from datetime import date
from urllib.parse import quote

from .extraction import extract_rules
from .schemas import DocumentInput
from .store import ingest

ALIASES = {
    "employer": ["公司名称", "公司", "企业名称", "company", "employer"],
    "title": ["招聘公告", "招聘岗位", "岗位", "title"],
    "url": ["公告链接", "公告地址", "notice_url", "official_url", "url"],
    "apply_url": ["投递链接", "网申链接", "apply_url"],
    "deadline": ["截止日期", "截止时间", "截止", "deadline"],
    "cohorts": ["届次", "届别", "target", "cohorts"],
    "locations": ["工作地点", "城市", "location", "locations"],
    "published_at": ["公告发布时间", "发布日期", "published_at"],
}


def import_csv(db, cfg, content: bytes, mapping: dict | None = None):
    if len(content) > 5_000_000:
        raise ValueError("CSV 最大 5MB")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = content.decode("gb18030")
    reader = csv.DictReader(io.StringIO(decoded))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    mapping = mapping or {}
    columns = {
        key: mapping.get(key) or next((x for x in aliases if x in reader.fieldnames), "")
        for key, aliases in ALIASES.items()
    }
    if not columns["employer"] and not columns["title"]:
        raise ValueError("需要公司名称或招聘公告列；可通过字段映射指定")
    ids, errors = [], []
    for index, row in enumerate(reader, start=2):
        if index > 2001:
            errors.append({"row": index, "error": "单次最多导入 2000 行，请拆分文件"})
            break

        def get(key):
            return (row.get(columns.get(key, "")) or "").strip()

        if not any(str(v).strip() for v in row.values() if v):
            continue
        title = get("title") or get("employer") + "招聘线索"
        # Import values are explicitly third-party evidence, never a fetched official document.
        url = get("url") or "https://import.invalid/" + quote(get("employer") + "/" + title)
        text = "\n".join(f"{key}：{value}" for key, value in row.items() if key and value)
        published = None
        try:
            if get("published_at"):
                published = date.fromisoformat(get("published_at").replace("/", "-"))
            doc = DocumentInput(
                url=url,
                title=title,
                text=text,
                published_at=published,
                source_id="import",
                employer_hint=get("employer"),
            )
            result = extract_rules(doc)
            result.apply_url = get("apply_url")
            result.review_reasons.append("CSV 为第三方线索，需读取官方原文核验")
            saved = ingest(db, cfg, doc, result)
            ids.append(saved["id"])
        except ValueError as exc:
            errors.append({"row": index, "error": str(exc)[:160]})
    return {"imported": len(ids), "ids": ids, "errors": errors, "mapping": columns}
