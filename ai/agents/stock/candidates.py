"""
ai/agents/stock/candidates.py
-----------------------------
Allowed candidates for one client (spec section 6, Allowed candidates). Code builds the lists and
the AI can only choose from them. Adds come from other clients sharing a treatment tag, ranked by
CPL divided by the source client's green limit so currencies never mix. Nothing here is converted.
"""

import re
from datetime import datetime, timedelta

from ai.agents.snapshot import _to_date, load_ad_days
from ai.agents.stock.checks import cpl, in_scope, scoped_days, totals
from ai.agents.stock.scoreboard import record_for
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings, resolve_zones
from ai.agents.stock.windows import Window

REVIVE_LOOKBACK_DAYS = 365
_REVIVABLE = {"PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED"}  # off by choice; never ARCHIVED
_NOT_COPYABLE = {"DISAPPROVED"}
_SEP = re.compile(r"\s*[-\u2013\u2014]\s*")
_DATE_RUN = re.compile(r"(?<!\d)(?:\d{6}|\d{8})(?!\d)")
_DATE_SEP = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
_COPY = re.compile(r"\s*\(?\bcopy(?:\s*\d+)?\)?\s*$")
_DATE_FORMATS = {6: ("%d%m%y", "%m%d%y"), 8: ("%d%m%Y", "%m%d%Y", "%Y%m%d")}


def _is_date(run: str) -> bool:
    for fmt in _DATE_FORMATS[len(run)]:
        try:
            datetime.strptime(run, fmt)
            return True
        except ValueError:
            pass
    return False


def normalise_name(name: str | None) -> str:
    """An ad name without its date and the "Copy" Meta appends at the end, so a re-post of a video
    matches its source. A 6 or 8 digit number is dropped only when it is a real date, and Copy
    only when it is the last segment ("Hook - Copy 1 - Win" stays as it is)."""
    text = _DATE_SEP.sub(" ", str(name or "").lower())
    text = _DATE_RUN.sub(lambda m: " " if _is_date(m.group()) else m.group(), text)
    parts = [" ".join(p.split()) for p in _SEP.split(text)]
    parts = [p for p in parts if p]
    if parts:
        parts[-1] = " ".join(_COPY.sub("", parts[-1]).split())
    return " - ".join(p for p in parts if p)


def creative_key(ad: dict) -> str:
    """img:<hash> for an image ad, else name:<normalised name>. A video's own hash may be its
    thumbnail and its id differs per account, so videos match by name. With no usable name the
    ad id keeps nameless ads from blocking each other."""
    c = ad.get("creative") or {}
    if c.get("image_hash") and not c.get("video_id"):
        return f"img:{c['image_hash']}"
    n = normalise_name(ad.get("name"))
    return f"name:{n}" if n else f"id:{ad.get('ad_id')}"


def _copyable(ad: dict) -> bool:
    c = ad.get("creative") or {}
    return bool(c.get("image_hash") or c.get("video_id"))


async def _account_ads(db, gid: str) -> list[dict]:
    """The group's whole cached ad account, archived and rejected ads included."""
    return [d async for d in db["meta_ad_state"].find({"client_group_id": gid})]


def _treatments(settings: dict, gid: str) -> set[str]:
    return set(((settings.get("clients") or {}).get(gid) or {}).get("treatment_ids") or [])


