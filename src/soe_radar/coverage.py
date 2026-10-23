"""Versioned official enterprise lists and conservative coverage accounting."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select

from .models import Employer, Opportunity, RegistrySnapshot, RegistryState, SourceState, stamp
from .schemas import local_now
from .urls import on_domains, public_url

COMPANY_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·\[\]【】—－\-]{4,120}"
    r"(?:公司|集团|银行|出版社|报刊社|总局)"
    r"(?:[（(\[][^（）()\[\]]{2,80}[）)\]])?"
)


def _entry(name, *, official_url="", profile_url="", rank=None, quote=""):
    return {
        "name": name,
        "official_url": official_url,
        "profile_url": profile_url,
        "rank": rank,
        "quote": quote or name,
    }


def _classify_target(target, base_url):
    """Separate an enterprise's own site from a government profile page."""
    if not target:
        return "", ""
    try:
        target = public_url(target)
    except ValueError:
        return "", ""
    if urlsplit(target).netloc == urlsplit(base_url).netloc:
        return "", target
    return target, ""


def _link_target(node, base_url, name=""):
    anchors = node.select("a[href]") if node.name != "a" else [node]
    for link in anchors:
        href = (link.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if name and name not in link.get_text(" ", strip=True):
            continue
        try:
            target = public_url(urljoin(base_url, href))
        except ValueError:
            continue
        return _classify_target(target, base_url)
    return "", ""


def _is_company(name, *, strict=True):
    if not name or "集体" in name:
        return False
    if COMPANY_RE.fullmatch(name):
        return True
    # A few official names append a bracketed alias, e.g. 中国旅游集团...[香港中旅...].
    if not strict and re.search(r"公司|集团|银行|出版社|报刊社|总局", name):
        return True
    return False


def parse_registry(html, spec):
    soup = BeautifulSoup(html, "html.parser")
    entries = {}
    if spec.adapter in ("links", "numbered", "text"):
        if not spec.entry_selector:
            raise ValueError("非表格名录必须配置正文选择器，不能从全站猜测企业名单")
        for node in soup.select(spec.entry_selector):
            if spec.adapter == "text":
                # Prefer one anchor per enterprise; fall back to whitespace-separated plain text.
                anchors = [
                    anchor
                    for anchor in node.select("a[href]")
                    if _is_company(anchor.get_text(" ", strip=True).strip())
                ]
                if anchors:
                    for anchor in anchors:
                        name = anchor.get_text(" ", strip=True).strip()
                        name = re.sub(r"^\s*\d{1,5}\s*[.、．)]\s*", "", name).lstrip("（(【[")
                        if not _is_company(name):
                            continue
                        official_url, profile_url = _link_target(anchor, spec.url, name)
                        entries[name] = _entry(
                            name,
                            official_url=official_url,
                            profile_url=profile_url,
                            quote=name,
                        )
                    continue
                for token in node.get_text(" ", strip=True).split():
                    name = re.sub(r"^\s*\d{1,5}\s*[.、．)]\s*", "", token)
                    if name.startswith(("（", "(", "【", "[")):
                        name = name.lstrip("（(【[").rstrip("）)】]")
                    if not _is_company(name):
                        continue
                    entries[name] = _entry(name, quote=token)
                continue
            values = (
                [node.get_text(" ", strip=True)]
                if spec.adapter == "links"
                else node.get_text("\n", strip=True).splitlines()
            )
            for value in values:
                name = re.sub(r"^\s*\d{1,5}\s*[.、．)]\s*", "", value).strip()
                name = name.lstrip("（(【[")
                if not _is_company(name):
                    continue
                official_url, profile_url = _link_target(node, spec.url, name)
                entries[name] = _entry(
                    name,
                    official_url=official_url,
                    profile_url=profile_url,
                    quote=value.strip(),
                )
    if spec.adapter == "table":
        for row in soup.select("table tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            texts = [re.sub(r"\s+", "", cell.get_text(" ", strip=True)) for cell in cells]
            # Two-column layouts (e.g. SASAC, Hunan) put rank/name pairs side by side.
            # Scan adjacent pairs so blank spacer cells and multi-pair rows are both handled.
            for index in range(len(cells) - 1):
                rank, name = texts[index], texts[index + 1]
                if not re.fullmatch(r"\d{1,5}", rank) or not _is_company(name, strict=False):
                    continue
                link = cells[index + 1].select_one("a[href]")
                official_url, profile_url = _link_target(link, spec.url) if link else ("", "")
                entries[name] = _entry(
                    name,
                    official_url=official_url,
                    profile_url=profile_url,
                    rank=int(rank),
                    quote=row.get_text(" ", strip=True),
                )
            # Explicit columns remain supported for non-adjacent or unusual layouts.
            for col in spec.name_columns:
                if col < 1 or len(cells) <= col:
                    continue
                rank, name = texts[col - 1], texts[col]
                if not re.fullmatch(r"\d{1,5}", rank) or not _is_company(name, strict=False):
                    continue
                link = cells[col].select_one("a[href]")
                official_url, profile_url = _link_target(link, spec.url) if link else ("", "")
                entries.setdefault(
                    name,
                    _entry(
                        name,
                        official_url=official_url,
                        profile_url=profile_url,
                        rank=int(rank),
                        quote=row.get_text(" ", strip=True),
                    ),
                )
    entries = {name: entry for name, entry in entries.items() if name not in spec.exclude_names}
    if len(entries) < spec.min_entries:
        raise ValueError(f"名录未取得足够的编号企业行（{len(entries)}/{spec.min_entries}）；保留已有版本")
    return sorted(entries.values(), key=lambda x: x["name"])


def publication_date(html):
    """Read an explicit page publication date; never infer it from fetch time or news lists."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    explicit = re.search(
        r"(?:发布时间|发布日期|时间|日期)\s*[：:]\s*(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})",
        text[:20000],
    )
    if explicit:
        return f"{int(explicit.group(1)):04d}-{int(explicit.group(2)):02d}-{int(explicit.group(3)):02d}"
    for node in soup.select("time[datetime],.publish-time,.publish_time,.con_time,.article-time"):
        candidate = node.get("datetime") or node.get_text(" ", strip=True)
        match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", candidate)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    meta = soup.select_one(
        'meta[name="PubDate"],meta[name="publishdate"],meta[property="article:published_time"]'
    )
    if meta:
        match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", meta.get("content", ""))
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return ""


def save_registry(
    db,
    cfg,
    spec,
    html,
    *,
    pages=None,
    status="success",
    publication=None,
    error="",
    provenance=None,
):
    if not spec.url or not on_domains(spec.url, spec.domains):
        raise ValueError("名录缺少已确认的官方入口")
    entries = parse_registry(html, spec)
    return _save_entries(
        db,
        cfg,
        spec,
        entries,
        html.encode(),
        spec.url,
        "html",
        status,
        pages=pages,
        publication=publication or publication_date(html),
        error=error,
        provenance=provenance,
    )


def import_registry_csv(db, cfg, spec, content, proof_url, *, confirmed=False):
    import csv
    import io

    if not confirmed:
        raise ValueError("导入底册前需明确确认已核对官方来源")
    proof_url = public_url(proof_url)
    if spec.domains and not on_domains(proof_url, spec.domains):
        raise ValueError("依据链接不在此名录配置的官方域名范围")
    if not spec.domains and not on_domains(proof_url, ["gov.cn"]):
        raise ValueError("未配置的地区名录需要政府官方网站依据；其他主体请先配置 domains")
    if len(content) > 5_000_000:
        raise ValueError("底册 CSV 超过 5MB")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = content.decode("gb18030")
    reader = csv.DictReader(io.StringIO(decoded))
    entries = {}
    for i, row in enumerate(reader):
        if i >= 20000:
            raise ValueError("每次最多导入 20,000 行，不能静默截断")
        name = next(
            (
                row.get(k, "").strip()
                for k in ["企业名称", "公司名称", "企业(集团)名称", "name"]
                if row.get(k)
            ),
            "",
        )
        if not name:
            raise ValueError(f"第 {i + 2} 行缺少企业名称；支持 企业名称/公司名称/name 列")
        if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9（）()·\[\]【】—－\-\s]{4,120}", name):
            raise ValueError(f"第 {i + 2} 行企业名称格式不正确")
        site = row.get("官网地址") or row.get("official_url") or ""
        entries[name] = {
            "name": name,
            "official_url": public_url(site) if site else "",
            "verification": "human_confirmed_import",
        }
    if not entries:
        raise ValueError("没有企业记录，请检查 CSV 表头")
    return _save_entries(
        db,
        cfg,
        spec,
        sorted(entries.values(), key=lambda x: x["name"]),
        content,
        proof_url,
        "csv",
        "imported",
    )


def _save_entries(
    db,
    cfg,
    spec,
    entries,
    raw,
    source_url,
    extension,
    status,
    *,
    provenance=None,
    pages=None,
    publication=None,
    error="",
):
    folder = cfg.data_dir / "registries"
    folder.mkdir(parents=True, exist_ok=True)
    page_records = []
    if pages:
        digest_input = b""
        for page in pages:
            page_raw = page["raw"]
            page_digest = hashlib.sha256(page_raw).hexdigest()
            page_path = folder / (page_digest + "." + extension)
            if not page_path.exists():
                page_path.write_bytes(page_raw)
            page_records.append(
                {
                    "url": page["url"],
                    "sha256": page_digest,
                    "path": str(page_path.relative_to(cfg.data_dir)),
                    "fetched_at": stamp(),
                }
            )
            digest_input += page["url"].encode() + b"\0" + page_raw + b"\0"
        digest = hashlib.sha256(digest_input).hexdigest()
    else:
        digest = hashlib.sha256(source_url.encode() + b"\0" + raw).hexdigest()
        path = folder / (digest + "." + extension)
        if not path.exists():
            path.write_bytes(raw)
        page_records = [
            {
                "url": source_url,
                "sha256": digest,
                "path": str(path.relative_to(cfg.data_dir)),
                "fetched_at": stamp(),
            }
        ]
    raw_path = page_records[0]["path"]
    details = dict(
        provenance
        or {
            "acquisition": "direct" if status == "success" else "manual_csv",
            "scope": spec.scope,
            "complete": False,
        }
    )
    details.setdefault("scope", spec.scope)
    details.setdefault("complete", False)
    details["pages"] = page_records
    details["pagination_complete"] = len(page_records) > 1 and not error
    if publication:
        details["published_at"] = publication
    if error:
        details["fetch_error"] = error[:300]
    with db.session() as s:
        snapshot = s.scalar(
            select(RegistrySnapshot).where(
                RegistrySnapshot.registry_id == spec.id, RegistrySnapshot.digest == digest
            )
        )
        if not snapshot:
            snapshot = RegistrySnapshot(
                registry_id=spec.id,
                digest=digest,
                source_url=source_url,
                entries=entries,
                raw_path=raw_path,
                provenance=details,
            )
            s.add(snapshot)
            s.flush()
        else:
            # Same content can gain better provenance without creating a duplicate version.
            snapshot.provenance = details
        state = s.get(RegistryState, spec.id)
        if not state:
            state = RegistryState(id=spec.id)
            s.add(state)
        # Missing enterprises remain in older versions; never delete their history.
        for entry in entries:
            employer = s.scalar(select(Employer).where(Employer.name == entry["name"]))
            if not employer:
                s.add(Employer(name=entry["name"], org_type="soe", proof_url=source_url, verified_at=stamp()))
            elif employer.org_type == "unknown":
                employer.org_type, employer.proof_url, employer.verified_at = "soe", source_url, stamp()
        state.active_snapshot_id = snapshot.id
        state.status = status
        state.last_success = state.last_attempt = stamp()
        state.last_error = error[:300]
        return {
            "id": snapshot.id,
            "registry_id": spec.id,
            "count": len(entries),
            "digest": digest,
            "pages": len(page_records),
        }


def registry_failure(db, registry_id, reason):
    with db.session() as s:
        state = s.get(RegistryState, registry_id)
        if not state:
            state = RegistryState(id=registry_id)
            s.add(state)
        state.status = "error"
        state.last_attempt = stamp()
        state.last_error = reason[:300]


async def refresh_registries(db, cfg, ids=None, *, fetcher=None):
    from .fetching import Fetcher
    from .models import uid

    selected = [r for r in cfg.registries if ids is None or r.id in ids]
    owner = uid()
    if not db.acquire("registry", owner, seconds=600):
        raise ValueError("已有名录刷新任务持有租约")
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(cfg)
    results = []
    try:
        for spec in selected:
            if not spec.url:
                results.append({"registry_id": spec.id, "status": "missing_entry"})
                continue
            db.acquire("registry", owner, seconds=600)
            pages, errors = [], []
            urls = [spec.url, *spec.page_urls]
            for page_url in urls:
                try:
                    response = await fetcher.get(
                        page_url,
                        spec.domains,
                        network_mode=spec.network_mode,
                        source_id="registry:" + spec.id,
                    )
                    pages.append({"url": str(response.url), "raw": response.content})
                except Exception as exc:
                    from .fetching import FetchFailure

                    reason = str(exc) if isinstance(exc, (ValueError, FetchFailure)) else type(exc).__name__
                    errors.append(f"{page_url}：{reason}")
            if not pages:
                registry_failure(db, spec.id, "；".join(errors) or "没有取得名录页面")
                results.append({"registry_id": spec.id, "status": "error", "error": "；".join(errors)[:300]})
                continue
            combined = "\n".join(page["raw"].decode("utf-8", "replace") for page in pages)
            status = "success" if not errors else "partial"
            try:
                saved = save_registry(
                    db,
                    cfg,
                    spec,
                    combined,
                    pages=pages,
                    status=status,
                    publication=publication_date(pages[0]["raw"].decode("utf-8", "replace")),
                    error="；".join(errors),
                )
                results.append({"status": status, "error": "；".join(errors)[:300], **saved})
            except ValueError as exc:
                registry_failure(db, spec.id, str(exc))
                results.append({"registry_id": spec.id, "status": "error", "error": str(exc)})
        return {"registries": results, "network_attempts": fetcher.network_attempts[-100:]}
    finally:
        if own_fetcher:
            await fetcher.close()
        db.release("registry", owner)


def coverage_report(db, cfg, *, cohort=None):
    cohort = cohort or local_now().year + 1
    fresh_after = (datetime.now(UTC) - timedelta(hours=cfg.coverage_fresh_hours)).isoformat()
    with db.session() as s:
        states = {r.id: r for r in s.scalars(select(RegistryState))}
        snapshots = {r.id: r for r in s.scalars(select(RegistrySnapshot))}
        sources = {r.id: r for r in s.scalars(select(SourceState))}
        employers = list(s.scalars(select(Employer).order_by(Employer.name)))
        opportunities = list(
            s.scalars(
                select(Opportunity).where(
                    Opportunity.merged_into_id.is_(None),
                    Opportunity.review_status.in_(["pending", "auto", "manual"]),
                    Opportunity.recruitment_type == "campus",
                )
            )
        )
        memberships = {}
        membership_evidence = {}
        registries = []
        for spec in cfg.registries:
            state = states.get(spec.id)
            snapshot = snapshots.get(state.active_snapshot_id) if state else None
            if snapshot:
                for entry in snapshot.entries:
                    memberships.setdefault(entry["name"], []).append(spec)
                    membership_evidence.setdefault(entry["name"], []).append(
                        {
                            "registry_id": spec.id,
                            "source_url": snapshot.source_url,
                            "provenance": snapshot.provenance,
                            "declared_domain": entry.get("declared_domain", ""),
                        }
                    )
            registries.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "category": spec.category,
                    "region": spec.region,
                    "url": snapshot.source_url if snapshot else spec.url,
                    "refreshable": bool(spec.url),
                    "status": state.status if state else "unverified" if spec.url else "missing_entry",
                    "count": len(snapshot.entries) if snapshot else None,
                    "snapshot_id": snapshot.id if snapshot else None,
                    "last_success": state.last_success if state else None,
                    "last_error": state.last_error if state else "",
                    "versions": sum(x.registry_id == spec.id for x in snapshots.values()),
                    "provenance": snapshot.provenance if snapshot else {},
                    "scope": spec.scope,
                    "authority_url": spec.authority_url,
                    "authority_proof_url": spec.authority_proof_url,
                }
            )
        rows = []
        for employer in employers:
            member_of = memberships.get(employer.name, [])
            # Exact entity / declared alias matching, never substring or parent inheritance.
            entries = [
                x
                for x in cfg.sources
                if x.enabled
                and x.employer
                and (x.employer == employer.name or x.employer in employer.aliases)
            ]
            own = [o for o in opportunities if o.employer_id == employer.id]
            # Orphan parser guesses from excluded documents cannot define the company universe.
            if not (member_of or entries or own or employer.org_type == "soe" and employer.proof_url):
                continue
            checks = [
                {
                    "id": x.id,
                    "name": x.name,
                    "url": x.homepage_url or x.url,
                    "status": sources[x.id].status if x.id in sources else "unverified",
                    "last_success": sources[x.id].last_success if x.id in sources else None,
                    "error": sources[x.id].last_error if x.id in sources else "",
                }
                for x in entries
            ]
            recent = any(
                x["status"] in ("success", "partial", "no_recruitment")
                and x["last_success"]
                and x["last_success"] >= fresh_after
                for x in checks
            )
            campus = [o for o in own if cohort in o.cohorts]
            verified = [o for o in campus if o.review_status in ("auto", "manual") and not o.needs_review]
            status = (
                "missing_entry"
                if not entries
                else "blocked"
                if checks and all(x["status"] in ("error", "failed") for x in checks)
                else "not_checked"
                if all(not x["last_success"] for x in checks)
                else "stale"
                if not recent
                else "campus_found"
                if campus
                else "not_found"
            )
            rows.append(
                {
                    "id": employer.id,
                    "name": employer.name,
                    "parent_id": employer.parent_id,
                    "registry_ids": [r.id for r in member_of],
                    "registry_evidence": membership_evidence.get(employer.name, []),
                    "categories": sorted({r.category for r in member_of}),
                    "regions": sorted({r.region for r in member_of}),
                    "proof_url": employer.proof_url,
                    "ownership_verified": employer.org_type == "soe" and bool(employer.proof_url),
                    "sources": checks,
                    "discovered_by": sorted({o.source_id for o in own}),
                    "status": status,
                    "recently_checked": recent,
                    "campus_count": len(campus),
                    "verified_count": len(verified),
                    "pending_count": len(campus) - len(verified),
                    "unknown_cohort_count": sum(not o.cohorts for o in own),
                }
            )
    rows.sort(key=lambda r: (r["status"] != "missing_entry", r["status"] != "blocked", r["name"]))
    return {
        "cohort": cohort,
        "national_total": None,
        "national_coverage_rate": None,
        "note": "全国企业及所属单位底册尚未齐备；已知企业和入口数量不代表全国覆盖率。集团入口不自动覆盖子公司。",
        "fresh_hours": cfg.coverage_fresh_hours,
        "registries": registries,
        "summary": {
            "known_entities": len(rows),
            "registry_entities": len(memberships),
            "with_entry": sum(bool(r["sources"]) for r in rows),
            "recently_checked": sum(r["recently_checked"] for r in rows),
            "with_campus": sum(bool(r["campus_count"]) for r in rows),
            "missing_entry": sum(r["status"] == "missing_entry" for r in rows),
            "blocked": sum(r["status"] == "blocked" for r in rows),
            "registry_gaps": sum(r["count"] is None for r in registries),
            "bundled_registries": sum(
                r["provenance"].get("acquisition") == "official_index_excerpt" for r in registries
            ),
        },
        "items": rows,
    }
