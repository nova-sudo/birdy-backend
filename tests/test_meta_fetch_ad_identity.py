import httpx
import pytest

from services import meta_service


def _patch_client(monkeypatch, handler, seen=None):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        if seen is not None:
            seen["timeout"] = kw.get("timeout")
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(meta_service.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_returns_names_and_creative(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "id": "a1", "name": "Ad", "adset": {"id": "s1", "name": "Set"},
            "campaign": {"id": "k1", "name": "Camp"},
            "creative": {"title": "T", "body": "B", "image_url": "img", "thumbnail_url": "th"}})

    _patch_client(monkeypatch, handler, seen)
    r = await meta_service.fetch_ad_identity("a1", "tok")
    assert r["name"] == "Ad" and r["adset_id"] == "s1" and r["adset_name"] == "Set"
    assert r["campaign_id"] == "k1" and r["campaign_name"] == "Camp"
    assert r["creative_image"] == "img" and r["creative_body"] == "B"
    assert "/v25.0/a1" in seen["url"] and seen["timeout"] == 4.0


@pytest.mark.asyncio
async def test_http_error_returns_none(monkeypatch):
    _patch_client(monkeypatch, lambda req: httpx.Response(400, json={"error": {}}))
    assert await meta_service.fetch_ad_identity("a1", "tok") is None


@pytest.mark.asyncio
async def test_network_error_returns_none_and_never_logs_token(monkeypatch, caplog):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    _patch_client(monkeypatch, handler)
    with caplog.at_level("DEBUG"):
        assert await meta_service.fetch_ad_identity("a1", "SECRET-TOKEN") is None
    assert "SECRET-TOKEN" not in caplog.text
