"""
sandbox/ghl_search.py
---------------------
The query language of ``POST /contacts/search`` (filters, sort, page / pageLimit, searchAfter,
query) and the opportunity search filters, applied to the GHL JSON from sandbox.ghl_data.

Filters, as LeadConnector takes them:

    {"field": "dateUpdated", "operator": "range", "value": {"gte": "2026-11-20T00:00:00.000Z"}}
    {"field": "tags", "operator": "eq", "value": ["booked"]}
    {"group": "OR", "filters": [...]}

Date fields accept only ``range`` (and ``exists`` / ``not_exists``); anything else is a 422, as the
live API answers (see integrations/gohighlevel.fetch_contacts_search).
"""

from __future__ import annotations

from typing import Optional

from sandbox.fakes import ProviderError
from sandbox.ghl_data import ms, parse_when

DATE_FIELDS = {"dateAdded", "dateUpdated", "lastActivity", "dateOfBirth"}
OPERATORS = {"eq", "not_eq", "contains", "not_contains", "exists", "not_exists", "range"}
MAX_PAGE_LIMIT = 500
MAX_WINDOW = 10000        # page * pageLimit beyond this needs searchAfter
SORT_FIELDS = {"dateAdded", "dateUpdated", "firstNameLowerCase", "lastNameLowerCase", "firstName",
               "lastName", "email", "businessName", "fullNameLowerCase"}


def unprocessable(message: str):
    return ProviderError(422, message)


def _value(contact: dict, field: str):
    if field.startswith("customFields.") or field.startswith("customField."):
        fid = field.split(".", 1)[1]
        return next((f.get("value") for f in contact.get("customFields") or [] if f.get("id") == fid), None)
    if field in ("firstNameLowerCase", "lastNameLowerCase", "fullNameLowerCase", "emailLowerCase"):
        return contact.get(field)
    if "." in field:
        head, rest = field.split(".", 1)
        inner = contact.get(head)
        return inner.get(rest) if isinstance(inner, dict) else None
    return contact.get(field)


def _norm(v):
    return v.strip().lower() if isinstance(v, str) else v


def _match_one(contact: dict, f: dict) -> bool:
    if not isinstance(f, dict):
        raise unprocessable("each filter must be an object")
    if "group" in f or "filters" in f:
        group = str(f.get("group") or "AND").upper()
        parts = [_match_one(contact, g) for g in f.get("filters") or []]
        return any(parts) if group == "OR" else all(parts)
    field, op, value = f.get("field"), str(f.get("operator") or "").lower(), f.get("value")
    if not field:
        raise unprocessable("filters.field must be a string")
    if op not in OPERATORS:
        raise unprocessable(f"filters.operator must be one of the following values: {', '.join(sorted(OPERATORS))}")
    actual = _value(contact, field)
    if op == "exists":
        return actual not in (None, "", [])
    if op == "not_exists":
        return actual in (None, "", [])
    if field in DATE_FIELDS:
        if op != "range":
            raise unprocessable(f"{field} supports only the range operator")
        if not isinstance(value, dict) or not set(value) & {"gt", "gte", "lt", "lte"}:
            raise unprocessable("range value must have gt, gte, lt or lte")
        at = parse_when(actual)
        if at is None:
            return False
        bounds = {k: parse_when(v) for k, v in value.items() if k in ("gt", "gte", "lt", "lte")}
        if any(b is None for b in bounds.values()):
            raise unprocessable("range bounds must be ISO dates or epoch milliseconds")
        return ((("gt" not in bounds) or at > bounds["gt"]) and (("gte" not in bounds) or at >= bounds["gte"])
                and (("lt" not in bounds) or at < bounds["lt"]) and (("lte" not in bounds) or at <= bounds["lte"]))
    if op == "range":
        if not isinstance(value, dict):
            raise unprocessable("range value must be an object")
        try:
            num = float(actual)
        except (TypeError, ValueError):
            return False
        return all({"gt": num > v, "gte": num >= v, "lt": num < v, "lte": num <= v}[k]
                   for k, v in ((k, float(v)) for k, v in value.items() if k in ("gt", "gte", "lt", "lte")))
    wanted = value if isinstance(value, list) else [value]
    wanted = [_norm(x) for x in wanted]
    if isinstance(actual, list):
        have = [_norm(x) for x in actual]
        if op == "eq":
            return any(x in have for x in wanted)
        if op == "not_eq":
            return not any(x in have for x in wanted)
        hit = any(isinstance(h, str) and any(isinstance(x, str) and x in h for x in wanted) for h in have)
        return hit if op == "contains" else not hit
    a = _norm(actual)
    if op == "eq":
        return a in wanted
    if op == "not_eq":
        return a not in wanted
    hit = isinstance(a, str) and any(isinstance(x, str) and x in a for x in wanted)
    return hit if op == "contains" else not hit


