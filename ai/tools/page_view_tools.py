"""
Saved views for the hub tables (Clients, Leads, Marketing, Sales), made from chat.

A saved view is a named column set. The columns a hub offers are built in the
browser (GHL custom fields and tags among them), so each hub saves its list to
the user's account when it renders (users.view_catalogues), and the chat
request also carries the lists this browser has seen, which win as the
freshest:

    { "<page key>": { "label": "Leads Hub", "path": "/contacts",
                      "columns": [ { "id", "label", "source" }, ... ] } }

The orchestrator injects that map, plus a `page_view_ops_sink` list the tools
append to, so the open page can show and apply a new view without a reload.
"""

from fastapi import HTTPException

from ai.tools.registry import registry
from services.page_views_service import create_page_view, get_view_catalogues, set_default_page_view

LIST_TOOL = "list_view_columns"
CREATE_TOOL = "create_saved_view"
PAGE_VIEW_TOOL_NAMES = {LIST_TOOL, CREATE_TOOL}


async def _pages(db, user_id, view_catalogues) -> dict:
    sent = view_catalogues if isinstance(view_catalogues, dict) else {}
    return {**await get_view_catalogues(db, user_id), **sent}


def _resolve(page, pages: dict):
    """A page key, or the hub's name as the user said it ("marketing hub")."""
    if page in pages:
        return page, None
    needle = " ".join(str(page or "").lower().replace("_", " ").split())
    if not needle:
        return None, None
    hits = [k for k, v in pages.items()
            if str(v.get("label", "")).lower() == needle
            or str(v.get("label", "")).lower().startswith(needle)]
    if len(hits) == 1:
        return hits[0], None
    if hits:
        options = ", ".join(f'{k} ({pages[k].get("label", k)})' for k in hits)
        return None, {"ok": False, "error": f'"{page}" matches several tables: {options}. '
                                            "Ask which one, or pick the one the user means."}
    return None, None


def _no_page(page, pages: dict) -> dict:
    known = ", ".join(f'{k} ({v.get("label", k)})' for k, v in pages.items()) or "none yet"
    return {
        "ok": False,
        "error": f'No column list for page "{page}". Known pages: {known}. '
                 "If the hub the user means is missing, it has not been opened since "
                 "this feature shipped: ask them to open (or reload) that hub once.",
    }


async def list_view_columns(db, user_id, view_catalogues=None, page=None, search=None, **_):
    pages = await _pages(db, user_id, view_catalogues)
    if not page:
        return {
            "ok": True,
            "pages": [
                {"page": k, "label": v.get("label", k), "column_count": len(v.get("columns") or [])}
                for k, v in pages.items()
            ],
        }
    key, ambiguous = _resolve(page, pages)
    if ambiguous:
        return ambiguous
    if key is None:
        return _no_page(page, pages)
    page = key
    columns = pages[page].get("columns") or []
    if search:
        needle = search.lower()
        columns = [c for c in columns
                   if needle in str(c.get("label", "")).lower() or needle in str(c.get("id", "")).lower()]
    return {"ok": True, "page": page, "label": pages[page].get("label", page), "columns": columns}


async def create_saved_view(db, user_id, view_catalogues=None, page_view_ops_sink=None,
                            page=None, name=None, column_ids=None, make_default=False, **_):
    pages = await _pages(db, user_id, view_catalogues)
    key, ambiguous = _resolve(page, pages)
    if ambiguous:
        return ambiguous
    if key is None:
        return _no_page(page, pages)
    page = key
    entry = pages[page]
    known = {c.get("id") for c in entry.get("columns") or []}

    ids = []
    for cid in column_ids or []:
        if cid not in ids:
            ids.append(cid)
    unknown = [cid for cid in ids if cid not in known]
    if unknown:
        return {"ok": False, "error": f"Unknown column ids for {page}: {unknown}. "
                                      f"Use {LIST_TOOL} to get exact ids."}
    if not ids:
        return {"ok": False, "error": "A view needs at least one column."}

    try:
        view = await create_page_view(db, user_id, page, name, {"visibleColumns": ids, "source": "all"})
        if make_default:
            await set_default_page_view(db, user_id, page, view["id"])
    except HTTPException as e:
        return {"ok": False, "error": e.detail}

    label = entry.get("label", page)
    if isinstance(page_view_ops_sink, list):
        page_view_ops_sink.append({
            "type": "view_created",
            "page": page,
            "page_label": label,
            "path": entry.get("path"),
            "view": view,
            "made_default": bool(make_default),
        })
    by_id = {c.get("id"): c.get("label") for c in entry.get("columns") or []}
    return {
        "ok": True,
        "view_id": view["id"],
        "name": view["name"],
        "page_label": label,
        "columns": [by_id.get(cid, cid) for cid in ids],
        "made_default": bool(make_default),
    }


def register_page_view_tools(reg=registry):
    reg.register(
        LIST_TOOL,
        "List the hub pages the user can save table views on, or the columns one page offers. "
        "Call with no page to see the pages (Clients Hub, Leads Hub, each Marketing Hub and "
        "Sales Hub tab); call with a page to get its exact column ids before create_saved_view. "
        "`search` filters columns by label or id.",
        {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page key from the no-page listing, e.g. contacts, mktg_campaigns"},
                "search": {"type": "string", "description": "Optional case-insensitive filter on column label or id"},
            },
        },
        list_view_columns,
    )
    reg.register(
        CREATE_TOOL,
        "Save a named table view (a column set, in order) on a hub page, as the user would from "
        "the page's Views menu. Use column ids exactly as list_view_columns returns them. If the "
        "user is on that page the view is applied straight away. Set make_default only when the "
        "user asks for the page to open on it.",
        {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page key, e.g. clients, contacts, mktg_campaigns, cc_leads"},
                "name": {"type": "string", "description": "View name, 60 characters at most"},
                "column_ids": {"type": "array", "items": {"type": "string"},
                               "description": "Columns to show, in display order"},
                "make_default": {"type": "boolean", "description": "Open the page on this view"},
            },
            "required": ["page", "name", "column_ids"],
        },
        create_saved_view,
    )
