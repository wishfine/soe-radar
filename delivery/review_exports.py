"""Generate reviewer-facing relationship and per-employer evidence checks. Offline."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).parent


def norm(value):
    return "".join((value or "").split())


def main():
    connection = sqlite3.connect(ROOT.parent / "data/radar.db")
    connection.row_factory = sqlite3.Row
    names = {row["id"]: row["name"] for row in connection.execute("select id,name from employers")}
    relationships = list(
        connection.execute(
            "select parent_id,child_id,relation_type,evidence_url,quote,evidence_date,status,provenance "
            "from relationships order by created_at"
        )
    )
    connection.close()
    with (ROOT / "relationship_review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["母公司", "子公司", "关系类型", "状态", "证据URL", "证据日期", "原文片段", "证据记录"]
        )
        for row in relationships:
            writer.writerow(
                [
                    names.get(row["parent_id"], row["parent_id"]),
                    names.get(row["child_id"], row["child_id"]),
                    row["relation_type"],
                    row["status"],
                    row["evidence_url"],
                    row["evidence_date"],
                    row["quote"],
                    row["provenance"],
                ]
            )

    rows = [json.loads(line) for line in (ROOT / "employers.jsonl").read_text().splitlines()]
    checks = []
    for row in rows:
        path = row.get("证据路径", "")
        if not path:
            continue
        file = ROOT / path
        if not file.is_file() or file.suffix.lower() != ".html":
            continue
        text = norm(
            BeautifulSoup(file.read_text(encoding="utf-8", errors="replace"), "html.parser").get_text()
        )
        checks.append(
            {
                "企业": row["原始名称"],
                "来源名称": row.get("来源名称", ""),
                "证据路径": path,
                "证据页URL": row.get("证据页URL", ""),
                "企业名在页内": norm(row["原始名称"]) in text,
                "来源名在页内": norm(row.get("来源名称", "")) in text if row.get("来源名称") else None,
            }
        )
    with (ROOT / "registry_page_check.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(checks[0]) if checks else ["企业"])
        writer.writeheader()
        writer.writerows(checks)
    print(
        json.dumps(
            {
                "relationships": len(relationships),
                "confirmed": sum(row["status"] == "confirmed" for row in relationships),
                "needs_review": sum(row["status"] != "confirmed" for row in relationships),
                "employer_evidence_checks": len(checks),
                "name_missing": sum(not row["企业名在页内"] for row in checks),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
