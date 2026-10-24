"""Compare the current database with the pre-round backup. Offline; no network/model calls."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tarfile
import tempfile
from pathlib import Path


def read_db(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "select id, source_id, title, cohorts, season, recruitment_type, review_status, "
        "published_at, official_url from opportunities"
    ).fetchall()
    connection.close()
    return {row["id"]: dict(row) for row in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=Path("backups/before-nationwide-20261023.tar.gz"))
    parser.add_argument("--database", type=Path, default=Path("data/radar.db"))
    parser.add_argument("--out", type=Path, default=Path("delivery/activity_diff.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="soe-diff-") as folder:
        with tarfile.open(args.baseline, "r:gz") as archive:
            archive.extract("radar.db", path=folder, filter="data")
        baseline = read_db(Path(folder) / "radar.db")
    current = read_db(args.database)
    new_ids = sorted(set(current) - set(baseline))
    removed_ids = sorted(set(baseline) - set(current))
    new_rows = [current[value] for value in new_ids]
    campus = [row for row in new_rows if row["recruitment_type"] == "campus"]
    campus_2027 = [row for row in campus if "2027" in (row["cohorts"] or "")]
    missing = [
        row
        for row in campus_2027
        if not row["official_url"] or not row["title"] or not row["source_id"]
    ]
    result = {
        "baseline": str(args.baseline),
        "database": str(args.database),
        "counts": {
            "baseline_opportunities": len(baseline),
            "current_opportunities": len(current),
            "new_opportunity_ids": len(new_ids),
            "removed_or_merged_ids": len(removed_ids),
            "new_campus": len(campus),
            "new_campus_2027": len(campus_2027),
        },
        "new_campus_2027": [
            {
                "id": row["id"],
                "source_id": row["source_id"],
                "title": row["title"],
                "cohorts": row["cohorts"],
                "season": row["season"],
                "review_status": row["review_status"],
                "published_at": row["published_at"],
                "official_url": row["official_url"],
            }
            for row in sorted(campus_2027, key=lambda x: (x["published_at"] or "", x["id"]))
        ],
        "new_campus_all_ids": [row["id"] for row in campus],
        "removed_or_merged_ids": removed_ids,
        "missing_required_fields": [row["id"] for row in missing],
        "note": "新增 ID 差集只证明数据库变化，不证明全国招聘覆盖；未取得的企业仍在 gaps.csv。",
    }
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
