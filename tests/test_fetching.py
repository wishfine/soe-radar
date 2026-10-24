from datetime import date

import httpx
import pytest

from soe_radar.fetching import Fetcher, FetchFailure, extract_page
from soe_radar.schemas import SourceSpec
from soe_radar.urls import on_domains, public_url


def test_article_heading_and_publication_do_not_read_deadline():
    page = extract_page(
        "https://www.boc.cn/notice",
        """<html><title>招聘网页</title><h1 class="logo"></h1>
      <h2 class="title">示例银行2027届校园招聘公告</h2><p class="con_time">2026-09-03</p>
      <div class="con_text"><p>报名截止时间为2026年10月9日24点。</p></div></html>""",
    )
    assert page.title == "示例银行2027届校园招聘公告"
    assert page.published_at == date(2026, 9, 3)
    assert "logo" not in page.text


def test_official_domain_matching_is_not_a_substring_test():
    assert on_domains("https://jobs.boc.cn/a", ["boc.cn"])
    assert not on_domains("https://boc.cn.evil.example/a", ["boc.cn"])


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://[::1]/",
        "file:///tmp/file",
        "http://169.254.169.254/latest",
        "http://user:secret@example.org",
    ],
)
def test_private_and_credentialed_urls_rejected(url):
    with pytest.raises(ValueError):
        public_url(url)


async def test_redirect_cannot_escape_source_domains(cfg):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://evil.example/private"})
    )
    fetcher = Fetcher(cfg, transport=transport)
    try:
        with pytest.raises(FetchFailure, match="域名"):
            await fetcher.get("https://www.boc.cn/notice", ["boc.cn"])
    finally:
        await fetcher.close()


async def test_dynamic_shell_is_not_counted_as_a_working_source(cfg):
    source = SourceSpec(id="test", name="测试", url="https://example.org", domains=["example.org"])
    fetcher = Fetcher(
        cfg,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='<html><div id="app"></div></html>')
        ),
    )
    try:
        with pytest.raises(FetchFailure):
            await fetcher.page(source.url, source)
    finally:
        await fetcher.close()


def test_candidates_use_actual_announcements_and_drop_navigation():
    source = SourceSpec(id="test", name="测试", url="https://example.org", domains=["example.org"])
    page = extract_page(
        source.url,
        """<a href="/">招聘</a><a href="/notice">示例集团2027届校园招聘公告</a>
      <a href="https://elsewhere.example/notice">另一个2027招聘公告</a>""",
    )
    assert Fetcher.candidates(page, source) == [("https://example.org/notice", "示例集团2027届校园招聘公告")]


def test_body_images_are_not_silently_treated_as_parsed():
    page = extract_page(
        "https://example.org/news",
        '<h1>示例集团2027届校园招聘</h1><div class="TRS_Editor"><img src="/poster.jpg" alt="招聘简章"></div>',
    )
    assert page.attachments == [{"url": "https://example.org/poster.jpg", "label": "招聘简章"}]


def test_site_logos_and_icons_are_not_counted_as_recruitment_attachments():
    page = extract_page(
        "https://example.org/notice",
        """<h1>示例集团2027届校园招聘</h1><article>
        <img src="/images/logo.png" alt="网站标志">
        <img src="/images/col-icon1.png" alt="栏目标志">
        <img src="/recruitment-poster.jpg" alt="招聘海报">
        <a href="/files/conditions.pdf">招聘条件.pdf</a>
        </article>""",
    )
    assert [item["url"] for item in page.attachments[:2]] == [
        "https://example.org/files/conditions.pdf",
        "https://example.org/recruitment-poster.jpg",
    ]
    assert page.attachments[-1]["status"] == "ignored_chrome"
    assert "2 张" in page.attachments[-1]["label"]


def test_volatile_waf_parameters_do_not_create_different_announcements():
    from soe_radar.urls import canonical

    assert (
        canonical("https://example.org/notice?id=123&CT6T=token&CT6TS=other")
        == "https://example.org/notice?id=123"
    )


def test_trs_pagination_is_parsed_without_executing_javascript():
    page = extract_page(
        "https://www.boc.cn/aboutboc/bi4/index_4.html",
        '<script>createPageHTML(18,4,"index","html");</script><p>共18页</p>',
    )
    assert ("https://www.boc.cn/aboutboc/bi4/index_5.html", "下一页") in page.links


def test_json_jobs_do_not_require_recruitment_word_in_job_name():
    from soe_radar.fetching import Page

    source = SourceSpec(
        id="api", name="岗位", url="https://example.org/list", domains=["example.org"], adapter="json"
    )
    page = Page(
        url=source.url,
        title="岗位",
        text="",
        via="api",
        links=[
            ("https://example.org/job/1", "示例公司 · 软件工程师"),
            ("https://example.org/list?page=2", "下一页"),
        ],
    )
    assert Fetcher.candidates(page, source) == [("https://example.org/job/1", "示例公司 · 软件工程师")]


def test_nuxt_article_reads_literals_without_executing_script():
    import json

    from soe_radar.fetching import embedded_article

    html = (
        'window.__NUXT__=(function(){throw Error("do not execute")})('
        + json.dumps("2027届校园招聘")
        + ","
        + json.dumps('<p><img src="https://example.org/poster.png"></p>')
        + ");"
    )
    page = embedded_article("https://example.org/notice", html)
    assert page.title == "2027届校园招聘"
    assert page.attachments[0]["url"] == "https://example.org/poster.png"


