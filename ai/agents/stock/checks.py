"""
ai/agents/stock/checks.py
-------------------------
The stock agent's per-client checks (spec section 6, Checks): CPL, zone, the action table,
follow-ups on the last card, and the facts a card needs. Fixed code, no AI. Money stays in the
client's own account currency.
"""

from datetime import date, datetime, timedelta

from ai.agents.snapshot import AdDay, AdState, load_ad_days
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings, resolve_zones
from ai.agents.stock.windows import Window

SPEND_STOPPED_DAYS = 2


def cpl(spend: float, leads: int | float) -> float | None:
    """Cost per lead, or None when there are no leads."""
    return spend / leads if leads and leads > 0 else None


def zone(spend: float, leads: int | float, zones) -> str:
    """green below the green limit, yellow up to and including the red limit, red above it.
    Spend with no leads is red. No spend has no zone."""
    if not spend or spend <= 0:
        return "no_data"
    c = cpl(spend, leads)
    if c is None:
        return "red"
    c = round(c, 6)
    if c < zones.green_under:
        return "green"
    return "yellow" if c <= zones.red_over else "red"


def decide(zone_: str, prev_cpl: float | None, cur_cpl: float | None, spend_stopped: bool,
           major_pct: float) -> str:
    """The action table. A drop of exactly major_pct counts as a major downtrend."""
    if spend_stopped:
        return "spend_stopped"
    if zone_ == "yellow":
        return "propose" if prev_cpl is not None and cur_cpl is not None and cur_cpl > prev_cpl else "none"
    if zone_ == "red":
        if prev_cpl and cur_cpl is not None and round((prev_cpl - cur_cpl) / prev_cpl * 100, 6) >= major_pct:
            return "hold_downtrend"
        return "propose"
    return "none"  # green, no_data


def in_scope(campaign_id: str | None, group: dict) -> bool:
    """`meta_campaign_ids` None means the whole account, [] means no campaigns."""
    ids = group.get("meta_campaign_ids")
    return True if ids is None else campaign_id in ids


def scoped_days(days: dict[str, list[AdDay]], group: dict) -> dict[str, list[AdDay]]:
    out = {}
    for ad_id, rows in days.items():
        keep = [d for d in rows if in_scope(d.campaign_id, group)]
        if keep:
            out[ad_id] = keep
    return out


def totals(days: dict[str, list[AdDay]], start: date, end: date) -> dict[str, tuple[float, int]]:
    """{ad_id: (spend, leads)} over [start, end]. Days with no row are zero spend."""
    out = {}
    for ad_id, rows in days.items():
        sel = [d for d in rows if start <= d.day <= end]
        if sel:
            out[ad_id] = (sum(d.spend for d in sel), sum(d.leads for d in sel))
    return out


async def client_facts(db, agent: dict, group: dict, window: Window) -> dict:
    """Everything a card needs about one client. `zones` is None when the client has no usable
    limits: zone and decision are then "no_zones" and nothing is judged, so the run can flag it."""
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    gid, currency = group.get("id"), group.get("ad_account_currency")
    user_id = agent.get("user_id") or group.get("user_id")
    zones = resolve_zones(settings, gid, currency)

    days = scoped_days(await load_ad_days(db, user_id, gid, window.prev_start, window.end), group)
    cur = totals(days, window.start, window.end)
    prev = totals(days, window.prev_start, window.prev_end)
    spend, leads = sum(s for s, _ in cur.values()), sum(l for _, l in cur.values())
    prev_spend, prev_leads = sum(s for s, _ in prev.values()), sum(l for _, l in prev.values())
    cur_cpl, prev_cpl = cpl(spend, leads), cpl(prev_spend, prev_leads)

    # A day with no row is a day with no spend.
    recent = totals(days, window.end - timedelta(days=SPEND_STOPPED_DAYS - 1), window.end)
    spend_stopped = (spend + prev_spend) > 0 and sum(s for s, _ in recent.values()) <= 0

    state = {d["ad_id"]: d async for d in db["meta_ad_state"].find({"client_group_id": gid})}
    live = {a: s for a, s in state.items()
            if str(s.get("effective_status") or "").upper() == "ACTIVE" and in_scope(s.get("campaign_id"), group)}
    ads = []
    for ad_id, (s, l) in sorted(cur.items(), key=lambda kv: -kv[1][0]):
        ads.append({"ad_id": ad_id, "name": (state.get(ad_id) or {}).get("name") or days[ad_id][-1].ad_name,
                    "status": (state.get(ad_id) or {}).get("effective_status") or "",
                    "spend": s, "leads": l, "cpl": cpl(s, l), "spend_share": s / spend if spend else 0.0})

    facts = {"client_group_id": gid, "name": group.get("name") or gid, "currency": currency,
             "window": {"start": window.start.isoformat(), "end": window.end.isoformat(),
                        "prev_start": window.prev_start.isoformat(), "prev_end": window.prev_end.isoformat()},
             "spend": spend, "leads": leads, "cpl": cur_cpl,
             "prev_spend": prev_spend, "prev_leads": prev_leads, "prev_cpl": prev_cpl,
             "ads_live": len(live), "ads": ads, "spend_stopped": spend_stopped,
             "zones": None if zones is None else {"green_under": zones.green_under, "red_over": zones.red_over}}
    if zones is None:
        return {**facts, "zone": "no_zones", "decision": "no_zones"}
    z = zone(spend, leads, zones)
    major = settings["creative"]["major_downtrend_pct"]
    return {**facts, "zone": z, "decision": decide(z, prev_cpl, cur_cpl, spend_stopped, major)}


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

