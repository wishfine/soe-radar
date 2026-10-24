"""Auditable delivery exports built from the local database and current config."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from bs4 import BeautifulSoup
from sqlalchemy import select

from .models import (
    Deadline,
    Document,
    Employer,
    Opportunity,
    RegistrySnapshot,
    RegistryState,
    Relationship,
    SourceState,
    stamp,
)

LEVELS = {
    "central_sasac": "中央（国务院国资委）",
    "central_mof": "中央（财政部）",
    "central_finance": "中央金融",
    "central_other": "中央其他",
    "central_subsidiaries": "中央所属单位",
    "local": "省级",
    "local_city": "地市",
    "local_prefecture": "地市",
    "local_county": "区县",
    "corps": "兵团",
}


def _csv(path: Path, fields: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _safe(row.get(key, "")) for key in fields})
    path.write_text("\ufeff" + stream.getvalue(), encoding="utf-8")


def _safe(value):
    text = ";".join(map(str, value)) if isinstance(value, list) else str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _state_snapshot(db, cfg):
    with db.session() as s:
        states = {row.id: row for row in s.scalars(select(RegistryState))}
        snapshots = {row.id: row for row in s.scalars(select(RegistrySnapshot))}
    active = {}
    for spec in cfg.registries:
        state = states.get(spec.id)
        snapshot = snapshots.get(state.active_snapshot_id) if state and state.active_snapshot_id else None
        active[spec.id] = (state, snapshot)
    return active


def _copy_evidence(cfg, spec, snapshot, evidence_dir: Path, records: list[dict]):
    if not snapshot:
        return "", {}
    pages = snapshot.provenance.get("pages") or []
    first_path = ""
    page_map = {}
    for page in pages:
        source = cfg.data_dir / page.get("path", "")
        if not source.is_file():
            continue
        digest = page.get("sha256") or hashlib.sha256(source.read_bytes()).hexdigest()
        target = evidence_dir / "registries" / f"{spec.id}-{digest[:12]}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
        relative = str(target.relative_to(evidence_dir.parent))
        first_path = first_path or relative
        page_map[digest] = relative
        records.append(
            {
                "文件": relative,
                "证据类型": "原始HTTP响应",
                "来源URL": page.get("url", ""),
                "访问时间": page.get("fetched_at", ""),
                "SHA256": digest,
                "说明": "HTML 原件；未执行页面脚本。",
            }
        )
        text_target = target.with_suffix(".txt")
        if not text_target.exists():
            text = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "html.parser")
            text_target.write_text(text.get_text("\n", strip=True), encoding="utf-8")
        records.append(
            {
                "文件": str(text_target.relative_to(evidence_dir.parent)),
                "证据类型": "静态文本提取（非浏览器渲染、非OCR）",
                "来源URL": page.get("url", ""),
                "访问时间": page.get("fetched_at", ""),
                "SHA256": hashlib.sha256(text_target.read_bytes()).hexdigest(),
                "说明": "由原始 HTML 去除标签得到，仅用于复核，不替代原件。",
            }
        )
    return first_path, page_map


def _manifest_rows(cfg, active, evidence_paths, evidence_hashes):
    rows = []
    for spec in cfg.registries:
        state, snapshot = active[spec.id]
        provenance = snapshot.provenance if snapshot else {}
        pages = provenance.get("pages") or []
        if not snapshot:
            pagination = "未接入"
        elif provenance.get("acquisition") == "official_index_excerpt":
            pagination = "索引摘录/未分页"
        elif len(pages) > 1:
            pagination = f"{len(pages)}页已读" + ("；有失败页" if provenance.get("fetch_error") else "")
        elif pages:
            pagination = "单页（是否还有分页未确认）"
        else:
            pagination = "未知"
        gap = ""
        if not snapshot:
            gap = state.last_error if state and state.last_error else "未取得官方名录入口"
        elif state and state.last_error:
            gap = state.last_error
        elif provenance.get("tls_verified") is False:
            gap = "TLS 证书校验未完成；仅作待复核官方页面证据，不据此提升企业性质核验。"
        elif provenance.get("acquisition") == "official_index_excerpt":
            gap = "历史或索引摘录，需按当前官网分页和现行名单复核"
        rows.append(
            {
                "范围ID": spec.id,
                "地区": spec.region,
                "层级": LEVELS.get(spec.category, spec.category),
                "官方名录URL": snapshot.source_url if snapshot else (spec.url or spec.authority_url),
                "发布日期": provenance.get("published_at", ""),
                "访问时间": (state.last_attempt if state else "")
                or (snapshot.fetched_at if snapshot else ""),
                "取得条数": len(snapshot.entries) if snapshot else "",
                "分页完成情况": pagination,
                "名单完整性": "未确认",
                "证据路径": evidence_paths.get(spec.id, snapshot.raw_path if snapshot else ""),
                "证据SHA256": evidence_hashes.get(spec.id, ""),
                "缺口原因": gap,
                "范围说明": spec.scope,
            }
        )
    return rows


def _memberships(cfg, active):
    memberships = defaultdict(list)
    for spec in cfg.registries:
        state, snapshot = active[spec.id]
        if not snapshot:
            continue
        for entry in snapshot.entries:
            memberships[entry["name"]].append((spec, snapshot, entry))
    return memberships


def _employer_rows(db, cfg, active, evidence_paths, evidence_hashes, page_evidence):
    memberships = _memberships(cfg, active)
    with db.session() as s:
        employers = list(s.scalars(select(Employer).order_by(Employer.name)))
        parent_names = {row.id: row.name for row in employers}
        source_employers = {source.employer: source for source in cfg.sources if source.employer}
        source_urls = {url for source in cfg.sources for url in (source.url, source.homepage_url) if url}
        relationships = {row.child_id: row for row in s.scalars(select(Relationship))}
    rows = []
    for employer in employers:
        member = memberships.get(employer.name, [])
        member.sort(key=lambda item: item[1].provenance.get("acquisition") == "official_index_excerpt")
        primary = member[0] if member else None
        source = source_employers.get(employer.name)
        title_like = bool(
            re.search(
                r"！|公告|招聘|启动|简章|岗位|公示|通知|人数|人！|待确认|拟录用|结果",
                employer.name,
            )
        )
        if not member and not source and not employer.parent_id:
            # Extraction can create title fragments as employer names. Keep only records with
            # an independent ownership/source basis, never a title-like fragment.
            if title_like or not employer.proof_url or employer.proof_url in source_urls:
                continue
        if not (member or source or employer.parent_id or employer.proof_url or employer.org_type == "soe"):
            continue
        spec, snapshot, entry = primary if primary else (None, None, {})
        history = [
            {
                "registry_id": item_spec.id,
                "source_url": item_snapshot.source_url,
                "fetched_at": item_snapshot.fetched_at,
                "acquisition": item_snapshot.provenance.get("acquisition", ""),
                "entry_quote": item_entry.get("quote", item_entry["name"]),
            }
            for item_spec, item_snapshot, item_entry in member
        ]
        acquisition = snapshot.provenance.get("acquisition") if snapshot else ""
        relation = relationships.get(employer.id)
        if acquisition == "official_index_excerpt":
            status = "历史/索引摘录待复核"
        elif relation and relation.status == "confirmed":
            status = "当前（关系证据已确认）"
        elif employer.verified_at:
            status = "当前"
        else:
            status = "待复核"
        evidence_entry = page_evidence.get((spec.id, entry.get("page_sha256"))) if spec else None
        evidence_path = (
            evidence_entry[0] if evidence_entry else (evidence_paths.get(spec.id, "") if spec else "")
        )
        evidence_sha = entry.get("page_sha256") or (evidence_hashes.get(spec.id, "") if spec else "")
        rows.append(
            {
                "id": employer.id,
                "原始名称": employer.name,
                "规范名称": re.sub(r"\s+", "", employer.name),
                "别名": employer.aliases,
                "地区": "/".join(sorted({item_spec.region for item_spec, _, _ in member})) or "",
                "主管口径": "/".join(sorted({item_spec.category for item_spec, _, _ in member})) or "",
                "层级": "所属单位" if employer.parent_id else ("集团/一级" if member else "待确认"),
                "母公司": parent_names.get(employer.parent_id, "") if employer.parent_id else "",
                "官方来源URL": (
                    source.homepage_url or source.url
                    if source
                    else snapshot.source_url
                    if snapshot
                    else relation.evidence_url
                    if relation
                    else employer.proof_url
                ),
                "页面发布日期": snapshot.provenance.get("published_at", "") if snapshot else "",
                "实际访问时间": (snapshot.provenance.get("pages") or [{}])[0].get("fetched_at", "")
                if snapshot
                else "",
                "来源名称": entry.get("source_name", employer.name) if entry else "",
                "证据页URL": entry.get("page_url", "") if entry else "",
                "来源原文片段": entry.get("quote", employer.name)
                if entry
                else ("来源配置声明的招聘主体" if source else relation.quote if relation else ""),
                "证据路径": evidence_path,
                "证据SHA256": evidence_sha,
                "企业官网": entry.get("official_url", "")
                if entry
                else (source.homepage_url if source else ""),
                "官方档案页": entry.get("profile_url", "") if entry else "",
                "官网域名线索": entry.get("declared_domain", "") if entry else "",
                "状态": status,
                "历史": history,
            }
        )
    return rows


def _relationship_rows(db):
    with db.session() as s:
        rows = list(
            s.scalars(
                select(Relationship)
                .where(Relationship.status == "confirmed")
                .order_by(Relationship.created_at)
            )
        )
        parents = {row.id: row.name for row in s.scalars(select(Employer))}
    return [
        {
            "母公司": parents.get(row.parent_id, row.parent_id),
            "子公司": parents.get(row.child_id, row.child_id),
            "关系类型": row.relation_type,
            "关系证据URL": row.evidence_url,
            "原文片段": row.quote,
            "证据日期": row.evidence_date,
            "状态": row.status,
            "创建时间": row.created_at,
        }
        for row in rows
    ]


def _source_rows(db, cfg):
    with db.session() as s:
        states = {row.id: row for row in s.scalars(select(SourceState))}
        documents = list(s.scalars(select(Document)))
        opportunities = list(
            s.scalars(
                select(Opportunity).where(
                    Opportunity.merged_into_id.is_(None),
                    Opportunity.recruitment_type == "campus",
                )
            )
        )
        deadlines = {row.id: row for row in s.scalars(select(Deadline))}
    rows = []
    for source in cfg.sources:
        state = states.get(source.id)
        docs = [doc for doc in documents if doc.source_id == source.id]
        campus = [opp for opp in opportunities if opp.source_id == source.id]
        pending = [opp for opp in campus if opp.review_status == "pending" or opp.needs_review]
        latest = sorted(campus, key=lambda opp: (opp.published_at or "", opp.first_seen), reverse=True)[:1]
        attachments = Counter()
        for doc in docs:
            for item in doc.attachments or []:
                attachments[item.get("status", "unknown")] += 1
        backlog = len((state.cursor or {}).get("backlog", [])) if state else 0
        next_page = (state.cursor or {}).get("next_page") if state else ""
        list_result = (
            f"状态={state.status if state else 'unverified'}；发现={state.last_count if state else 0}；"
            f"积压详情={backlog}；续页={'有' if next_page else '无'}"
        )
        detail_result = (
            f"校招机会={len(campus)}；待核验={len(pending)}；文档={len(docs)}；"
            f"最新={latest[0].title if latest else '无'}"
        )
        attachment_result = (
            f"解析={attachments.get('parsed', 0)}；失败={attachments.get('failed', 0)}；"
            f"待OCR={attachments.get('needs_ocr', 0)}；待复核={attachments.get('needs_review', 0)}；"
            f"忽略站点装饰={attachments.get('ignored_chrome', 0)}"
        )
        annual = ""
        if latest:
            opp = latest[0]
            claim = deadlines.get(opp.latest_deadline_id) if opp.latest_deadline_id else None
            annual = f"{opp.title} {opp.official_url}"
            if claim:
                annual += f"（截止：{claim.kind} {claim.value or ''}）"
        rows.append(
            {
                "企业": source.employer or source.name,
                "来源ID": source.id,
                "长期入口": source.homepage_url or source.url,
                "年度专题": annual,
                "官方身份依据": source.homepage_url or source.url,
                "采集方式": "+".join(
                    part
                    for part in [
                        source.adapter,
                        "browser" if source.browser else "",
                        "api" if source.adapter in ("json", "csg") else "",
                        source.network_mode or "env",
                    ]
                    if part
                ),
                "测试时间": state.last_attempt if state else "",
                "列表结果": list_result,
                "详情结果": detail_result,
                "附件结果": attachment_result,
                "当前状态": f"{state.status if state else 'unverified'}；{state.last_error if state else ''}".strip(
                    "；"
                ),
            }
        )
    return rows


def _gap_rows(db, cfg, active, employer_rows):
    rows = []
    with db.session() as s:
        states = {row.id: row for row in s.scalars(select(SourceState))}
        parent_links = [row for row in s.scalars(select(Employer)) if row.parent_id and row.proof_url]
        relationship_rows = list(s.scalars(select(Relationship)))
        relationships = {(row.parent_id, row.child_id) for row in relationship_rows}
        needs_review = [row for row in relationship_rows if row.status != "confirmed"]
        parents = {row.id: row.name for row in s.scalars(select(Employer))}
    for spec in cfg.registries:
        state, snapshot = active[spec.id]
        if not spec.url:
            rows.append(
                _gap(
                    "未建档",
                    f"{spec.region}/{spec.name}",
                    "未取得官方名录入口，不能把省级集团名单当作全部地方国企。",
                    "",
                    "从主管部门官网导航、站内搜索或政府公开目录定位名录页并保存证据。",
                    "高" if spec.category in ("corps", "local_county") else "中",
                )
            )
        elif state and state.status == "error":
            rows.append(
                _gap(
                    "访问受阻",
                    spec.name,
                    state.last_error or "名录读取失败",
                    state.last_attempt,
                    "保留已有版本；稍后重试或人工读取官方页面。",
                    "高",
                )
            )
        elif state and state.status == "partial":
            rows.append(
                _gap(
                    "分页未完",
                    spec.name,
                    state.last_error or "部分页面读取失败",
                    state.last_attempt,
                    "补齐失败分页并核对总页数。",
                    "高",
                )
            )
        if snapshot and snapshot.provenance.get("acquisition") == "official_index_excerpt":
            rows.append(
                _gap(
                    "历史名单待更新",
                    spec.name,
                    "当前仅有历史/索引摘录，未完成官网分页核对。",
                    snapshot.fetched_at,
                    "按现行官网重新读取并保留旧版本。",
                    "中",
                )
            )
        if spec.category in ("central_subsidiaries", "local_prefecture", "local_county", "corps"):
            rows.append(
                _gap(
                    "所属单位待查",
                    spec.name,
                    spec.scope,
                    "",
                    "按集团架构、地市国资委和区县国资监管名单逐批下钻。",
                    "高",
                )
            )
    for source in cfg.sources:
        state = states.get(source.id)
        if not state:
            continue
        if state.status in ("error", "failed"):
            rows.append(
                _gap(
                    "访问受阻",
                    source.employer or source.name,
                    state.last_error or "来源读取失败",
                    state.last_attempt,
                    "检查入口、动态加载和网络路径；保留原失败证据。",
                    "高",
                )
            )
        elif state.status == "partial":
            rows.append(
                _gap(
                    "分页未完",
                    source.employer or source.name,
                    state.last_error or "存在未完成分页或详情积压",
                    state.last_attempt,
                    "续跑积压详情并核对总页数。",
                    "中",
                )
            )
    source_campus = Counter()
    with db.session() as s:
        for opp in s.scalars(
            select(Opportunity).where(
                Opportunity.merged_into_id.is_(None), Opportunity.recruitment_type == "campus"
            )
        ):
            source_campus[opp.source_id] += 1
    for source in cfg.sources:
        state = states.get(source.id)
        if state and state.last_success and source_campus.get(source.id, 0) == 0:
            rows.append(
                _gap(
                    "当届未发现",
                    source.employer or source.name,
                    "来源可读但未发现当届校招公告；不能推断企业不招聘。",
                    state.last_success,
                    "继续巡查年度专题、所属单位和补录入口。",
                    "中",
                )
            )
    for row in employer_rows:
        names = {row["原始名称"], row["规范名称"], *row.get("别名", [])}
        matching = [source for source in cfg.sources if source.employer in names or source.name in names]
        available = [source for source in matching if source.enabled]
        in_scope = bool(
            row["主管口径"]
            or row["母公司"]
            or row["层级"] in ("集团/一级", "所属单位")
            or row["状态"].startswith("当前（关系")
        )
        if in_scope and not available:
            issue = "已建档但未配置可用官方招聘入口。"
            if matching:
                issue = "仅有已禁用来源，当前没有可用监测入口。"
            rows.append(
                _gap(
                    "入口未找到",
                    row["原始名称"],
                    issue,
                    row["实际访问时间"],
                    "从企业官网、集团招聘平台和官方公告列表寻找入口并做列表/详情验证。",
                    "中",
                )
            )
    for employer in parent_links:
        if (employer.parent_id, employer.id) not in relationships:
            rows.append(
                _gap(
                    "关系待复核",
                    employer.name,
                    "存在母公司链接但缺少原文片段和证据日期，未计入已确认关系。",
                    employer.verified_at or "",
                    "补录关系证据原文、日期和URL。",
                    "中",
                )
            )
    for relationship in needs_review:
        rows.append(
            _gap(
                "关系待复核",
                parents.get(relationship.child_id, relationship.child_id),
                "关系证据不足以确认所选法人主体，未计入已确认关系。",
                relationship.evidence_date,
                "补充该法人全称对应的独立官方披露及简称映射。",
                "中",
            )
        )
    for source_id, count in source_campus.items():
        pending_count = 0
        with db.session() as s:
            pending_count = len(
                list(
                    s.scalars(
                        select(Opportunity).where(
                            Opportunity.source_id == source_id,
                            Opportunity.recruitment_type == "campus",
                            Opportunity.review_status == "pending",
                        )
                    )
                )
            )
        if pending_count:
            rows.append(
                _gap(
                    "截止待补证",
                    source_id,
                    f"{pending_count} 条校招机会待核验截止或主体证据。",
                    "",
                    "逐条读取官方原文、附件或人工转录。",
                    "中",
                )
            )
    for index, row in enumerate(rows, start=1):
        row["缺口ID"] = f"GAP-{index:04d}"
    return rows


def _gap(kind, scope, problem, evidence_time, action, priority):
    return {
        "类型": kind,
        "范围/企业": scope,
        "问题": problem,
        "证据/检查时间": evidence_time,
        "下一步动作": action,
        "优先级": priority,
    }


def export_delivery(db, cfg, out_dir: Path):
    out_dir = out_dir.resolve()
    evidence_dir = out_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    active = _state_snapshot(db, cfg)
    evidence_records = []
    evidence_paths, evidence_hashes = {}, {}
    page_evidence = {}
    for spec in cfg.registries:
        _, snapshot = active[spec.id]
        path, page_map = _copy_evidence(cfg, spec, snapshot, evidence_dir, evidence_records)
        evidence_paths[spec.id] = path
        for digest, relative in page_map.items():
            page_evidence[(spec.id, digest)] = (relative, digest)
        if snapshot:
            pages = snapshot.provenance.get("pages") or []
            evidence_hashes[spec.id] = pages[0].get("sha256", "") if pages else snapshot.digest
    known = {row["文件"] for row in evidence_records}
    for extra in sorted(evidence_dir.rglob("*")):
        if not extra.is_file():
            continue
        relative = str(extra.relative_to(evidence_dir.parent))
        if relative in known or extra.name in ("manifest.csv", "SHA256SUMS"):
            continue
        evidence_type, source_url, note = "人工归档证据", "", "未由导出器自动抓取；复核时请结合 REPORT.md。"
        sibling = extra.with_suffix(".json")
        if sibling.is_file() and extra.suffix != ".json":
            try:
                meta = json.loads(sibling.read_text(encoding="utf-8"))
                source_url = meta.get("official_url", "")
                if "relationships" in extra.parts and extra.suffix == ".html":
                    evidence_type = "官方关系证据（原始HTML）"
                    note = f"关系证据原件：{meta.get('title', '')}"
                elif "relationships" in extra.parts and extra.suffix == ".txt":
                    evidence_type = "静态文本提取（关系证据，非OCR）"
                    note = f"由关系证据 HTML 去除标签得到：{meta.get('title', '')}"
                elif extra.suffix == ".html":
                    evidence_type = "原始HTTP响应（招聘详情）"
                    note = f"招聘详情原件：{meta.get('title', '')}"
                elif extra.suffix == ".txt":
                    evidence_type = "静态文本提取（招聘详情，非OCR）"
                    note = f"由详情 HTML 去除标签得到：{meta.get('title', '')}"
            except (ValueError, OSError):
                pass
        elif extra.suffix == ".json" and "recruitment" in extra.parts:
            try:
                meta = json.loads(extra.read_text(encoding="utf-8"))
                source_url = meta.get("official_url", "")
                evidence_type = "程序抽取详情JSON（非原始HTML）"
                note = f"程序保存的详情文本与附件元数据：{meta.get('title', '')}"
            except (ValueError, OSError):
                pass
        evidence_records.append(
            {
                "文件": relative,
                "证据类型": evidence_type,
                "来源URL": source_url,
                "访问时间": "",
                "SHA256": hashlib.sha256(extra.read_bytes()).hexdigest(),
                "说明": note,
            }
        )
    manifest = _manifest_rows(cfg, active, evidence_paths, evidence_hashes)
    employers = _employer_rows(db, cfg, active, evidence_paths, evidence_hashes, page_evidence)
    relationships = _relationship_rows(db)
    sources = _source_rows(db, cfg)
    gaps = _gap_rows(db, cfg, active, employers)

    _csv(
        out_dir / "registry_manifest.csv",
        [
            "范围ID",
            "地区",
            "层级",
            "官方名录URL",
            "发布日期",
            "访问时间",
            "取得条数",
            "分页完成情况",
            "名单完整性",
            "证据路径",
            "证据SHA256",
            "缺口原因",
            "范围说明",
        ],
        manifest,
    )
    _jsonl(out_dir / "employers.jsonl", employers)
    _jsonl(out_dir / "relationships.jsonl", relationships)
    _csv(
        out_dir / "recruitment_sources.csv",
        [
            "企业",
            "来源ID",
            "长期入口",
            "年度专题",
            "官方身份依据",
            "采集方式",
            "测试时间",
            "列表结果",
            "详情结果",
            "附件结果",
            "当前状态",
        ],
        sources,
    )
    _csv(
        out_dir / "gaps.csv",
        ["缺口ID", "类型", "范围/企业", "问题", "证据/检查时间", "下一步动作", "优先级"],
        gaps,
    )

    manifest_csv = evidence_dir / "manifest.csv"
    _csv(
        manifest_csv,
        ["文件", "证据类型", "来源URL", "访问时间", "SHA256", "说明"],
        evidence_records,
    )
    sums = evidence_dir / "SHA256SUMS"
    sums.write_text(
        "".join(
            f"{row['SHA256']}  {row['文件']}\n" for row in sorted(evidence_records, key=lambda x: x["文件"])
        ),
        encoding="utf-8",
    )
    summary = {
        "generated_at": stamp(),
        "registry_rows": len(manifest),
        "registry_rows_with_live_snapshot": sum(
            1 for row in manifest if row["取得条数"] != "" and row["分页完成情况"] not in ("索引摘录/未分页",)
        ),
        "registry_entries": sum(len(snapshot.entries) for _, snapshot in active.values() if snapshot),
        "employers": len(employers),
        "relationships_confirmed": len(relationships),
        "sources": len(sources),
        "gaps": len(gaps),
        "gap_types": dict(Counter(row["类型"] for row in gaps)),
        "evidence_files": len(evidence_records),
        "note": "全国完整名单、全部所属单位和招聘覆盖均未确认；本文件只汇总可复核的已知范围。",
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