async def test_json_read_only_post_adapter_uses_configured_body(cfg):
    source = SourceSpec(
        id="api",
        name="岗位",
        url="https://example.org/list",
        domains=["example.org"],
        adapter="json",
        api_method="POST",
        api_body={"size": 10},
        api_items_path="items",
        api_fields={"title": "name", "url": "url"},
    )

    def response(request):
        import json

        assert request.method == "POST"
        assert json.loads(request.content) == {"size": 10}
        return httpx.Response(200, json={"items": [{"name": "软件工程师", "url": "/job/1"}]})

    fetcher = Fetcher(cfg, transport=httpx.MockTransport(response))
    page = await fetcher.page(source.url, source)
    assert Fetcher.candidates(page, source)
    await fetcher.close()


def test_lazy_poster_uses_real_image_url_instead_of_empty_src():
    page = extract_page(
        "https://example.org/notice", '<article><img src="" data-original="/recruitment.png"></article>'
    )
    assert page.attachments[0]["url"] == "https://example.org/recruitment.png"


async def test_csg_public_api_preserves_deadline_and_ignores_publisher_identity(cfg):
    from soe_radar.extraction import extract_rules

    source = next(s for s in cfg.sources if s.id == "csg")
    announcement_id = "A" * 32

    def response(req):
        if "searchRecruitAnnouncement" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "list": [
                            {"id": announcement_id, "title": "中国南方电网有限责任公司2027年校园招聘公告"}
                        ]
                    }
                },
            )
        if "loadDetailById" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "title": "中国南方电网有限责任公司2027年校园招聘公告",
                        "publishTime": 1792396731829,
                        "recruitType": 1,
                        "staticPageDxUsAgentUcode": "public-resource-code",
                        "creatorName": "不需要的发布人字段",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": "<h1>2027年校园招聘</h1><p>中国南方电网有限责任公司2027年校园招聘已启动。报名截止时间为2026年11月2日24:00。应聘者请通过官方网站投递。</p>",
            },
        )

    fetcher = Fetcher(cfg, transport=httpx.MockTransport(response))
    try:
        listing = await fetcher.page(source.url, source)
        candidates = fetcher.candidates(listing, source)
        assert len(candidates) == 1
        document = await fetcher.document(*[candidates[0][0], source])
        assert document.published_at == date(2026, 10, 19)
        assert "发布人字段" not in document.text
        result = extract_rules(document)
        assert result.cohorts == [2027]
        assert result.deadline.value == "2026-11-03T00:00:00+08:00"
        assert "报名截止时间" in result.deadline.quote
    finally:
        await fetcher.close()


async def test_post_pagination_is_sent_in_json_not_gateway_query(cfg):
    import json

    source = SourceSpec(
        id="post",
        name="公开列表",
        url="https://example.org/api?limit=1&pageNo=2",
        domains=["example.org"],
        adapter="json",
        api_method="POST",
        api_body={"pageNo": 1, "pageSize": 1},
        api_next_param="pageNo",
        api_items_path="items",
    )
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [{"title": "2027校园招聘", "url": "/notice"}]})

    f = Fetcher(cfg, transport=httpx.MockTransport(response))
    try:
        page = await f.page(source.url, source)
        assert not requests[0].url.query
        assert json.loads(requests[0].content)["pageNo"] == 2
        assert any("pageNo=3" in url for url, label in page.links if label == "下一页")
    finally:
        await f.close()


def test_spa_announcement_fragments_are_preserved_and_do_not_collapse_ids():
    from soe_radar.urls import canonical

    one = "https://zhaopin.csg.cn/#/notice-details?id=" + "A" * 32 + "&recuitType=1"
    two = one.replace("A" * 32, "B" * 32)
    assert "#/notice-details?" in public_url(one)
    assert canonical(one) != canonical(two)
    assert canonical(one) == canonical(one.replace("/#/notice-details", "/notice-details"))
    assert canonical("https://example.org/notice#footer") == "https://example.org/notice"


def test_unlabelled_large_body_poster_is_kept_as_attachment():
    poster = "https://example.org/images/20261023/123456.jpg"
    page = extract_page(
        "https://example.org/recruit",
        "<article><h1>示例公司2027校园招聘</h1><p>详见下方海报</p>"
        f'<img src="{poster}" width="1000" height="2000"></article>',
    )
    assert any(item["url"] == poster for item in page.attachments)


def test_recruitment_banner_is_kept_but_qr_and_logo_are_ignored():
    banner = extract_page(
        "https://example.org/recruit",
        '<article><img src="/images/banner-recruit.jpg" width="1200" height="400" alt="招聘横幅"></article>',
    )
    assert any(item["url"].endswith("banner-recruit.jpg") for item in banner.attachments)
    qr = extract_page(
        "https://example.org/recruit",
        '<article><img src="/images/qrcode.png" alt="扫码关注"></article>',
    )
    assert not any(item["url"] for item in qr.attachments)
    logo = extract_page(
        "https://example.org/recruit",
        '<article><img src="/images/logo.png" alt="网站标志"></article>',
    )
    assert not any(item["url"] for item in logo.attachments)
