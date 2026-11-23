"""
routers/sandbox.py
------------------
Public placeholder media for sandbox accounts (see sandbox/meta_media.py). The Meta fake hands out
creative image / thumbnail / video picture / ad preview URLs on this route, so a browser can load
them in an <img> or <iframe>. The path is a signed sandbox ``media`` credential: only an object of a
real sandbox world can be drawn, and only from its fake names. No auth (an <img> sends none).
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter()

_HEADERS = {
    "cache-control": "public, max-age=86400",
    # An SVG opened on its own is a document: let it draw, nothing else.
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    "x-content-type-options": "nosniff",
}


@router.get("/api/sandbox/media/{name}")
async def sandbox_media(name: str):
    from sandbox import meta_media

    token = name[:-4] if name.endswith(".svg") else name
    info = meta_media.read_token(token)
    if info is None:
        raise HTTPException(status_code=404, detail="Not found")
    user_id, what, object_id = info
    described = await meta_media.describe(user_id, what, object_id)
    if described is None:
        raise HTTPException(status_code=404, detail="Not found")
    title, subtitle, kind = described
    return Response(meta_media.svg(object_id, title, subtitle, what=kind), media_type="image/svg+xml",
                    headers=_HEADERS)
