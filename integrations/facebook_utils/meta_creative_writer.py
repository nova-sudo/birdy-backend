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
from urllib.parse import urlparse
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


BLOB_SUFFIX = ".public.blob.vercel-storage.com"
MAX_IMAGE_BYTES = 30 * 1024 * 1024
IMAGE_TYPES = ("image/jpeg", "image/png")


META_CDN_SUFFIX = ".fbcdn.net"
VIDEO_PROCESS_FAILED = "Meta could not process the video"
AD_READ_PAGES = 20


def _check_blob_url(url: str, *, allow_meta_cdn: bool = False) -> None:
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    ok = host.endswith(BLOB_SUFFIX) or (allow_meta_cdn and host.endswith(META_CDN_SUFFIX))
    if u.scheme != "https" or not ok:
        raise MetaWriteError("Media must be an https Birdy upload URL", step="media")


async def _download_image(url: str, *, allow_meta_cdn: bool = False) -> bytes:
    _check_blob_url(url, allow_meta_cdn=allow_meta_cdn)
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as dl:
                if dl.status_code != 200:
                    raise MetaWriteError(f"Could not download image (HTTP {dl.status_code})",
                                         step="media")
                ctype = dl.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype not in IMAGE_TYPES:
                    raise MetaWriteError("Image must be image/jpeg or image/png", step="media")
                buf = bytearray()
                async for chunk in dl.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_IMAGE_BYTES:
                        raise MetaWriteError("Image is larger than 30 MB", step="media")
                return bytes(buf)
    except httpx.HTTPError as e:
        raise MetaWriteError(f"Could not download image: {e}", step="media") from e


async def upload_image(act: str, token: str, *, image_url: str, allow_meta_cdn: bool = False) -> str:
    """Download the image (Birdy blob host only) and post it base64-encoded; returns the hash.

    Revive creatives that already hold a Meta image_hash skip this call entirely and pass the
    hash as media_ref to create_adcreative. A revive into a different ad account (hashes are
    account-scoped) passes allow_meta_cdn=True to re-upload the source ad's *.fbcdn.net image
    under the same guards (https, no redirects, jpeg/png, 30 MB streaming cap).
    """
    content = await _download_image(image_url, allow_meta_cdn=allow_meta_cdn)
    data = await _request("POST", f"{GRAPH}/{act}/adimages", "upload_image",
                          data={"access_token": token,
                                "bytes": base64.b64encode(content).decode()})
    images = data.get("images") or {}
    first = next(iter(images.values()), None) or {}
    if not first.get("hash"):
        raise MetaWriteError("Meta returned no image hash", step="upload_image")
    return first["hash"]


async def upload_video(act: str, token: str, *, file_url: str, name: str, allow_meta_cdn: bool = False) -> str:
    """Meta pulls the file from `file_url`. `allow_meta_cdn` is for a source ad's own short-lived URL,
    posted into another account (video ids are account-scoped)."""
    _check_blob_url(file_url, allow_meta_cdn=allow_meta_cdn)
    data = await _request("POST", f"{GRAPH}/{act}/advideos", "upload_video",
                          data={"access_token": token, "file_url": file_url, "name": name},
                          timeout=300.0)
    if not data.get("id"):
        raise MetaWriteError("Meta returned no video id", step="upload_video")
    return data["id"]


async def video_source(video_id: str, token: str) -> Optional[str]:
    """The video's short-lived download URL. None when Meta holds it back, as it does for many ad videos."""
    data = await _request("GET", f"{GRAPH}/{video_id}", "video_source",
                          params={"access_token": token, "fields": "source"}, timeout=30.0)
    return data.get("source") or None


async def image_url_for_hash(act: str, token: str, image_hash: str) -> Optional[str]:
    """The URL of an image in `act`, the only account its hash means anything in."""
    data = await _request("GET", f"{GRAPH}/{act}/adimages", "image_url",
                          params={"access_token": token, "hashes": json.dumps([image_hash]), "fields": "hash,url"},
                          timeout=30.0)
    rows = data.get("data") or []
    return (rows[0].get("url") or None) if rows else None


