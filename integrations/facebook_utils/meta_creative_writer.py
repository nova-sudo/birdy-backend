"""
Meta Graph writes for the creative-test posting wizard: upload media, create the
ad creative, create the ad. Every ad is created PAUSED; going live is a separate pass.

Graph error payload (verified against Meta's error reference and the existing
readers): HTTP 4xx with
    {"error": {"message", "type", "code", "error_subcode",
               "error_user_title", "error_user_msg", "fbtrace_id"}}
Failures raise MetaWriteError tagged with the wizard `step` that failed.
"""

import base64
import json
from typing import Optional, Union

import httpx

from integrations.facebook_utils.meta_creative_reader import GRAPH, RETRYABLE_CODES


class MetaWriteError(Exception):
    def __init__(self, message: str, *, code: Optional[int] = None, subcode: Optional[int] = None,
                 user_msg: Optional[str] = None, step: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.user_msg = user_msg
        self.step = step

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def _int(v) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _error_from(resp: httpx.Response, step: str) -> MetaWriteError:
    try:
        body = resp.json()
    except Exception:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    code = _int(err.get("code"))
    if resp.status_code == 429 and code is None:
        code = 17
    return MetaWriteError(err.get("message") or f"Meta HTTP {resp.status_code}", code=code,
                          subcode=_int(err.get("error_subcode")),
                          user_msg=err.get("error_user_msg") or None, step=step)


async def _request(method: str, url: str, step: str, *, params=None, data=None, timeout=60.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, params=params, data=data)
    except httpx.HTTPError as e:
        raise MetaWriteError(f"Meta request failed: {e}", step=step) from e
    if resp.status_code != 200:
        raise _error_from(resp, step)
    return resp.json()


async def upload_image(act: str, token: str, *, image_url: str) -> str:
    """Download the image and post it base64-encoded; returns the image hash."""
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            dl = await client.get(image_url)
            dl.raise_for_status()
    except httpx.HTTPError as e:
        raise MetaWriteError(f"Could not download image: {e}", step="upload_image") from e
    data = await _request("POST", f"{GRAPH}/{act}/adimages", "upload_image",
                          data={"access_token": token,
                                "bytes": base64.b64encode(dl.content).decode()})
    images = data.get("images") or {}
    first = next(iter(images.values()), None) or {}
    if not first.get("hash"):
        raise MetaWriteError("Meta returned no image hash", step="upload_image")
    return first["hash"]


async def upload_video(act: str, token: str, *, file_url: str, name: str) -> str:
    data = await _request("POST", f"{GRAPH}/{act}/advideos", "upload_video",
                          data={"access_token": token, "file_url": file_url, "name": name},
                          timeout=300.0)
    if not data.get("id"):
        raise MetaWriteError("Meta returned no video id", step="upload_video")
    return data["id"]


async def video_ready(video_id: str, token: str) -> bool:
    data = await _request("GET", f"{GRAPH}/{video_id}", "video_status",
                          params={"access_token": token, "fields": "status"}, timeout=30.0)
    state = (data.get("status") or {}).get("video_status")
    if state == "error":
        raise MetaWriteError("Meta could not process the video", step="video_status")
    return state == "ready"


def _whatsapp_link(number: Optional[str]) -> str:
    digits = "".join(c for c in (number or "") if c.isdigit())
    return f"https://api.whatsapp.com/send?phone={digits}"


async def create_adcreative(
    act: str, token: str, *, name: str, page_id: str, fmt: str,
    media_ref: Union[str, dict], primary_text: str, headline: str, description: str,
    cta_type: str, destination: str, website_url: Optional[str] = None,
    url_params: Optional[str] = None, lead_form_id: Optional[str] = None,
    whatsapp_number: Optional[str] = None,
) -> str:
    """media_ref: image hash (image), or video id / {"video_id", "image_url"} (video)."""
    if destination == "lead_form":
        value = {"lead_gen_form_id": lead_form_id}
        link = website_url or "https://fb.me/"
    elif destination == "whatsapp":
        link = _whatsapp_link(whatsapp_number)
        value = {"app_destination": "WHATSAPP", "link": link}
    else:
        link = website_url
        value = {"link": link}
    cta = {"type": cta_type, "value": value}

    if fmt == "image":
        story = {"link_data": {"image_hash": media_ref, "link": link, "message": primary_text,
                               "name": headline, "description": description,
                               "call_to_action": cta}}
    else:
        ref = media_ref if isinstance(media_ref, dict) else {"video_id": media_ref}
        video = {"video_id": ref["video_id"], "message": primary_text, "title": headline,
                 "link_description": description, "call_to_action": cta}
        if ref.get("image_url"):
            video["image_url"] = ref["image_url"]
        story = {"video_data": video}
    story["page_id"] = page_id

    body = {"access_token": token, "name": name, "object_story_spec": json.dumps(story)}
    if url_params:
        body["url_tags"] = url_params
    data = await _request("POST", f"{GRAPH}/{act}/adcreatives", "create_creative", data=body)
    return data["id"]


async def create_ad(act: str, token: str, *, name: str, adset_id: str, creative_id: str) -> str:
    """Always PAUSED: go-live is a separate pass."""
    data = await _request("POST", f"{GRAPH}/{act}/ads", "create_ad", data={
        "access_token": token, "name": name, "adset_id": adset_id,
        "creative": json.dumps({"creative_id": creative_id}), "status": "PAUSED"})
    return data["id"]
