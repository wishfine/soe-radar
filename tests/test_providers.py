import json

import httpx
import pytest
from sqlalchemy import func, select
from test_store import sample

from soe_radar.config import ProviderConfig
from soe_radar.models import ModelCall
from soe_radar.providers import Extractor, ModelUnavailable
from soe_radar.schemas import ExtractionResult


def provider(**kwargs):
    values = dict(
        name="free-test",
        protocol="chat_completions",
        base_url="https://api.example.org/v1",
        models=["test-free"],
        free_models=["test-free"],
        key_required=False,
        billing="free",
    )
    values.update(kwargs)
    return ProviderConfig(**values)


async def test_caches_success_and_preserves_explicit_model_id(cfg, db):
    cfg.llm.providers = [provider()]
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ExtractionResult().model_dump_json()}}]}
        )

    engine = Extractor(cfg, db, transport=httpx.MockTransport(handle))
    await engine.extract(sample())
    await engine.extract(sample())
    assert len(requests) == 1
    assert requests[0]["model"] == "test-free"
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(ModelCall)) == 1


async def test_paid_provider_and_unconfirmed_free_model_are_never_requested(cfg, db):
    cfg.llm.providers = [provider(billing="paid"), provider(models=["test-paid"])]

    def forbidden(_):
        pytest.fail("default zero budget must not send this request")

    with pytest.raises(ModelUnavailable):
        await Extractor(cfg, db, transport=httpx.MockTransport(forbidden)).extract(sample())


async def test_quota_pauses_provider_and_keeps_task_pending(cfg, db):
    cfg.llm.providers = [provider()]
    calls = []

    def handle(_):
        calls.append(1)
        return httpx.Response(429)

    engine = Extractor(cfg, db, transport=httpx.MockTransport(handle))
    for _ in range(2):
        with pytest.raises(ModelUnavailable):
            await engine.extract(sample())
    assert len(calls) == 1


async def test_invalid_output_has_three_attempt_ceiling(cfg, db):
    cfg.llm.providers = [
        provider(models=["test-free", "fallback-free"], free_models=["test-free", "fallback-free"])
    ]
    calls = []

    def handle(_):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(ModelUnavailable):
        await Extractor(cfg, db, transport=httpx.MockTransport(handle)).extract(sample())
    assert len(calls) == 3


async def test_daily_limit_reserves_before_network_request(cfg, db):
    cfg.llm.providers = [provider()]
    cfg.llm.daily_calls = 0

    def forbidden(_):
        pytest.fail("must not exceed budget")

    with pytest.raises(ModelUnavailable):
        await Extractor(cfg, db, transport=httpx.MockTransport(forbidden)).extract(sample())


async def test_missing_key_is_not_a_request(cfg, db, monkeypatch):
    monkeypatch.delenv("SOE_TEST_KEY", raising=False)
    cfg.llm.providers = [provider(key_required=True, api_key_env="SOE_TEST_KEY")]
    with pytest.raises(ModelUnavailable, match="SOE_TEST_KEY"):
        await Extractor(cfg, db).extract(sample())


@pytest.mark.parametrize("status", [401, 402, 403, 404])
async def test_unavailable_provider_is_paused_without_paid_fallback(cfg, db, status):
    cfg.llm.providers = [provider(), provider(name="paid", billing="paid")]
    calls = []

    def handle(_):
        calls.append(1)
        return httpx.Response(status)

    engine = Extractor(cfg, db, transport=httpx.MockTransport(handle))
    for _ in range(2):
        with pytest.raises(ModelUnavailable):
            await engine.extract(sample())
    assert len(calls) == 1


async def test_paid_monthly_budget_reserves_before_request(cfg, db):
    cfg.llm.allow_paid = True
    cfg.llm.monthly_budget_usd = 0.000001
    cfg.llm.providers = [provider(billing="paid", input_usd_per_million=1, output_usd_per_million=1)]

    def forbidden(_):
        pytest.fail("monthly budget must be reserved before network request")

    with pytest.raises(ModelUnavailable, match="预算"):
        await Extractor(cfg, db, transport=httpx.MockTransport(forbidden)).extract(sample())


async def test_responses_protocol(cfg, db):
    cfg.llm.providers = [provider(protocol="responses")]

    def handle(request):
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": ExtractionResult().model_dump_json()}],
                    }
                ]
            },
        )

    result = await Extractor(cfg, db, transport=httpx.MockTransport(handle)).extract(sample())
    assert result.deadline.value is None