def query_match(contact: dict, q: str) -> bool:
    q = q.strip().lower()
    hay = " ".join(str(contact.get(k) or "") for k in ("firstName", "lastName", "email", "phone", "companyName")).lower()
    digits = "".join(ch for ch in q if ch.isdigit()) if not any(ch.isalpha() for ch in q) else ""
    return q in hay or (len(digits) >= 4 and digits in "".join(ch for ch in str(contact.get("phone") or "") if ch.isdigit()))


def _sort_value(contact: dict, field: str):
    v = _value(contact, field)
    if field in DATE_FIELDS:
        return ms(parse_when(v)) or 0
    return (v or "").lower() if isinstance(v, str) else (v if v is not None else "")


def search_contacts(contacts: list, body: dict) -> dict:
    """The /contacts/search response for ``contacts`` (newest first) and a request body."""
    try:
        page_limit = int(body.get("pageLimit") or body.get("limit") or 20)
        page = int(body.get("page") or 1)
    except (TypeError, ValueError):
        raise unprocessable("page and pageLimit must be numbers")
    if page_limit < 1 or page_limit > MAX_PAGE_LIMIT:
        raise unprocessable(f"pageLimit must not be greater than {MAX_PAGE_LIMIT}")
    if page < 1:
        raise unprocessable("page must not be less than 1")
    filters = body.get("filters") or []
    if not isinstance(filters, list):
        raise unprocessable("filters must be an array")
    sort = body.get("sort") or [{"field": "dateAdded", "direction": "desc"}]
    if not isinstance(sort, list) or not sort:
        raise unprocessable("sort must be an array")
    for s in sort:
        if not isinstance(s, dict) or s.get("field") not in SORT_FIELDS:
            raise unprocessable(f"sort.field must be one of the following values: {', '.join(sorted(SORT_FIELDS))}")
        if str(s.get("direction") or "desc").lower() not in ("asc", "desc"):
            raise unprocessable("sort.direction must be one of the following values: asc, desc")

    rows = [c for c in contacts if all(_match_one(c, f) for f in filters)]
    q = body.get("query")
    if isinstance(q, str) and q.strip():
        rows = [c for c in rows if query_match(c, q)]
    # Stable multi-key sort: apply the keys from the last to the first.
    rows = sorted(rows, key=lambda c: c["id"])
    for s in reversed(sort):
        rows.sort(key=lambda c, f=s["field"]: _sort_value(c, f), reverse=str(s.get("direction") or "desc").lower() == "desc")
    primary = sort[0]["field"]

    search_after = body.get("searchAfter")
    if search_after:
        if not isinstance(search_after, list) or len(search_after) < 2:
            raise unprocessable("searchAfter must be an array of [sort value, id]")
        last_id = str(search_after[-1])
        idx = next((i for i, c in enumerate(rows) if c["id"] == last_id), None)
        if idx is None:
            desc = str(sort[0].get("direction") or "desc").lower() == "desc"
            pivot = search_after[0]
            idx = next((i - 1 for i, c in enumerate(rows)
                        if (_sort_value(c, primary) < pivot if desc else _sort_value(c, primary) > pivot)),
                       len(rows) - 1)
        chunk = rows[idx + 1: idx + 1 + page_limit]
    else:
        if (page - 1) * page_limit + page_limit > MAX_WINDOW:
            raise unprocessable(f"page * pageLimit must not exceed {MAX_WINDOW}; use searchAfter")
        chunk = rows[(page - 1) * page_limit: page * page_limit]
    out = []
    for c in chunk:
        item = dict(c)
        item["searchAfter"] = [_sort_value(c, primary), c["id"]]
        out.append(item)
    return {"contacts": out, "total": len(rows)}


def cursor_page(rows: list, *, limit: int, start_after: Optional[int] = None,
                start_after_id: Optional[str] = None, time_field: str = "dateAdded") -> tuple:
    """The ``startAfter`` / ``startAfterId`` page of ``rows`` (already in order). Returns
    (chunk, next_start_after_ms, next_start_after_id)."""
    start = 0
    if start_after_id:
        idx = next((i for i, r in enumerate(rows) if r["id"] == start_after_id), None)
        if idx is not None:
            start = idx + 1
        elif start_after is not None:
            start = next((i for i, r in enumerate(rows) if (ms(parse_when(r.get(time_field))) or 0) < start_after),
                         len(rows))
    elif start_after is not None:
        start = next((i for i, r in enumerate(rows) if (ms(parse_when(r.get(time_field))) or 0) < start_after), len(rows))
    chunk = rows[start: start + limit]
    more = start + limit < len(rows)
    if chunk and more:
        last = chunk[-1]
        return chunk, ms(parse_when(last.get(time_field))), last["id"]
    return chunk, None, None
