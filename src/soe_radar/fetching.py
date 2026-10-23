from __future__ import annotations

import asyncio
import hashlib
import io
import re
import socket
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .config import Settings
from .schemas import DocumentInput, SourceSpec
from .urls import canonical, on_domains, public_url

SIGNAL = re.compile(r"招聘|校招|毕业生|补录|报名|延期")
SPECIFIC = re.compile(r"20\d{2}|公司|集团|银行|研究院|研究所|总部|学院|校园")
PUBLISH_RE = re.compile(r"(?:发布(?:时间|日期)|来源时间)[：:\s]*(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})")


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)
    published_at: date | None = None
    attachments: list[dict] = field(default_factory=list)
    via: str = "http"
    pagination_known: bool = False


class FetchFailure(Exception):
    pass


def extract_page(url: str, html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    # Read literal TRS template arguments, never execute source JavaScript.
    pagination = re.search(
        r'createPageHTML\(\s*(\d+)\s*,\s*(\d+)\s*,\s*["\']([\w-]+)["\']\s*,\s*["\'](html?)["\']\s*\)', html
    )

    if pagination:
        count, current, name, extension = pagination.groups()
        if int(current) + 1 < int(count) <= 10000:
            links.append((urljoin(url, f"{name}_{int(current) + 1}.{extension}"), "下一页"))
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        target = urljoin(url, href)
        if target.startswith(("http://", "https://")):
            label = (a.get("title") or a.get_text(" ", strip=True)).strip()
            links.append((target, label))
    heading = next(
        (node for node in soup.select("h1,h2.title,h2.article-title") if len(node.get_text(strip=True)) >= 6),
        None,
    )
    title = (
        heading.get_text(" ", strip=True)
        if heading
        else (soup.title.get_text(" ", strip=True) if soup.title else "")
    )
    full_text = (soup.body or soup).get_text(" ", strip=True)
    published = None
    meta = soup.select_one(
        'meta[property="article:published_time"],meta[name="publishdate"],meta[name="PubDate"]'
    )
    candidate = meta.get("content", "") if meta else ""
    time_node = soup.select_one(".con_time,.publish-time,.publish_time,.article-time,time[datetime]")
    if not candidate and time_node:
        candidate = time_node.get("datetime") or time_node.get_text(" ", strip=True)
    match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", candidate)
    if not match:
        # BOC and other official articles put the publish date immediately after the heading.
        if heading:
            nearby = " ".join(str(x) for x in list(heading.next_siblings)[:6])
            nearby = BeautifulSoup(nearby, "html.parser").get_text(" ", strip=True)
            match = re.match(r"\s*(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})(?:日)?(?:\s|$)", nearby)
    if not match:
        match = PUBLISH_RE.search(full_text[:6000])
    if match:
        try:
            published = date(*map(int, match.groups()))
        except ValueError:
            pass
    for element in soup.select(
        'script,style,noscript,nav,header,footer,[hidden],[aria-hidden="true"],.modal,.dialog,.pop-up'
    ):
        element.decompose()
    body = None
    for selector in [
        ".jobdetail-box .mainContent",
        ".TRS_Editor",
        ".TRS_PreAppend",
        ".con_text",
        ".con_area",
        ".article-content",
        ".article_content",
        ".content_detail",
        "#zoom",
        "#UCAP-CONTENT",
        "article",
        "main",
    ]:
        candidate_body = soup.select_one(selector)
        if candidate_body and (
            len(candidate_body.get_text(strip=True)) > 20 or candidate_body.select_one("img")
        ):
            body = candidate_body
            break
    body = body or soup.body or soup
    text = body.get_text("\n", strip=True)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    attachments = [
        {"url": target, "label": label}
        for target, label in links
        if re.search(r"\.(pdf|png|jpg|jpeg)(?:[?#]|$)", target, re.I)
    ]
    for img in body.select("img[src],img[data-src],img[data-original]"):
        image_url = img.get("data-original") or img.get("data-src") or img.get("src", "")
        if not image_url.strip():
            continue
        target = urljoin(url, image_url)
        if target.startswith(("http://", "https://")) and target not in {a["url"] for a in attachments}:
            attachments.append({"url": target, "label": img.get("alt") or "正文图片（待人工读取）"})
    return Page(
        url=url,
        title=title,
        text=text,
        links=links,
        published_at=published,
        attachments=attachments,
        pagination_known=bool(pagination),
    )


def embedded_article(url, html):
    """Read JSON string literals from Nuxt SSR data; do not evaluate JavaScript."""
    import json

    values = []
    for token in re.finditer(r'"(?:[^"\\]|\\.)*"', html):
        try:
            value = json.loads(token.group())
        except ValueError:
            continue
        if isinstance(value, str):
            values.append(value)
    headings = [
        v for v in values if re.fullmatch(r"[^<>]{0,50}20\d{2}[^<>]{0,40}(?:校园招聘|届校招)[^<>]{0,20}", v)
    ]
    fragments = [
        v for v in values if re.search(r"<(?:p|article|div)[\s>]", v) and ("<img" in v or SIGNAL.search(v))
    ]
    if not headings or not fragments:
        raise FetchFailure("内嵌页面没有找到明确的校招正文")
    from html import escape

    return extract_page(
        url, "<h1>" + escape(headings[-1]) + "</h1><article>" + "\n".join(fragments[:5]) + "</article>"
    )


class Fetcher:
    def __init__(self, cfg: Settings, *, transport=None, route_transports=None):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            timeout=cfg.http_timeout,
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": "SOE-Radar/0.1 (+personal recruitment research)",
                "Accept": "text/html,application/json,application/pdf;q=0.9,*/*;q=0.5",
            },
        )
        self.transport = transport
        self.route_transports = route_transports or {}
        self.clients = {"env": self.client}
        self.network_attempts: list[dict] = []
        self._last_request: dict[str, float] = {}
        self.api_metadata: dict[str, dict] = {}

    async def close(self):
        for client in self.clients.values():
            await client.aclose()

    async def get(
        self,
        url: str,
        domains: list[str] | None = None,
        *,
        method="GET",
        payload=None,
        network_mode=None,
        source_id="",
    ) -> httpx.Response:
        import ipaddress
        import time

        url = public_url(url)
        for _ in range(6):
            public_url(url)
            if domains and not on_domains(url, domains):
                raise FetchFailure("跳转或链接超出来源官方域名范围")
            host = urlsplit(url).hostname
            # Prevent public hostnames that resolve to private services from entering the crawler.
            if self.transport is None:
                try:
                    addresses = await asyncio.to_thread(socket.getaddrinfo, host, None)
                    fake_nets = [ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48")]

                    def allowed(address):
                        ip = ipaddress.ip_address(address)
                        return ip.is_global or (
                            self.cfg.allow_fake_ip_dns
                            and any(ip.version == net.version and ip in net for net in fake_nets)
                        )

                    if any(not allowed(item[4][0]) for item in addresses):
                        raise FetchFailure("域名解析到非公网地址")
                except socket.gaierror as exc:
                    raise FetchFailure("来源域名无法解析") from exc
            elapsed = time.monotonic() - self._last_request.get(host, 0)
            await asyncio.sleep(max(0, self.cfg.request_delay - elapsed))
            self._last_request[host] = time.monotonic()
            from .models import stamp
            from .network import proxy_url, routes

            try:
                selected_routes = routes(self.cfg, network_mode)
            except ValueError as exc:
                raise FetchFailure(str(exc)) from exc
            response = None
            for route_index, route_name in enumerate(selected_routes):
                if route_name not in self.clients:
                    self.clients[route_name] = httpx.AsyncClient(
                        timeout=self.cfg.http_timeout,
                        follow_redirects=False,
                        trust_env=False,
                        proxy=proxy_url(self.cfg)
                        if route_name == "proxy" and self.transport is None
                        else None,
                        transport=self.route_transports.get(route_name, self.transport),
                        headers=dict(self.client.headers),
                    )
                for attempt in range(2):
                    began = time.monotonic()
                    item = {"source_id": source_id, "host": host, "route": route_name, "at": stamp()}
                    try:
                        response = await self.clients[route_name].request(
                            method, url, json=payload if method == "POST" else None
                        )
                        item["status_code"] = response.status_code
                    except (
                        httpx.TimeoutException,
                        httpx.NetworkError,
                        httpx.RemoteProtocolError,
                        httpx.ProxyError,
                    ) as exc:
                        response = None
                        item["error"] = type(exc).__name__
                    finally:
                        item["duration_ms"] = round((time.monotonic() - began) * 1000)
                        self.network_attempts.append(item)
                        self.network_attempts = self.network_attempts[-200:]
                    if response is not None and response.status_code < 500:
                        break
                    if attempt == 0:
                        await asyncio.sleep(1)
                if response is not None and response.status_code not in (403, 412, 502, 503, 504):
                    break
                if route_index + 1 == len(selected_routes):
                    break
            if response is None:
                raise FetchFailure("网络路径均失败：" + " / ".join(selected_routes))
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            if response.status_code >= 400:
                raise FetchFailure(f"HTTP {response.status_code}")
            if len(response.content) > 20_000_000:
                raise FetchFailure("文档超过 20MB，保留链接待人工处理")
            return response
        raise FetchFailure("来源跳转次数过多")

    async def page(self, url: str, source: SourceSpec, *, detail=False) -> Page:
        detail = detail or source.entry_is_document
        failure = ""
        try:
            if source.adapter == "csg" and detail:
                return await self.csg_document_page(url, source)
            is_api = source.adapter in ("json", "csg") and urlsplit(url).path == urlsplit(source.url).path
            payload = dict(source.api_body)
            request_url = url
            if is_api and source.api_next_param:
                value = dict(parse_qsl(urlsplit(url).query)).get(
                    source.api_next_param, payload.get(source.api_next_param, 1)
                )
                payload[source.api_next_param] = int(value) if str(value).isdigit() else value
                if source.api_method == "POST":
                    parts = urlsplit(url)
                    query = [
                        (k, v) for k, v in parse_qsl(parts.query) if k not in (source.api_next_param, "limit")
                    ]
                    request_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
            response = await self.get(
                request_url,
                source.domains,
                method=source.api_method if is_api else "GET",
                payload=payload,
                network_mode=source.network_mode,
                source_id=source.id,
            )
            content_type = response.headers.get("content-type", "")
            if "pdf" in content_type or urlsplit(url).path.lower().endswith(".pdf"):
                reader = PdfReader(io.BytesIO(response.content))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                if len(text.strip()) < 40:
                    return Page(
                        url=str(response.url),
                        title=source.name + "招聘附件",
                        text="",
                        via="pdf",
                        attachments=[
                            {"url": str(response.url), "label": "扫描型招聘 PDF", "status": "needs_ocr"}
                        ],
                    )
                return Page(url=str(response.url), title=source.name + "招聘附件", text=text, via="pdf")
            if "json" in content_type:
                return self.api_page(
                    url if is_api and source.api_method == "POST" else str(response.url),
                    response.json(),
                    source,
                )
            encoding = re.search(rb'charset=["\s\']*([\w-]+)', response.content[:3000], re.I)
            if encoding:
                try:
                    response.encoding = encoding.group(1).decode()
                except (UnicodeError, LookupError):
                    pass
            page = (
                embedded_article(str(response.url), response.text)
                if source.adapter == "nuxt"
                else extract_page(str(response.url), response.text)
            )
            shell = (
                (len(page.text) < 40 and not (page.attachments and SIGNAL.search(page.title)))
                if detail
                else not self.candidates(page, source)
            )
            if not shell:
                return page
            failure = "页面仅返回动态壳或未发现有效招聘公告"
        except (FetchFailure, httpx.HTTPError, ValueError, OSError) as exc:
            failure = str(exc) if isinstance(exc, FetchFailure) else type(exc).__name__
        if source.browser and self.cfg.browser_enabled:
            try:
                from playwright.async_api import TimeoutError as BrowserTimeout
                from playwright.async_api import async_playwright

                async with async_playwright() as p:
                    from .network import proxy_url, routes

                    route_name = routes(self.cfg, source.network_mode)[0]
                    launch = {"channel": self.cfg.browser_channel}
                    if route_name == "direct":
                        launch["args"] = ["--no-proxy-server"]
                    elif route_name == "proxy":
                        proxy = urlsplit(proxy_url(self.cfg))
                        from urllib.parse import unquote

                        launch["proxy"] = {"server": f"{proxy.scheme}://{proxy.hostname}:{proxy.port or 80}"}
                        if proxy.username:
                            launch["proxy"].update(
                                username=unquote(proxy.username), password=unquote(proxy.password or "")
                            )
                    browser = await p.chromium.launch(**launch)
                    tab = await browser.new_page()

                    # Dynamic subrequests may fetch resources, but navigating away from the source is blocked.
                    async def route(request):
                        try:
                            public_url(request.request.url)
                        except ValueError:
                            await request.abort()
                            return
                        if request.request.is_navigation_request() and not on_domains(
                            request.request.url, source.domains
                        ):
                            await request.abort()
                        else:
                            await request.continue_()

                    await tab.route("**/*", route)
                    await tab.goto(url, wait_until="domcontentloaded", timeout=self.cfg.http_timeout * 1000)
                    try:
                        await tab.wait_for_load_state("networkidle", timeout=5000)
                    except BrowserTimeout:
                        pass
                    html = await tab.content()
                    final_url = tab.url
                    await browser.close()
                page = extract_page(final_url, html)
                page.via = "browser"
                if len(page.text) > 80 and (detail or self.candidates(page, source)):
                    return page
            except Exception as exc:
                failure += f"；浏览器：{type(exc).__name__}（请检查浏览器依赖与来源网络路径）"
        if self.cfg.jina_enabled:
            try:
                response = await self.get(
                    "https://r.jina.ai/" + public_url(url),
                    network_mode=source.network_mode,
                    source_id=source.id,
                )
                body = response.text
                if len(body) < 100 or "captcha" in body.lower() or "403 Forbidden" in body:
                    raise FetchFailure("Reader 未取得有效正文")
                links = [
                    (urljoin(url, href), label)
                    for label, href in re.findall(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", body)
                ]
                page = Page(url=url, title=source.name, text=body, links=links, via="jina")
                if detail or self.candidates(page, source):
                    return page
            except (FetchFailure, httpx.HTTPError) as exc:
                failure += f"；Reader：{type(exc).__name__}"
        if source.browser and not self.cfg.browser_enabled:
            failure += "；可选浏览器采集未启用"
        raise FetchFailure(failure or "没有取得有效招聘内容")

    async def csg_document_page(self, url, source):
        """Public announcement metadata and HTML, as used by CSG's public frontend. No login/token."""
        parts = urlsplit(url)
        announcement_id = dict(parse_qsl(parts.query or parts.fragment.partition("?")[2])).get("id", "")
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", announcement_id):
            raise FetchFailure("南方电网公告标识无效")
        origin = "https://zhaopin.csg.cn"
        meta_url = origin + "/recruitment-dmz/service/announcement/loadDetailById/" + announcement_id
        response = await self.get(
            meta_url, source.domains, network_mode=source.network_mode, source_id=source.id
        )
        payload = response.json()
        meta = payload.get("data")
        if (
            payload.get("code") != 200
            or not isinstance(meta, dict)
            or not meta.get("staticPageDxUsAgentUcode")
        ):
            raise FetchFailure("南方电网公告元数据不可用")
        body_url = (
            origin
            + "/recruitment-application-dmz/service/api/notice/findByNoticeUltra?"
            + urlencode(
                {
                    "code": meta["staticPageDxUsAgentUcode"],
                    "id": announcement_id,
                    "type": "announcement",
                    "t": meta.get("updateTime", ""),
                }
            )
        )
        body = (
            await self.get(body_url, source.domains, network_mode=source.network_mode, source_id=source.id)
        ).json()
        if body.get("code") != 200 or not isinstance(body.get("data"), str):
            raise FetchFailure("南方电网公告正文不可用")
        soup = BeautifulSoup(body["data"], "html.parser")
        # The public frontend uses these attributes for downloads; read literal URLs only.
        for link in soup.select("a[archref]"):
            link["href"] = link["archref"]
        for image in soup.select("img[arcsrc]"):
            image["src"] = image["arcsrc"]
        page = extract_page(url, str(soup))
        if len(page.text) < 40:
            raise FetchFailure("南方电网公告正文过短")
        page.title = meta.get("title") or page.title
        page.via = "csg_public_api"
        # Store recruitment facts only; publisher staff/account fields are irrelevant to extraction.
        metadata = {
            key: meta[key] for key in ("title", "publishTime", "recruitType", "recruitYear") if key in meta
        }
        self.api_metadata[canonical(url)] = metadata
        return page

    def api_page(self, url, data, source):
        if not source.api_items_path:
            raise FetchFailure("JSON 来源尚未配置 api_items_path 和 api_fields")
        rows = data
        for part in source.api_items_path.split("."):
            rows = rows[int(part)] if isinstance(rows, list) else rows.get(part, [])
        if not isinstance(rows, list):
            raise FetchFailure("公开接口字段结构发生变化")
        title_key, url_key = source.api_fields.get("title", "title"), source.api_fields.get("url", "url")
        links = []
        for row in rows:
            if not isinstance(row, dict) or not row.get(url_key) or not row.get(title_key):
                continue
            if any(str(row.get(key)) != value for key, value in source.api_required_fields.items()):
                continue
            target = (
                source.api_url_template.format(id=quote(str(row[url_key]), safe=""))
                if source.api_url_template
                else urljoin(url, str(row[url_key]))
            )
            company = str(row.get(source.api_fields.get("company", "company")) or "")
            label = company + " · " + str(row[title_key])
            links.append((target, label))
            self.api_metadata[canonical(target)] = row
        if source.api_next_param:
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query))
            page_size = int(query.get("limit", "20"))
            if len(rows) >= page_size:
                query[source.api_next_param] = str(int(query.get(source.api_next_param, "1")) + 1)
                links.append(
                    (urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")), "下一页")
                )
        return Page(url=url, title=source.name, text=json_text(data), links=links, via="api")

    @staticmethod
    def candidates(page: Page, source: SourceSpec):
        found = {}
        for url, label in page.links:
            if label == "下一页":
                continue
            if page.via != "api" and (
                len(label) < 6 or not SIGNAL.search(label) or not SPECIFIC.search(label)
            ):
                continue
            if not on_domains(url, source.domains):
                continue
            try:
                key = canonical(url)
                if key != canonical(page.url):
                    found.setdefault(key, label)
            except ValueError:
                continue
        return list(found.items())

    async def document(self, url: str, source: SourceSpec, title_hint="") -> DocumentInput:
        page = await self.page(url, source, detail=True)
        if len(page.text) < 40 and not page.attachments:
            raise FetchFailure("公告正文过短")
        title = (
            page.title
            if SIGNAL.search(page.title) and re.search(r"20\d{2}|校园|毕业生", page.title)
            else title_hint or page.title
        )
        metadata = self.api_metadata.get(canonical(url), {})
        if len(title) < 6 or not SIGNAL.search(title) and not metadata:
            raise FetchFailure("详情页未发现招聘标题，疑似导航/错误页面")
        attachments = []
        sections = [page.text]
        for attachment in page.attachments[:8]:
            item = dict(attachment)
            parts = urlsplit(attachment["url"])
            filename = dict(parse_qsl(parts.query)).get("fileName", "")
            if (parts.path.lower().endswith(".pdf") or filename.lower().endswith(".pdf")) and on_domains(
                attachment["url"], source.domains
            ):
                try:
                    response = await self.get(
                        attachment["url"],
                        source.domains,
                        network_mode=source.network_mode,
                        source_id=source.id,
                    )
                    item["sha256"] = hashlib.sha256(response.content).hexdigest()
                    folder = self.cfg.data_dir / "attachments"
                    folder.mkdir(parents=True, exist_ok=True)
                    local = folder / (item["sha256"] + ".pdf")
                    if not local.exists():
                        local.write_bytes(response.content)
                    item["local_path"] = str(local.relative_to(self.cfg.data_dir))
                    reader = PdfReader(io.BytesIO(response.content))
                    content = "\n".join(p.extract_text() or "" for p in reader.pages)
                    item["status"] = "parsed" if len(content.strip()) >= 40 else "needs_ocr"
                    item["text"] = content
                    if content:
                        sections.append(f"\n[附件 {attachment['label']}]\n{content}")
                except Exception as exc:
                    item["status"] = "failed"
                    item["error"] = type(exc).__name__
            else:
                item["status"] = "needs_review"
                if on_domains(attachment["url"], source.domains):
                    try:
                        response = await self.get(
                            attachment["url"],
                            source.domains,
                            network_mode=source.network_mode,
                            source_id=source.id,
                        )
                        mime = response.headers.get("content-type", "").split(";")[0]
                        suffix = {
                            "image/png": ".png",
                            "image/jpeg": ".jpg",
                            "image/gif": ".gif",
                            "image/webp": ".webp",
                        }.get(mime)
                        if suffix:
                            item["sha256"] = hashlib.sha256(response.content).hexdigest()
                            local = self.cfg.data_dir / "attachments" / (item["sha256"] + suffix)
                            local.parent.mkdir(parents=True, exist_ok=True)
                            if not local.exists():
                                local.write_bytes(response.content)
                            item["local_path"] = str(local.relative_to(self.cfg.data_dir))
                    except (FetchFailure, httpx.HTTPError):
                        item["error"] = "图片未留存，可核对原链接后人工转录"
            attachments.append(item)
        if len(page.attachments) > 8:
            attachments.append({"label": "其余附件超过本轮读取上限", "status": "needs_review"})
        structured = {}
        if metadata:
            for field in ("location", "education", "major"):
                if metadata.get(source.api_fields.get(field, field)):
                    structured[field] = str(metadata[source.api_fields.get(field, field)])
            if str(metadata.get(source.api_fields.get("type", "recruitType"))) == "1":
                structured["recruitment_type"] = "campus"
            published = metadata.get(source.api_fields.get("published", "publishDate"))
            if published and not page.published_at:
                try:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo

                    page.published_at = (
                        datetime.fromtimestamp(
                            float(published) / (1000 if float(published) > 100000000000 else 1),
                            ZoneInfo("Asia/Shanghai"),
                        ).date()
                        if str(published).isdigit()
                        else date.fromisoformat(str(published)[:10])
                    )
                except (ValueError, OverflowError, OSError):
                    pass
        employer = metadata.get(source.api_fields.get("company", "company")) or source.employer
        if metadata:
            sections.append("\n公开接口原始记录：\n" + json_text(metadata))
        return DocumentInput(
            url=page.url,
            title=title,
            text="\n".join(sections),
            published_at=page.published_at,
            employer_hint=employer,
            source_id=source.id,
            attachments=attachments,
            structured=structured,
        )


def json_text(value):
    import json

    return json.dumps(value, ensure_ascii=False)
