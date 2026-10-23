import httpx
import pytest

from soe_radar.fetching import Fetcher, FetchFailure
from soe_radar.schemas import SourceSpec


def test_source_network_override_is_not_ignored():
    spec = SourceSpec(
        id="x", name="x", url="https://example.org", domains=["example.org"], network_mode="direct"
    )
    assert getattr(spec, "network_mode", None) == "direct"


async def test_auto_can_use_configured_proxy_without_logging_secrets(cfg, monkeypatch):
    cfg.network_mode = "auto"
    monkeypatch.setenv("SOE_RADAR_HTTP_PROXY", "http://user:secret@127.0.0.1:7897")

    def failing(request):
        raise httpx.ConnectError("contains-sensitive-upstream-text", request=request)

    fetcher = Fetcher(
        cfg,
        transport=httpx.MockTransport(failing),
        route_transports={"proxy": httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))},
    )
    try:
        response = await fetcher.get("https://example.org/a?token=secret", ["example.org"])
        assert response.text == "ok"
        assert [x["route"] for x in fetcher.network_attempts] == ["env", "env", "proxy"]
        assert "secret" not in str(fetcher.network_attempts)
        assert "sensitive" not in str(fetcher.network_attempts)
    finally:
        await fetcher.close()


async def test_unconfigured_proxy_does_not_silently_fall_back(cfg, monkeypatch):
    cfg.network_mode = "proxy"
    monkeypatch.delenv("SOE_RADAR_HTTP_PROXY", raising=False)
    fetcher = Fetcher(cfg, transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        with pytest.raises(FetchFailure, match="代理"):
            await fetcher.get("https://example.org", ["example.org"])
    finally:
        await fetcher.close()


async def test_rate_limit_is_not_retried_via_another_route(cfg, monkeypatch):
    cfg.network_mode = "auto"
    monkeypatch.setenv("SOE_RADAR_HTTP_PROXY", "http://127.0.0.1:7897")
    fetcher = Fetcher(cfg, transport=httpx.MockTransport(lambda req: httpx.Response(429)))
    try:
        with pytest.raises(FetchFailure, match="429"):
            await fetcher.get("https://example.org", ["example.org"])
        assert len(fetcher.network_attempts) == 1
    finally:
        await fetcher.close()
