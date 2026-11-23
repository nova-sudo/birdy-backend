"""
sandbox/meta_media.py
---------------------
Placeholder creative media for sandbox accounts. Two kinds of URL:

- ``public_url``: on the backend's own public base (``API_PUBLIC_URL``), served by
  ``routers/sandbox.py`` as a generated SVG. These go where a browser loads them: creative
  ``image_url`` / ``thumbnail_url``, a video's ``picture`` and thumbnails, ad previews.
- ``cdn_url``: on ``scontent.xx.fbcdn.net`` with an ``sbx_`` path segment. These are URLs the
  backend itself downloads (``/act/adimages`` ``url``, which the creative writer re-uploads with
  ``allow_meta_cdn``): the httpx hook answers them in-process from the Meta fake, which serves a
  small PNG. They never reach a real Meta host.

The path segment is a signed ``media`` credential whose nonce is ``<what>.<object id>``, so the
public route can only draw an object of a real sandbox world, from its fake names.
"""

from __future__ import annotations

import hashlib
import html
import struct
import zlib
from typing import Optional

from sandbox.credentials import make_credential, parse_credential

CDN_HOST = "scontent.xx.fbcdn.net"
WHATS = ("image", "thumb", "preview", "video")


def public_base() -> str:
    from core.config import API_PUBLIC_URL

    return API_PUBLIC_URL.rstrip("/")


def media_token(user_id: str, what: str, object_id: str) -> str:
    return make_credential(user_id, "media", nonce=f"{what}.{object_id}")


def public_url(user_id: str, what: str, object_id: str) -> str:
    """A browser-loadable placeholder (SVG) for an object of the account's world."""
    return f"{public_base()}/api/sandbox/media/{media_token(user_id, what, object_id)}.svg"


def cdn_url(user_id: str, object_id: str, ext: str = "png") -> str:
    """A placeholder the backend may download: answered in-process by the Meta fake."""
    return f"https://{CDN_HOST}/sandbox/{media_token(user_id, 'image', object_id)}/{object_id}.{ext}"


def read_token(token: str):
    """(user_id, what, object_id) for a media token, or None."""
    cred = parse_credential(token)
    if cred is None or cred.kind != "media" or "." not in cred.nonce:
        return None
    what, _, object_id = cred.nonce.partition(".")
    return cred.user_id, what, object_id


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _hue(object_id: str) -> int:
    return int.from_bytes(hashlib.blake2b(str(object_id).encode(), digest_size=2).digest(), "big") % 360


def rgb(object_id: str) -> tuple:
    """A flat colour from the id (HSL with fixed saturation / lightness)."""
    import colorsys

    r, g, b = colorsys.hls_to_rgb(_hue(object_id) / 360.0, 0.42, 0.55)
    return int(r * 255), int(g * 255), int(b * 255)


def _wrap(text: str, width: int, lines: int) -> list:
    words, out, cur = (text or "").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][: max(0, width - 1)].rstrip() + "…"
    return out


def svg(object_id: str, title: str, subtitle: str, *, what: str = "image") -> str:
    """The placeholder: a flat colour from the id, the client name and the ad / creative name."""
    r, g, b = rgb(object_id)
    fill = f"rgb({r},{g},{b})"
    esc = html.escape
    if what == "preview":
        w, h = 540, 690
        title_lines = _wrap(title, 30, 2)
        body = "".join(
            f'<text x="270" y="{455 + 34 * k}" font-size="26" fill="#fff" text-anchor="middle" '
            f'font-weight="600">{esc(t)}</text>' for k, t in enumerate(title_lines))
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<rect width="{w}" height="{h}" fill="#ffffff"/>'
            f'<circle cx="40" cy="40" r="20" fill="{fill}"/>'
            f'<text x="72" y="36" font-family="Helvetica,Arial,sans-serif" font-size="16" font-weight="700" '
            f'fill="#1c1e21">{esc(subtitle)}</text>'
            f'<text x="72" y="56" font-family="Helvetica,Arial,sans-serif" font-size="13" fill="#65676b">'
            f'Sponsored · Sandbox</text>'
            f'<rect x="0" y="80" width="{w}" height="540" fill="{fill}"/>'
            f'<g font-family="Helvetica,Arial,sans-serif">{body}</g>'
            f'<rect x="380" y="636" width="140" height="38" rx="6" fill="#e4e6eb"/>'
            f'<text x="450" y="661" font-family="Helvetica,Arial,sans-serif" font-size="15" font-weight="600" '
            f'fill="#1c1e21" text-anchor="middle">Learn more</text>'
            f'</svg>')
    w = h = 1080
    title_lines = _wrap(title, 22, 3)
    texts = "".join(
        f'<text x="540" y="{470 + 78 * k}" font-size="64" font-weight="700" fill="#fff" '
        f'text-anchor="middle">{esc(t)}</text>' for k, t in enumerate(title_lines))
    badge = "▶ VIDEO" if what in ("video", "thumb_video") else "SANDBOX"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<rect width="{w}" height="{h}" fill="{fill}"/>'
        f'<rect x="60" y="60" width="960" height="960" rx="40" fill="none" stroke="#fff" '
        f'stroke-opacity="0.35" stroke-width="6"/>'
        f'<g font-family="Helvetica,Arial,sans-serif">'
        f'<text x="540" y="260" font-size="40" fill="#fff" fill-opacity="0.85" text-anchor="middle" '
        f'letter-spacing="6">{esc(badge)}</text>{texts}'
        f'<text x="540" y="900" font-size="38" fill="#fff" fill-opacity="0.9" text-anchor="middle">'
        f'{esc(subtitle)}</text></g></svg>')


def png(object_id: str, size: int = 64) -> bytes:
    """A small flat-colour PNG (no imaging library needed)."""
    r, g, b = rgb(object_id)
    row = b"\x00" + bytes((r, g, b)) * size
    raw = row * size

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


async def describe(user_id: str, what: str, object_id: str) -> Optional[tuple]:
    """(title, subtitle, kind) for the object behind a media token, from the world's fake names
    (or a creative / ad / video the account created), or None when there is no such object."""
    from sandbox import meta_graph

    return await meta_graph.describe_media(user_id, what, object_id)
