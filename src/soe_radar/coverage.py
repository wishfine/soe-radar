"""Versioned official enterprise lists and conservative coverage accounting."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select

from .extraction import PARSER_VERSION
from .models import (
    CoverageTask,
    Employer,
    Opportunity,
    RegistrySnapshot,
    RegistryState,
    SourceCandidate,
    SourceEmployerScope,
    SourceState,
    stamp,
)
from .schemas import local_now
from .urls import on_domains, public_url

COMPANY_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·\[\]【】—－\-]{4,120}"
    r"(?:公司|集团|银行|出版社|报刊社|总局)"
    r"(?:[（(\[][^（）()\[\]]{2,80}[）)\]])?"
)
ALIAS_RE = re.compile(r"^(.+?(?:公司|集团|银行|出版社|报刊社|总局))[\[（(](.+)[\]）)]$")


def split_alias(name):
    """Split a display form like 中国旅游集团...[香港中旅...] into legal name and aliases."""
    value = re.sub(r"\s+", "", name or "")
    match = ALIAS_RE.fullmatch(value)
    if not match:
        return value, []
    canonical, alias = match.group(1).strip(), match.group(2).strip()
    aliases = [alias]
    if value != canonical:
        aliases.append(value)
    return canonical, aliases


def _entry(name, *, official_url="", profile_url="", rank=None, quote=""):
    canonical, aliases = split_alias(name)
    return {
        "name": canonical,
        "source_name": name,
        "aliases": aliases,
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


def parse_registry(html, spec, *, enforce_min=True):
    soup = BeautifulSoup(html, "html.parser")
    entries = {}
    if spec.adapter in ("links", "numbered", "text"):
        if not spec.entry_selector:
            raise ValueError("非表格名录必须配置正文选择器，不能从全站猜测企业名单")
        for node in soup.select(spec.entry_selector):
            if spec.adapter == "text":
                # A directory can mix linked enterprises and plain-text enterprises in one node.
                anchors = [
                    anchor
                    for anchor in node.select("a[href]")
                    if _is_company(anchor.get_text(" ", strip=True).strip())
                ]
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
                # Remove anchors from a copy before reading the remaining plain text.
                clone = BeautifulSoup(str(node), "html.parser")
                for anchor in clone.select("a[href]"):
                    anchor.decompose()
                plain_text = clone.get_text(" ", strip=True)
                for token in plain_text.split():
                    name = re.sub(r"^\s*\d{1,5}\s*[.、．)]\s*", "", token)
                    if name.startswith(("（", "(", "【", "[")):
                        name = name.lstrip("（(【[").rstrip("）)】]")
                    if not _is_company(name):
                        continue
                    entries.setdefault(name, _entry(name, quote=token))
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
    if enforce_min and len(entries) < spec.min_entries:
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
    activate=True,
):
    if not spec.url or not on_domains(spec.url, spec.domains):
        raise ValueError("名录缺少已确认的官方入口")
    if pages:
        return save_registry_pages(
            db,
            cfg,
            spec,
            pages,
            status=status,
            publication=publication,
            error=error,
            provenance=provenance,
            activate=activate,
        )
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
        publication=publication or publication_date(html),
        error=error,
        provenance=provenance,
        activate=activate,
    )


def save_registry_pages(
    db,
    cfg,
    spec,
    pages,
    *,
    status="success",
    publication=None,
    error="",
    provenance=None,
    activate=True,
):
    """Parse each page separately so every enterprise keeps its own page evidence."""
    merged = {}
    for index, page in enumerate(pages):
        text = page["raw"].decode("utf-8", "replace")
        page_digest = hashlib.sha256(page["raw"]).hexdigest()
        parsed = parse_registry(text, spec, enforce_min=False)
        for entry in parsed:
            entry["page_url"] = page["url"]
            entry["page_index"] = index
            entry["page_sha256"] = page_digest
            existing = merged.get(entry["name"])
            if existing:
                existing["aliases"] = list(
                    dict.fromkeys([*existing.get("aliases", []), *entry.get("aliases", [])])
                )
            else:
                merged[entry["name"]] = entry
    entries = sorted(merged.values(), key=lambda x: x["name"])
    if len(entries) < spec.min_entries:
        raise ValueError(f"名录未取得足够的编号企业行（{len(entries)}/{spec.min_entries}）；保留已有版本")
    combined = b"\n".join(page["raw"] for page in pages)
    return _save_entries(
        db,
        cfg,
        spec,
        entries,
        combined,
        spec.url,
        "html",
        status,
        pages=pages,
        publication=publication or publication_date(pages[0]["raw"].decode("utf-8", "replace")),
        error=error,
        provenance=provenance,
        activate=activate,
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


def snapshot_digest(pages):
    """Version identity over every page URL/content and the parser version."""
    digest = hashlib.sha256(PARSER_VERSION.encode() + b"\0")
    for page in pages:
        digest.update(page["url"].encode() + b"\0" + page["raw"] + b"\0")
    return digest.hexdigest()


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
    activate=True,
):
    folder = cfg.data_dir / "registries"
    folder.mkdir(parents=True, exist_ok=True)
    page_records = []
    if pages:
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
    else:
        content_digest = hashlib.sha256(raw).hexdigest()
        path = folder / (content_digest + "." + extension)
        if not path.exists():
            path.write_bytes(raw)
        page_records = [
            {
                "url": source_url,
                "sha256": content_digest,
                "path": str(path.relative_to(cfg.data_dir)),
                "fetched_at": stamp(),
            }
        ]
    snapshot_id_digest = snapshot_digest(pages or [{"url": source_url, "raw": raw}])
    raw_path = page_records[0]["path"]
    path_by_sha = {record["sha256"]: record for record in page_records}
    for entry in entries:
        entry_digest = entry.get("page_sha256") or page_records[0]["sha256"]
        record = path_by_sha.get(entry_digest, page_records[0])
        entry.setdefault("page_url", record["url"])
        entry.setdefault("page_sha256", record["sha256"])
        entry["page_path"] = record["path"]
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
    entries_fingerprint = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    with db.session() as s:
        digest = snapshot_id_digest
        snapshot = s.scalar(
            select(RegistrySnapshot).where(
                RegistrySnapshot.registry_id == spec.id, RegistrySnapshot.digest == digest
            )
        )
        if snapshot and snapshot.entries != entries:
            # Same source bytes with a different parse result must not reuse stale entries.
            digest = hashlib.sha256((snapshot_id_digest + entries_fingerprint).encode()).hexdigest()
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
            # Same identity and same parsed entries can gain better provenance.
            snapshot.provenance = details
        state = s.get(RegistryState, spec.id)
        if not state:
            state = RegistryState(id=spec.id)
            s.add(state)
        # Match canonical names and confirmed aliases; never create a second identity for a display alias.
        existing = list(s.scalars(select(Employer)))
        by_name = {row.name: row for row in existing}
        by_alias = {}
        for row in existing:
            for alias in row.aliases or []:
                by_alias.setdefault(alias, row)
        trusted = details.get("tls_verified", True)
        for entry in entries:
            canonical = entry["name"]
            aliases = [value for value in entry.get("aliases", []) if value]
            source_name = entry.get("source_name", canonical)
            employer = by_name.get(canonical) or by_alias.get(canonical)
            if not employer:
                for alias in aliases:
                    employer = by_name.get(alias) or by_alias.get(alias)
                    if employer:
                        break
            if not employer:
                employer = Employer(
                    name=canonical,
                    aliases=aliases,
                    org_type="soe" if trusted else "unknown",
                    proof_url=source_url,
                    verified_at=stamp() if trusted else None,
                )
                s.add(employer)
                by_name[canonical] = employer
            else:
                merged = list(dict.fromkeys([*(employer.aliases or []), *aliases, source_name]))
                employer.aliases = [value for value in merged if value != employer.name]
                if trusted:
                    if employer.org_type == "unknown":
                        employer.org_type, employer.proof_url, employer.verified_at = (
                            "soe",
                            source_url,
                            stamp(),
                        )
                elif employer.proof_url == source_url and employer.org_type == "soe":
                    # A manual/unverified-TLS list must not silently promote ownership verification.
                    employer.org_type, employer.verified_at = "unknown", None
            for alias in aliases + ([source_name] if source_name != canonical else []):
                by_alias.setdefault(alias, employer)
        if not trusted:
            other_names = set()
            for other in cfg.registries:
                if other.id == spec.id:
                    continue
                other_state = s.get(RegistryState, other.id)
                other_snapshot = (
                    s.get(RegistrySnapshot, other_state.active_snapshot_id)
                    if other_state and other_state.active_snapshot_id
                    else None
                )
                if other_snapshot:
                    other_names.update(entry["name"] for entry in other_snapshot.entries)
            source_names = set()
            for source in cfg.sources:
                if source.employer:
                    source_names.add(source.employer)
                    source_names.update(source.aliases or [])
            for entry in entries:
                employer = by_name.get(entry["name"]) or by_alias.get(entry["name"])
                if not employer:
                    continue
                covered = employer.name in other_names or employer.name in source_names
                covered = covered or any(alias in source_names for alias in (employer.aliases or []))
                if not covered and employer.proof_url and on_domains(employer.proof_url, spec.domains):
                    employer.org_type, employer.verified_at = "unknown", None
        if activate:
            state.active_snapshot_id = snapshot.id
        state.status = status
        state.last_attempt = stamp()
        if activate and status in ("success", "imported", "partial"):
            state.last_success = state.last_attempt
        state.last_error = error[:300]
        return {
            "id": snapshot.id,
            "registry_id": spec.id,
            "count": len(entries),
            "digest": digest,
            "pages": len(page_records),
            "activated": activate,
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


def repair_registry_snapshots(db, *, apply=False):
    """Recompute version digests and repair per-entry page bindings, idempotently."""

    def normalized(value):
        return "".join((value or "").split())

    plans = []
    with db.session() as s:
        snapshots = list(s.scalars(select(RegistrySnapshot)))
        for snapshot in snapshots:
            pages = (snapshot.provenance or {}).get("pages") or []
            if not pages:
                continue
            raw_pages = []
            page_text = {}
            for page in pages:
                source = Path(db.path).parent / page.get("path", "")
                if not source.is_file():
                    raw_pages = []
                    break
                raw = source.read_bytes()
                raw_pages.append({"url": page["url"], "raw": raw})
                page_text[page["sha256"]] = normalized(
                    BeautifulSoup(raw.decode("utf-8", "replace"), "html.parser").get_text(" ")
                )
            if not raw_pages:
                continue
            correct = snapshot_digest(raw_pages)
            path_by_sha = {page["sha256"]: page for page in pages}
            path_by_url = {page["url"]: page for page in pages}
            repaired_entries = []
            binding_changes = 0
            unresolved = 0
            changed = False
            for entry in snapshot.entries:
                item = dict(entry)
                name = normalized(item.get("source_name") or item.get("name", ""))
                matches = [
                    candidate
                    for candidate in pages
                    if name and name in page_text.get(candidate["sha256"], "")
                ]
                existing = path_by_sha.get(item.get("page_sha256")) or path_by_url.get(item.get("page_url"))
                if existing and existing in matches:
                    page = existing
                    reason = ""
                elif len(matches) == 1:
                    page = matches[0]
                    reason = ""
                elif len(matches) > 1:
                    page = None
                    reason = "企业名称在多页出现，无法唯一定位证据页"
                else:
                    page = None
                    reason = "所有保留原件均无法定位企业名称"
                if page:
                    before = (
                        item.get("page_url"),
                        item.get("page_sha256"),
                        item.get("page_path"),
                        item.get("page_binding"),
                        item.get("page_binding_reason"),
                        item.get("page_binding_candidates"),
                    )
                    item["page_path"] = page["path"]
                    item["page_sha256"] = page["sha256"]
                    item["page_url"] = page["url"]
                    item.pop("page_binding", None)
                    item.pop("page_binding_reason", None)
                    item.pop("page_binding_candidates", None)
                    after = (
                        item.get("page_url"),
                        item.get("page_sha256"),
                        item.get("page_path"),
                        item.get("page_binding"),
                        item.get("page_binding_reason"),
                        item.get("page_binding_candidates"),
                    )
                    if before != after:
                        binding_changes += 1
                        changed = True
                else:
                    old = {
                        "url": item.get("page_url", ""),
                        "sha256": item.get("page_sha256", ""),
                        "path": item.get("page_path", ""),
                    }
                    candidates = list(item.get("page_binding_candidates") or [])
                    if any(old.values()) and old not in candidates:
                        candidates.append({**old, "reason": reason})
                    before = (
                        item.get("page_binding"),
                        item.get("page_binding_reason"),
                        item.get("page_binding_candidates"),
                        item.get("page_url"),
                        item.get("page_sha256"),
                        item.get("page_path"),
                    )
                    item.pop("page_url", None)
                    item.pop("page_sha256", None)
                    item.pop("page_path", None)
                    item["page_binding"] = "needs_review"
                    item["page_binding_reason"] = reason
                    item["page_binding_candidates"] = candidates
                    after = (
                        item.get("page_binding"),
                        item.get("page_binding_reason"),
                        item.get("page_binding_candidates"),
                        item.get("page_url"),
                        item.get("page_sha256"),
                        item.get("page_path"),
                    )
                    unresolved += 1
                    if before != after:
                        changed = True
                repaired_entries.append(item)
            repaired = (snapshot.provenance or {}).get("digest_repaired_from")
            digest_changed = snapshot.digest != correct and not repaired
            if digest_changed or changed:
                plans.append(
                    {
                        "id": snapshot.id,
                        "registry_id": snapshot.registry_id,
                        "old_digest": snapshot.digest,
                        "new_digest": correct,
                        "digest_changed": digest_changed,
                        "binding_changes": binding_changes,
                        "unresolved_entries": unresolved,
                        "entries": repaired_entries,
                        "pages": pages,
                        "old_provenance": dict(snapshot.provenance or {}),
                    }
                )
        if not apply:
            return [
                {
                    key: value
                    for key, value in plan.items()
                    if key not in ("entries", "pages", "old_provenance")
                }
                for plan in plans
            ]
        for plan in plans:
            snapshot = s.get(RegistrySnapshot, plan["id"])
            correct = plan["new_digest"]
            if plan["digest_changed"]:
                existing = s.scalar(
                    select(RegistrySnapshot).where(
                        RegistrySnapshot.registry_id == snapshot.registry_id,
                        RegistrySnapshot.digest == correct,
                        RegistrySnapshot.id != snapshot.id,
                    )
                )
                if existing:
                    correct = hashlib.sha256((correct + snapshot.id).encode()).hexdigest()
                provenance = dict(snapshot.provenance or {})
                provenance["digest_repaired_from"] = plan["old_digest"]
                provenance["digest_repair_note"] = (
                    "旧版整页摘要被末页摘要覆盖；按全部页面内容与解析版本重算。"
                )
                snapshot.digest = correct
                snapshot.provenance = provenance
            provenance = dict(snapshot.provenance or {})
            provenance["page_binding_repaired"] = plan["binding_changes"]
            if plan["unresolved_entries"]:
                provenance["page_binding_unresolved"] = plan["unresolved_entries"]
            snapshot.entries = plan["entries"]
            snapshot.provenance = provenance
        return [
            {key: value for key, value in plan.items() if key not in ("entries", "pages", "old_provenance")}
            for plan in plans
        ]


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
            with db.session() as session:
                current = session.get(RegistryState, spec.id)
                has_active = bool(current and current.active_snapshot_id)
            # Never replace a known membership set with a partial page set. The failed
            # pages may contain enterprises that are absent from this fetch.
            activate = not errors or not has_active
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
                    activate=activate,
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
        task_counts = {}
        for task in s.scalars(select(CoverageTask)):
            task_counts[task.status] = task_counts.get(task.status, 0) + 1
        candidate_counts = {}
        for candidate in s.scalars(select(SourceCandidate)):
            candidate_counts[candidate.status] = candidate_counts.get(candidate.status, 0) + 1
        source_by_id = {source.id: source for source in cfg.sources if source.enabled}
        scopes_by_employer = {}
        for scope in s.scalars(select(SourceEmployerScope).where(SourceEmployerScope.status == "confirmed")):
            source = source_by_id.get(scope.source_id)
            if not source or scope.mode not in ("group_umbrella", "independent"):
                continue
            if scope.cohorts and cohort not in scope.cohorts:
                continue
            scopes_by_employer.setdefault(scope.employer_id, []).append((scope, source))
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
            exact = [
                x
                for x in cfg.sources
                if x.enabled
                and x.employer
                and (x.employer == employer.name or x.employer in employer.aliases)
            ]
            scoped = scopes_by_employer.get(employer.id, [])
            entries = list({source.id: source for source in [*exact, *(s for _, s in scoped)]}.values())
            own = [o for o in opportunities if o.employer_id == employer.id]
            # Orphan parser guesses from excluded documents cannot define the company universe.
            if not (member_of or entries or own or employer.org_type == "soe" and employer.proof_url):
                continue
            scope_by_source = {scope.source_id: scope for scope, _ in scoped}
            checks = [
                {
                    "id": x.id,
                    "name": x.name,
                    "url": x.homepage_url or x.url,
                    "status": sources[x.id].status if x.id in sources else "unverified",
                    "last_success": sources[x.id].last_success if x.id in sources else None,
                    "error": sources[x.id].last_error if x.id in sources else "",
                    "scope_mode": scope_by_source[x.id].mode if x.id in scope_by_source else "",
                    "scope_cohorts": scope_by_source[x.id].cohorts if x.id in scope_by_source else [],
                    "scope_batch": scope_by_source[x.id].batch_scope if x.id in scope_by_source else "",
                    "scope_evidence": scope_by_source[x.id].evidence_url if x.id in scope_by_source else "",
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
            "coverage_tasks": task_counts,
            "source_candidates": candidate_counts,
        },
        "items": rows,
    }
