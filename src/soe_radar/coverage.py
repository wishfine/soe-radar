"""Versioned official enterprise lists and conservative coverage accounting."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import select

from .models import Employer, Opportunity, RegistrySnapshot, RegistryState, SourceState, stamp
from .schemas import local_now
from .urls import on_domains, public_url


def parse_registry(html, spec):
    soup = BeautifulSoup(html, "html.parser")
    entries = {}
    for row in soup.select("table tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        for col in spec.name_columns:
            if col < 1 or len(cells) <= col:
                continue
            rank = cells[col - 1].get_text(strip=True)
            name = re.sub(r"\s+", "", cells[col].get_text())
            if not re.fullmatch(r"\d{1,5}", rank) or not re.fullmatch(
                r"[\u4e00-\u9fffA-Za-z0-9（）()·\[\]【】—－\-]{4,120}", name
            ):
                continue
            if not re.search(r"公司|集团|银行|出版社|报刊社", name):
                continue
            link = cells[col].select_one("a[href]")
            official_url = ""
            if link:
                try:
                    official_url = public_url(urljoin(spec.url, link.get("href", "")))
                except ValueError:
                    pass
            entries[name] = {"name": name, "official_url": official_url}
    if len(entries) < spec.min_entries:
        raise ValueError(f"名录未取得足够的编号企业行（{len(entries)}/{spec.min_entries}）；保留已有版本")
    return sorted(entries.values(), key=lambda x: x["name"])


def save_registry(db, cfg, spec, html):
    if not spec.url or not on_domains(spec.url, spec.domains):
        raise ValueError("名录缺少已确认的官方入口")
    entries = parse_registry(html, spec)
    return _save_entries(db, cfg, spec, entries, html.encode(), spec.url, "html", "success")


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


def _save_entries(db, cfg, spec, entries, raw, source_url, extension, status):
    digest = hashlib.sha256(source_url.encode() + b"\0" + raw).hexdigest()
    folder = cfg.data_dir / "registries"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + "." + extension)
    if not path.exists():
        path.write_bytes(raw)
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
                raw_path=str(path.relative_to(cfg.data_dir)),
            )
            s.add(snapshot)
            s.flush()
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
        state.last_error = ""
        return {"id": snapshot.id, "registry_id": spec.id, "count": len(entries), "digest": digest}


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
            try:
                response = await fetcher.get(
                    spec.url, spec.domains, network_mode=spec.network_mode, source_id="registry:" + spec.id
                )
                results.append({"status": "success", **save_registry(db, cfg, spec, response.text)})
            except Exception as exc:
                # Only local, controlled failure messages are safe to retain.
                from .fetching import FetchFailure

                reason = str(exc) if isinstance(exc, (ValueError, FetchFailure)) else type(exc).__name__
                registry_failure(db, spec.id, reason)
                results.append({"registry_id": spec.id, "status": "error", "error": reason})
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
        registries = []
        for spec in cfg.registries:
            state = states.get(spec.id)
            snapshot = snapshots.get(state.active_snapshot_id) if state else None
            if snapshot:
                for entry in snapshot.entries:
                    memberships.setdefault(entry["name"], []).append(spec)
            registries.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "category": spec.category,
                    "region": spec.region,
                    "url": spec.url or (snapshot.source_url if snapshot else ""),
                    "refreshable": bool(spec.url),
                    "status": state.status if state else "unverified" if spec.url else "missing_entry",
                    "count": len(snapshot.entries) if snapshot else None,
                    "snapshot_id": snapshot.id if snapshot else None,
                    "last_success": state.last_success if state else None,
                    "last_error": state.last_error if state else "",
                    "versions": sum(x.registry_id == spec.id for x in snapshots.values()),
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
        },
        "items": rows,
    }