async def video_ready(video_id: str, token: str) -> bool:
    data = await _request("GET", f"{GRAPH}/{video_id}", "video_status",
                          params={"access_token": token, "fields": "status"}, timeout=30.0)
    state = (data.get("status") or {}).get("video_status")
    if state == "error":
        raise MetaWriteError(VIDEO_PROCESS_FAILED, step="video_status")
    return state == "ready"


async def video_thumbnail(video_id: str, token: str) -> Optional[str]:
    """A thumbnail URL Meta generated for an uploaded video: the preferred one, else the
    first, else the video's `picture`. None when Meta has none yet."""
    data = await _request("GET", f"{GRAPH}/{video_id}/thumbnails", "video_thumbnail",
                          params={"access_token": token, "fields": "uri,is_preferred"},
                          timeout=30.0)
    thumbs = [t for t in data.get("data") or [] if isinstance(t, dict) and t.get("uri")]
    pick = next((t for t in thumbs if t.get("is_preferred")), thumbs[0] if thumbs else None)
    if pick:
        return pick["uri"]
    data = await _request("GET", f"{GRAPH}/{video_id}", "video_thumbnail",
                          params={"access_token": token, "fields": "picture"}, timeout=30.0)
    return data.get("picture") or None


async def create_adcreative(
    act: str, token: str, *, name: str, page_id: str, fmt: str,
    media_ref: Union[str, dict], primary_text: str, headline: str, description: str,
    cta_type: str, destination: str, website_url: Optional[str] = None,
    url_params: Optional[str] = None, lead_form_id: Optional[str] = None,
    whatsapp_number: Optional[str] = None,
) -> str:
    """media_ref: image hash (image), or video id / {"video_id", "image_url"} (video)."""
    if destination == "lead_form":
        link = "http://fb.me/"
        value = {"lead_gen_form_id": lead_form_id, "link": link}
    elif destination == "whatsapp":
        # whatsapp_number is informational only; Meta resolves the number from the Page/ad set.
        link = "https://api.whatsapp.com/send"
        value = {"app_destination": "WHATSAPP"}
        cta_type = "WHATSAPP_MESSAGE"
    else:
        if not website_url:
            raise MetaWriteError("A website URL is required", step="creative")
        link = website_url
        value = {"link": link}
    cta = {"type": cta_type, "value": value}

    if fmt == "image":
        story = {"link_data": {"image_hash": media_ref, "link": link, "message": primary_text,
                               "name": headline, "description": description,
                               "call_to_action": cta}}
    else:
        ref = media_ref if isinstance(media_ref, dict) else {"video_id": media_ref}
        if not (ref.get("image_url") or ref.get("image_hash")):
            raise MetaWriteError("A video ad needs a thumbnail image", step="creative")
        video = {"video_id": ref["video_id"], "message": primary_text, "title": headline,
                 "link_description": description, "call_to_action": cta}
        for k in ("image_url", "image_hash"):
            if ref.get(k):
                video[k] = ref[k]
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


async def find_ad_in_adset(adset_id: str, token: str, *, name: str, creative_id: str) -> Optional[str]:
    """An ad already in this ad set that a lost `create_ad` reply left behind: the one on this creative, else
    the one with exactly this name (a retry can have rebuilt the creative). None when there isn't one. Raises
    MetaWriteError when the whole ad set couldn't be read, since then nobody can say there's none."""
    params = {"access_token": token, "fields": "id,name,creative{id}", "limit": 200}
    by_name = None
    for _ in range(AD_READ_PAGES):
        data = await _request("GET", f"{GRAPH}/{adset_id}/ads", "read_adset_ads", params=params, timeout=30.0)
        for ad in data.get("data") or []:
            if str((ad.get("creative") or {}).get("id")) == str(creative_id):
                return ad["id"]
            if by_name is None and ad.get("name") == name:
                by_name = ad["id"]
        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not (paging.get("next") and after):
            return by_name
        params = {**params, "after": after}
    raise MetaWriteError("Too many ads in this ad set to check", step="read_adset_ads")