def _card_day(card: dict) -> date | None:
    for v in (card.get("created_at"), (card.get("window") or {}).get("end")):
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str) and v:
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                pass
    return None


def _line_ad_ids(line: dict) -> list[str]:
    m = line.get("meta_ids")
    if isinstance(m, dict):
        m = m.get("ad_ids") or [m.get("ad_id")]
    elif isinstance(m, str):
        m = [m]
    return [str(a) for a in (m or []) if a]


def follow_ups(card: dict, ad_states: dict[str, AdState], ad_days: dict[str, list[AdDay]]) -> list[dict]:
    """Each line of the last card that was applied or flagged, checked against Meta: Add and Bring
    back are ok when the ad is live and has spent since the card's date, Remove is ok when the ad
    is off. A flagged Add has no ad id (people create the ad), so it is found by name, and if it
    isn't there yet it shows ok None, "not done yet". Additional changes can't be checked by code
    (ok None, shown as Check by hand). Lines never applied, and every line of a dismissed card,
    are skipped."""
    from ai.agents.stock.candidates import normalise_name  # candidates imports this module

    since = _card_day(card)
    out = []
    for line in [] if card.get("status") == "dismissed" else card.get("lines") or []:
        kind, state = line.get("kind"), line.get("state")
        if state not in ("applied", "flagged") or kind not in ("add", "remove", "revive"):
            continue
        name = line.get("ad_name") or "Ad"
        ids = _line_ad_ids(line)
        ads = [ad_states[i] for i in ids if i in ad_states]
        if not ids and kind == "add":
            want = normalise_name(line.get("ad_name"))
            ads = [a for a in ad_states.values() if want and normalise_name(a.name) == want]
            if not ads:
                out.append({"line_id": line.get("id"), "text": f"{name}: not done yet", "ok": None})
                continue
        if not ads:
            out.append({"line_id": line.get("id"), "text": f"{name}: not found in Meta", "ok": False})
            continue
        if kind == "remove":
            ok = all(str(a.effective_status).upper() != "ACTIVE" for a in ads)
            text = f"{name}: {'off' if ok else 'still live'}"
        else:
            live = any(str(a.effective_status).upper() == "ACTIVE" for a in ads)
            spent = any(d.spend > 0 and (since is None or d.day >= since)
                        for a in ads for d in ad_days.get(a.ad_id, []))
            ok = live and spent
            when = f" since {since.isoformat()}" if since else ""
            text = f"{name}: " + ("live and spending" + when if ok else
                                  "live but not spending" + when if live else "not live")
        out.append({"line_id": line.get("id"), "text": text, "ok": ok})
    if (card.get("additional_changes") or "").strip():
        out.append({"line_id": "additional_changes", "text": "Additional changes: check by hand", "ok": None})
    return out