async def _adds(db, agent, settings, group, window, user_id, already_run: set[str]) -> list[dict]:
    gid = group.get("id")
    mine = _treatments(settings, gid)
    if not mine:
        return []  # no treatment tag: gives nothing and gets nothing
    cfg, scoring = settings["creative"], settings["scoring"]
    start = window.end - timedelta(days=cfg["winner_days"] - 1)
    others = [g for g in await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": [k for k in (settings.get("clients") or {}) if k != gid]}}).to_list(None)]
    best: dict[str, dict] = {}
    for src in others:
        shared = mine & _treatments(settings, src["id"])
        zones = resolve_zones(settings, src["id"], src.get("ad_account_currency"))
        if not shared or zones is None or zones.green_under <= 0:
            continue
        days = scoped_days(await load_ad_days(db, user_id, src["id"], start, window.end), src)
        sums = totals(days, start, window.end)
        for ad in await _account_ads(db, src["id"]):
            s, leads = sums.get(ad["ad_id"], (0.0, 0))
            c = cpl(s, leads)
            key = creative_key(ad)
            if (c is None or leads < cfg["winner_min_leads"] or not _copyable(ad) or key in already_run
                    or str(ad.get("effective_status") or "").upper() in _NOT_COPYABLE):
                continue
            rec = await record_for(db, agent.get("_id"), key, sorted(shared))
            if rec["lost"] >= 2:
                continue
            cand = {"candidate_id": f"{src['id']}:{ad['ad_id']}", "source_client_group_id": src["id"],
                    "source_name": src.get("name") or src["id"], "ad_id": ad["ad_id"],
                    "ad_name": ad.get("name") or "", "creative_key": key, "creative": ad.get("creative") or {},
                    "treatment_ids": sorted(shared), "currency": src.get("ad_account_currency"),
                    "spend": s, "leads": leads, "cpl": c, "green_under": zones.green_under,
                    "ratio": c / zones.green_under, "record": rec}
            if key not in best or _rank(cand) < _rank(best[key]):
                best[key] = cand
    return sorted(best.values(), key=_rank)


def _rank(c: dict):
    return (-c["record"]["won"], c["ratio"])


async def _revive_and_remove(db, settings, group, window, user_id, ads: list[dict]) -> tuple[list, list]:
    gid, cfg = group.get("id"), settings["creative"]
    scoped = [a for a in ads if in_scope(a.get("campaign_id"), group)]
    live_keys = {creative_key(a) for a in scoped if str(a.get("effective_status") or "").upper() == "ACTIVE"}
    days = scoped_days(await load_ad_days(db, user_id, gid, window.end - timedelta(days=REVIVE_LOOKBACK_DAYS),
                                          window.end), group)
    recent = totals(days, window.start, window.end)
    zones = resolve_zones(settings, gid, group.get("ad_account_currency"))

    remove = []
    for a in scoped:
        if str(a.get("effective_status") or "").upper() == "ACTIVE":
            s, leads = recent.get(a["ad_id"], (0.0, 0))
            remove.append({"ad_id": a["ad_id"], "ad_name": a.get("name") or "", "adset_id": a.get("adset_id"),
                           "spend": s, "leads": leads,
                           "cpl": cpl(s, leads)})
    remove.sort(key=lambda r: -r["spend"])

    revive = []
    cutoff = window.end - timedelta(days=cfg["revive_after_days"])
    for a in scoped:
        if zones is None or str(a.get("effective_status") or "").upper() not in _REVIVABLE:
            continue
        rows = days.get(a["ad_id"]) or []
        last_spend = max((d.day for d in rows if d.spend > 0), default=None)
        off_since = _to_date(a.get("paused_at")) or (last_spend + timedelta(days=1) if last_spend else None)
        s, leads = sum(d.spend for d in rows), sum(d.leads for d in rows)
        c = cpl(s, leads)
        if (off_since is None or off_since > cutoff or c is None or leads < cfg["winner_min_leads"]
                or c >= zones.green_under or creative_key(a) in live_keys):
            continue
        revive.append({"ad_id": a["ad_id"], "ad_name": a.get("name") or "", "adset_id": a.get("adset_id"),
                       "off_since": off_since.isoformat(),
                       "spend": s, "leads": leads, "cpl": c, "ratio": c / zones.green_under})
    revive.sort(key=lambda r: r["ratio"])
    return revive, remove


async def build_candidates(db, agent: dict, group: dict, window: Window) -> dict:
    """{add, revive, remove} for one client. Add excludes every creative the destination account
    has already run in any campaign (archived and rejected copies included). Revive and remove
    only look at ads in the client's own campaigns."""
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    user_id = agent.get("user_id") or group.get("user_id")
    ads = await _account_ads(db, group.get("id"))
    already_run = {creative_key(a) for a in ads}
    revive, remove = await _revive_and_remove(db, settings, group, window, user_id, ads)
    return {"add": await _adds(db, agent, settings, group, window, user_id, already_run),
            "revive": revive, "remove": remove}
