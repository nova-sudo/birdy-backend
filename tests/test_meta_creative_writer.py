"""Meta write calls: request bodies and error parsing."""

import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest

from integrations.facebook_utils import meta_creative_writer as w


REAL = httpx.AsyncClient


@pytest.fixture
def serve(monkeypatch):
    def apply(responses, seen):
        calls = {"n": 0}

        def handler(request):
            body = parse_qs(request.content.decode()) if request.content else {}
            seen.append({"method": request.method, "url": str(request.url),
                         "body": {k: v[0] for k, v in body.items()}})
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            if "content" in r:
                return httpx.Response(r.get("status", 200), content=r["content"])
            return httpx.Response(r.get("status", 200), json=r["json"])

        real = REAL
        monkeypatch.setattr(
            "integrations.facebook_utils.meta_creative_writer.httpx.AsyncClient",
            lambda *a, **kw: real(transport=httpx.MockTransport(handler),
                                  **{k: v for k, v in kw.items() if k != "timeout"}))
    return apply


async def test_upload_image_posts_base64_and_returns_hash(serve):
    seen = []
    serve([{"content": b"PNGDATA"}, {"json": {"images": {"x.png": {"hash": "H1"}}}}], seen)
    assert await w.upload_image("act_1", "tok", image_url="https://cdn/x.png") == "H1"
    assert seen[1]["url"].endswith("/v25.0/act_1/adimages")
    assert base64.b64decode(seen[1]["body"]["bytes"]) == b"PNGDATA"


async def test_upload_video_and_ready(serve):
    seen = []
    serve([{"json": {"id": "V1"}}], seen)
    assert await w.upload_video("act_1", "tok", file_url="https://b/v.mp4", name="CT-0001 | x") == "V1"
    assert seen[0]["url"].endswith("/act_1/advideos") and seen[0]["body"]["file_url"] == "https://b/v.mp4"
    serve([{"json": {"status": {"video_status": "processing"}}}], seen)
    assert await w.video_ready("V1", "tok") is False
    serve([{"json": {"status": {"video_status": "ready"}}}], seen)
    assert await w.video_ready("V1", "tok") is True
    serve([{"json": {"status": {"video_status": "error"}}}], seen)
    with pytest.raises(w.MetaWriteError):
        await w.video_ready("V1", "tok")


COMMON = dict(name="CT-0001 | x", page_id="P1", primary_text="pt", headline="h", description="d",
              cta_type="SIGN_UP")


async def creative_spec(serve, **kw):
    seen = []
    serve([{"json": {"id": "CR1"}}], seen)
    assert await w.create_adcreative("act_1", "tok", **COMMON, **kw) == "CR1"
    assert seen[0]["url"].endswith("/act_1/adcreatives")
    return json.loads(seen[0]["body"]["object_story_spec"]), seen[0]["body"]


async def test_image_lead_form_creative(serve):
    spec, _ = await creative_spec(serve, fmt="image", media_ref="HASH", destination="lead_form",
                                  lead_form_id="F1")
    ld = spec["link_data"]
    assert spec["page_id"] == "P1" and ld["image_hash"] == "HASH"
    assert ld["call_to_action"] == {"type": "SIGN_UP", "value": {"lead_gen_form_id": "F1"}}


async def test_video_website_creative_with_utm(serve):
    spec, body = await creative_spec(
        serve, fmt="video", media_ref={"video_id": "V1", "image_url": "https://t/th.jpg"},
        destination="website", website_url="https://x.com", url_params="utm_source=fb")
    vd = spec["video_data"]
    assert vd["video_id"] == "V1" and vd["image_url"] == "https://t/th.jpg"
    assert vd["call_to_action"]["value"] == {"link": "https://x.com"}
    assert body["url_tags"] == "utm_source=fb"


async def test_whatsapp_creative(serve):
    spec, _ = await creative_spec(serve, fmt="image", media_ref="H", destination="whatsapp",
                                  whatsapp_number="+44 7700 900123")
    cta = spec["link_data"]["call_to_action"]["value"]
    assert cta["app_destination"] == "WHATSAPP" and cta["link"].endswith("phone=447700900123")


async def test_create_ad_is_always_paused(serve):
    seen = []
    serve([{"json": {"id": "AD1"}}], seen)
    assert await w.create_ad("act_1", "tok", name="CT-0001 | x", adset_id="AS1", creative_id="CR1") == "AD1"
    b = seen[0]["body"]
    assert b["status"] == "PAUSED" and b["adset_id"] == "AS1"
    assert json.loads(b["creative"]) == {"creative_id": "CR1"}


async def test_error_payload_parsed_with_step(serve):
    serve([{"status": 400, "json": {"error": {
        "message": "Invalid parameter", "code": 100, "error_subcode": 1487,
        "error_user_title": "T", "error_user_msg": "The form does not belong to this page"}}}], [])
    with pytest.raises(w.MetaWriteError) as ei:
        await w.create_ad("act_1", "tok", name="n", adset_id="a", creative_id="c")
    e = ei.value
    assert (e.code, e.subcode, e.step) == (100, 1487, "create_ad")
    assert e.user_msg == "The form does not belong to this page" and not e.retryable


async def test_rate_limit_retryable(serve):
    serve([{"status": 400, "json": {"error": {"message": "slow", "code": 17}}}], [])
    with pytest.raises(w.MetaWriteError) as ei:
        await w.create_ad("act_1", "tok", name="n", adset_id="a", creative_id="c")
    assert ei.value.retryable
