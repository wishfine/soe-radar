import httpx
from sqlalchemy import func, select

from soe_radar.fetching import Fetcher
from soe_radar.models import CrawlRun, Document, Opportunity, SourceState
from soe_radar.sync import Pipeline, Scheduler, execute_run, queue_run


async def test_failed_source_preserves_previous_opportunity(db, cfg):
    from test_store import sample

    pipeline = Pipeline(
        cfg, db, fetcher=Fetcher(cfg, transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    )
    first = await pipeline.process(sample())
    summary = await pipeline.crawl(cfg.sources[0])
    assert summary["status"] == "error"
    with db.session() as s:
        assert s.get(Opportunity, first["id"]).review_status == "auto"
        assert s.get(SourceState, "boc").failures == 1
    await pipeline.close()


async def test_repeated_successful_sync_does_not_reparse_or_duplicate(db, cfg):
    from test_store import sample

    pipeline = Pipeline(cfg, db)
    await pipeline.process(sample())
    assert (await pipeline.process(sample()))["cached"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 1
        assert s.scalar(select(func.count()).select_from(Opportunity)) == 1
    await pipeline.close()


async def test_reclassification_hides_old_same_title_record_even_when_document_changes(db, cfg):
    from test_store import sample

    from soe_radar.schemas import ExtractionResult
    from soe_radar.store import ingest

    document = sample().model_copy(update={"title": "中国银行2027届拟接收毕业生公示"})
    mistaken = ExtractionResult(
        title=document.title, employer=document.employer_hint, cohorts=[2027], recruitment_type="unknown"
    )
    row = ingest(db, cfg, document, mistaken)
    updated = document.model_copy(
        update={"text": "面向2027届。这是拟接收毕业生公示，名单见附件，并非正在接受报名。"}
    )
    pipeline = Pipeline(cfg, db)
    assert (await pipeline.process(updated))["excluded"]
    with db.session() as s:
        assert s.get(Opportunity, row["id"]).review_status == "excluded"
    await pipeline.close()


async def test_pending_retry_preserves_original_employer_context_and_document_identity(db, cfg):
    from test_store import sample

    document = sample().model_copy(
        update={
            "source_id": "ncss",
            "url": "https://www.ncss.cn/student/jobs/123/detail.html",
            "employer_hint": "示例国有企业",
            "text": "面向2027届毕业生。报名截止尚未公布。",
        }
    )
    pipeline = Pipeline(cfg, db)
    await pipeline.process(document)
    for _ in range(2):
        await pipeline.retry_pending()
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 1
        assert s.scalar(select(func.count()).select_from(Opportunity)) == 1
    await pipeline.close()


async def test_open_announcement_is_rechecked_after_leaving_list(db, cfg):
    from test_store import sample

    from soe_radar.extraction import extract_rules
    from soe_radar.store import ingest

    document = sample().model_copy(
        update={"title": "中国银行2035届校园招聘公告", "text": "面向2035届毕业生。报名截止2035年10月31日。"}
    )
    original = ingest(db, cfg, document, extract_rules(document))
    with db.session() as s:
        s.get(Opportunity, original["id"]).last_checked = "2020-01-01T00:00:00+00:00"
    visited = []

    def response(request):
        visited.append(request.url.path)
        if request.url.path == "/aboutboc/bi4/":
            return httpx.Response(200, text='<a href="/other">中国银行2036届校园招聘公告</a>')
        return httpx.Response(503)

    pipeline = Pipeline(cfg, db, fetcher=Fetcher(cfg, transport=httpx.MockTransport(response)))
    await pipeline.crawl(cfg.sources[0])
    assert "/notice" in visited
    await pipeline.close()


async def test_limits_leave_resume_cursor_and_always_refresh_front_page(db, cfg):
    cfg.details_per_source = 1
    requests = []

    def transport(request):
        requests.append(request.url.path)
        if request.url.path == "/aboutboc/bi4/":
            return httpx.Response(
                200,
                text="""<a href="/one">中国银行2035届校园招聘第一批公告</a>
              <a href="/two">中国银行2035届校园招聘第二批公告</a>""",
            )
        return httpx.Response(
            200,
            text="<h1>中国银行2035届校园招聘公告</h1><article>面向2035届毕业生。报名截止2035年10月31日。工作地点北京。欢迎毕业生了解岗位及专业要求并在线报名，招聘程序以公告为准。</article>",
        )

    pipeline = Pipeline(cfg, db, fetcher=Fetcher(cfg, transport=httpx.MockTransport(transport)))
    first = await pipeline.crawl(cfg.sources[0])
    assert first["status"] == "partial"
    with db.session() as s:
        assert len(s.get(SourceState, "boc").cursor["backlog"]) == 1
    await pipeline.crawl(cfg.sources[0])
    assert requests.count("/aboutboc/bi4/") == 2
    assert "/one" in requests and "/two" in requests
    await pipeline.close()


def test_interrupted_run_recovers_only_after_lease_expiration(db, cfg):
    run = queue_run(db)
    with db.session() as s:
        s.get(CrawlRun, run).status = "running"
    assert db.acquire("sync", "live-worker")
    Scheduler(db, cfg).recover()
    with db.session() as s:
        assert s.get(CrawlRun, run).status == "running"
    db.release("sync", "live-worker")
    Scheduler(db, cfg).recover()
    with db.session() as s:
        assert s.get(CrawlRun, run).status == "queued"


async def test_resume_pages_advance_without_restart_from_front_pagination(db, cfg):
    from urllib.parse import urlsplit

    cfg.source_pages = 2
    with db.session() as s:
        s.get(SourceState, "boc").cursor = {"next_page": "https://www.boc.cn/aboutboc/bi4/index_6.html"}
    paths = []

    def response(request):
        path = urlsplit(str(request.url)).path
        paths.append(path)
        current = 6 if "index_6" in path else 0
        return httpx.Response(
            200,
            text=f'<a href="/notice">中国银行2035届校园招聘公告</a><p>共18页</p><script>createPageHTML(18,{current},"index","html");</script>',
        )

    pipeline = Pipeline(cfg, db, fetcher=Fetcher(cfg, transport=httpx.MockTransport(response)))
    await pipeline.crawl(cfg.sources[0])
    with db.session() as s:
        assert s.get(SourceState, "boc").cursor["next_page"] == "https://www.boc.cn/aboutboc/bi4/index_7.html"
    assert paths[:2] == ["/aboutboc/bi4/", "/aboutboc/bi4/index_6.html"]
    await pipeline.close()


async def test_run_records_source_failures_and_finishes(db, cfg):
    cfg.sources = cfg.sources[:1]
    pipeline = Pipeline(
        cfg, db, fetcher=Fetcher(cfg, transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    )
    run = queue_run(db)
    summary = await execute_run(db, cfg, run, pipeline=pipeline)
    assert summary["status"] == "partial"
    assert (cfg.data_dir / "observations.jsonl").exists()
