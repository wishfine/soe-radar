"""Offline reviewer probes. Run: uv run python delivery/review_repro.py.

Uses temporary databases only. Prints defects; does not touch personal data.
"""

import asyncio
import csv
import hashlib
import json
import re
import tempfile
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from soe_radar.config import RegistryConfig, Settings
from soe_radar.coverage import coverage_report, parse_registry, refresh_registries, save_registry
from soe_radar.db import initialize
from soe_radar.fetching import FetchFailure, extract_page
from soe_radar.models import Employer, RegistrySnapshot


def emit(probe, expected, actual):
    print(
        json.dumps(
            {"probe": probe, "expected": expected, "actual": actual, "passes": expected == actual},
            ensure_ascii=False,
        )
    )


with tempfile.TemporaryDirectory() as folder:
    cfg = Settings.model_validate(
        {
            "data_dir": folder,
            "registries": [
                {
                    "id": "demo",
                    "name": "示例",
                    "category": "local",
                    "url": "https://example.gov.cn/list",
                    "domains": ["example.gov.cn"],
                    "page_urls": ["https://example.gov.cn/list_1"],
                    "min_entries": 1,
                }
            ],
        }
    )
    db = initialize(cfg)
    spec = cfg.registries[0]
    a = "<table><tr><td>1</td><td>示例一有限公司</td></tr></table>"
    b = "<table><tr><td>2</td><td>示例二有限公司</td></tr></table>"
    save_registry(
        db,
        cfg,
        spec,
        a + b,
        pages=[
            {"url": spec.url, "raw": a.encode()},
            {"url": spec.page_urls[0], "raw": b.encode()},
        ],
    )

    class MockFetcher:
        network_attempts = []

        async def get(self, url, *args, **kwargs):
            if url.endswith("_1"):
                raise FetchFailure("HTTP 503")
            return httpx.Response(200, text=a, request=httpx.Request("GET", url))

    asyncio.run(refresh_registries(db, cfg, fetcher=MockFetcher()))
    emit(
        "failed_page_preserves_known_membership", 2, coverage_report(db, cfg)["summary"]["registry_entities"]
    )
    saved = save_registry(db, cfg, spec, a)
    with db.session() as s:
        snap = s.get(RegistrySnapshot, saved["id"])
        page = snap.provenance["pages"][0]
        emit(
            "single_page_content_sha256",
            hashlib.sha256((cfg.data_dir / page["path"]).read_bytes()).hexdigest(),
            page["sha256"],
        )
        s.add(Employer(name="中国旅游集团有限公司", aliases=["香港中旅（集团）有限公司"]))
    save_registry(
        db,
        cfg,
        spec,
        "<table><tr><td>1</td><td>中国旅游集团有限公司[香港中旅（集团）有限公司]</td></tr></table>",
    )
    with db.session() as s:
        rows = [x for x in s.scalars(select(Employer)) if x.name.startswith("中国旅游集团有限公司")]
        emit("one_legal_entity_despite_bracket_alias", 1, len(rows))
    db.engine.dispose()

spec = RegistryConfig(
    id="mixed",
    name="混合名单",
    category="local",
    url="https://example.gov.cn/list",
    domains=["example.gov.cn"],
    adapter="text",
    entry_selector="#list",
)
rows = parse_registry(
    '<div id="list"><a href="https://one.example">示例一集团有限公司</a>'
    "<p>示例二集团有限公司</p><p>示例三集团有限公司</p></div>",
    spec,
)
emit("mixed_link_plaintext_directory", 3, len(rows))
poster = "https://example.gov.cn/images/20261023/123456.jpg"
page = extract_page(
    "https://example.gov.cn/recruit",
    "<article><h1>示例公司2027校园招聘</h1><p>报名时间详见下方招聘海报</p>"
    f'<img src="{poster}" width="1000" height="2000"></article>',
)
emit("unlabelled_body_poster_retained", True, any(a["url"] == poster for a in page.attachments))

root = Path(__file__).parent
manifest = list(csv.DictReader((root / "evidence/manifest.csv").open(encoding="utf-8-sig")))
emit(
    "delivered_file_hash_mismatches",
    0,
    sum(hashlib.sha256((root / row["文件"]).read_bytes()).hexdigest() != row["SHA256"] for row in manifest),
)


def norm(value):
    return re.sub(r"\s+", "", value)


wrong_pages = []
for line in (root / "employers.jsonl").read_text().splitlines():
    row = json.loads(line)
    path = root / row["证据路径"] if row["证据路径"] else None
    if path and path.is_file() and path.suffix == ".html":
        text = BeautifulSoup(path.read_text(), "html.parser").get_text()
        if norm(row["原始名称"]) not in norm(text):
            wrong_pages.append(row["原始名称"])
emit("employer_names_absent_from_bound_evidence", 0, len(wrong_pages))
