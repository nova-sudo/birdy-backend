"""
ai/agents/stock/alerts.py
-------------------------
The stock agent's daily alert check (spec section 6, Alerts). Pure detection in `health_alerts`,
reads in `fetch_account_health` and `run_alerts`, which also remembers each account's timezone on
its client group for the checks that follow. Alerts are always Flag: they never change
anything in Meta, never use AI and never go through `resolve_mode`.

Prepaid is an estimate from top-ups the agency entered. Meta's `balance` is the bill due, not
money remaining, so it is never shown as a balance.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from ai.agents.history import HISTORY, is_ready
from ai.agents.meta_state import AD_STATE, ADSET_STATE, MetaTokenError, _is_active, _parse_time
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
from ai.agents.stock.windows import _tz, last_full_day
from ai.agents.store import AD_DAYS, AGENTS

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v25.0"
_HEALTH_FIELDS = "name,account_status,disable_reason,funding_source,is_prepay_account,timezone_name,currency"
# Meta's account_status codes
_DISABLED, _UNSETTLED, _RISK_REVIEW, _GRACE, _PENDING_CLOSURE, _CLOSED = 2, 3, 7, 9, 100, 101
_DISABLE_REASONS = {1: "ads integrity policy", 2: "ad account violation", 3: "risk review",
                    4: "grace period ended", 5: "any other reason", 6: "unsettled payment",
                    7: "pending risk review", 8: "pending settlement", 9: "in grace period",
                    10: "temporarily unavailable", 11: "pending closure"}
_NO_SPEND_DAYS = 2
ACCOUNT_TZ_FIELD = "ad_account_timezone"  # on client_groups: Meta's timezone_name, set by run_alerts


async def fetch_account_health(ad_account_id: str, token: str) -> dict:
    """One Graph read of the ad account. Raises MetaTokenError, RuntimeError or httpx.HTTPError."""
    acct = ad_account_id if str(ad_account_id).startswith("act_") else f"act_{ad_account_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{_GRAPH}/{acct}", params={"fields": _HEALTH_FIELDS, "access_token": token})
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code != 200:
        err = body.get("error") or {}
        if resp.status_code == 401 or err.get("code") in (190, 102):
            raise MetaTokenError(err.get("message") or "Meta token invalid")
        raise RuntimeError(f"Meta Graph error on {acct}: {err.get('message') or resp.status_code}")
    return {"account_status": body.get("account_status"), "disable_reason": body.get("disable_reason"),
            "funding_source": body.get("funding_source"), "is_prepay_account": body.get("is_prepay_account"),
            "timezone_name": body.get("timezone_name"), "currency": body.get("currency"),
            "name": body.get("name")}


async def fetch_account_spend(ad_account_id: str, token: str, since: date, until: date) -> dict[str, float]:
    """Account-level spend per day, {iso_date: spend}, from Meta insights. Days with no spend have
    no entry. Raises MetaTokenError, RuntimeError or httpx.HTTPError."""
    acct = ad_account_id if str(ad_account_id).startswith("act_") else f"act_{ad_account_id}"
    url = f"{_GRAPH}/{acct}/insights"
    params = {"level": "account", "time_increment": "1", "fields": "spend,date_start",
              "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
              "limit": 100, "access_token": token}
    out: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        while url:
            resp = await client.get(url, params=params)
            try:
                body = resp.json()
            except Exception:
                body = {}
            if resp.status_code != 200:
                err = body.get("error") or {}
                if resp.status_code == 401 or err.get("code") in (190, 102):
                    raise MetaTokenError(err.get("message") or "Meta token invalid")
                raise RuntimeError(f"Meta Graph error on {acct}/insights: {err.get('message') or resp.status_code}")
            for row in body.get("data") or []:
                day = str(row.get("date_start") or "")[:10]
                if day:
                    out[day] = out.get(day, 0.0) + float(row.get("spend") or 0)
            url, params = (body.get("paging") or {}).get("next"), None
    return out


# ---------------------------------------------------------------------------
# Prepaid estimate
# ---------------------------------------------------------------------------

def prepaid_estimate(topups: list[dict], spend_since: float, last3_days: list[float],
                     daily_budgets: list[float]) -> dict | None:
    """left = latest top-up minus account spend since its date. per_day is the average of the last
    3 full days, or the active daily budgets when there are fewer than 3 days. days_left is None
    when there is no spend rate to divide by. None when there is no top-up to count from."""
    latest = max((t for t in topups or [] if t.get("date")), key=lambda t: t["date"], default=None)
    if latest is None:
        return None
    left = max(0.0, float(latest.get("amount") or 0) - float(spend_since or 0))
    per_day = sum(last3_days) / 3 if len(last3_days) >= 3 else sum(daily_budgets or [])
    return {"left": _tidy(left), "per_day": _tidy(per_day),
            "days_left": _tidy(left / per_day) if per_day > 0 else None}


def _tidy(x: float):
    x = round(x, 2)
    return int(x) if x == int(x) else x


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _alert(gid: str, code: str, severity: str, title: str, detail: str, since: str) -> dict:
    return {"client_group_id": gid, "code": code, "severity": severity, "title": title,
            "detail": detail, "since": since}


def _texts(v) -> list[str]:
    """Every string inside Meta's nested feedback / issue payloads."""
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, dict):
        return [t for x in v.values() for t in _texts(x)]
    if isinstance(v, list):
        return [t for x in v for t in _texts(x)]
    return []


def _issue_text(ad: dict, *keys) -> str:
    issues = ad.get("issues") or []
    msgs = [i.get(k) for i in issues if isinstance(i, dict) for k in keys if i.get(k)]
    return "; ".join(dict.fromkeys(msgs)) or "; ".join(dict.fromkeys(_texts(issues)))


def _feedback_text(ad: dict) -> str:
    return "; ".join(dict.fromkeys(_texts(ad.get("review_feedback")))) or _issue_text(ad, "error_message", "error_summary")


def _ads_detail(ads: list[dict], text_of) -> str:
    lines = []
    for a in ads:
        msg = text_of(a)
        lines.append(f"{a.get('name') or a.get('ad_id')}: {msg}" if msg else f"{a.get('name') or a.get('ad_id')}")
    return "\n".join(lines)


def usable_topups(topups) -> list[dict]:
    """Top-ups with a real ISO date and a numeric amount. Anything else is ignored."""
    out = []
    for t in topups or []:
        if not isinstance(t, dict):
            continue
        try:
            date.fromisoformat(str(t.get("date"))[:10])
        except ValueError:
            continue
        if isinstance(t.get("amount"), (int, float)) and not isinstance(t.get("amount"), bool):
            out.append(t)
    return out


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _day(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    parsed = _parse_time(v)
    return parsed.date() if parsed else None


def health_alerts(health: dict, ads: list[dict], daily_spend: dict, settings: dict, gid: str,
                  now_utc: datetime, daily_budgets: list[float] | None = None,
                  account_spend: dict | None = None, campaign_ids: list | None = None,
                  history_state: str = "ready") -> list[dict]:
    """Alerts for one client. `ads` are its meta_ad_state rows, narrowed to `campaign_ids` when
    that list is set. `daily_spend` is {ad_id: {iso_date: spend}} from its daily ad rows.
    `history_state` is "ready", "late" (synced, but not through the last full day) or "unready":
    spend alerts run only on "ready", and "late" gives one low-severity spend_data_late.
    `account_spend` is {iso_date: spend} for the whole ad account, from Meta, and feeds the
    prepaid estimate. `daily_budgets` are the active daily ad set budgets, used by that estimate
    when fewer than 3 days of spend exist."""
    if campaign_ids is not None:
        ads = [a for a in ads if a.get("campaign_id") in set(campaign_ids)]
    cfg = merge_settings(DEFAULT_SETTINGS, settings or {})
    thresholds = cfg["alerts"]
    out: list[dict] = []
    today = now_utc.date().isoformat()
    status = health.get("account_status")
    reason = _DISABLE_REASONS.get(health.get("disable_reason"))

    # -- account state, spend-stoppers first
    if status in (_DISABLED, _PENDING_CLOSURE, _CLOSED):
        out.append(_alert(gid, "account_disabled", "critical", "Ad account is disabled",
                          f"Meta reports the account as disabled{f' ({reason})' if reason else ''}. "
                          "Nothing is delivering.", today))
    elif status == _RISK_REVIEW:
        out.append(_alert(gid, "risk_review", "critical", "Ad account is in risk review",
                          "Meta is reviewing the account and delivery may be stopped until it finishes.", today))

    pay: list[str] = []
    if status == _UNSETTLED:
        pay.append("The account has an unsettled payment.")
    elif status == _GRACE:
        pay.append("The account is in its payment grace period.")
    billing = [a for a in ads if str(a.get("effective_status") or "").upper() == "PENDING_BILLING_INFO"]
    if billing:
        pay.append(f"{_plural(len(billing), 'ad is', 'ads are')} waiting on billing info.")
    if not health.get("funding_source") and status not in (_DISABLED, _PENDING_CLOSURE, _CLOSED):
        pay.append("The account has no payment method on file.")
    if pay:
        out.append(_alert(gid, "payment_issue", "critical", "Payment problem on the ad account",
                          " ".join(pay), today))

    # -- ad review state
    def by_status(s):
        return [a for a in ads if str(a.get("effective_status") or "").upper() == s]

    for code, s, label, text_of in (
            ("ad_rejected", "DISAPPROVED", "rejected", _feedback_text),
            ("ad_with_issues", "WITH_ISSUES", "with issues", lambda a: _issue_text(a, "error_message", "error_summary")),
            ("ad_in_review", "PENDING_REVIEW", "in review", lambda a: "")):
        hit = by_status(s)
        if hit:
            title = f"{_plural(len(hit), 'ad', 'ads')} {label}"
            out.append(_alert(gid, code, "warning", title, _ads_detail(hit, text_of), today))

    # -- spend, judged on full days in the account's own timezone
    tz = health.get("timezone_name")
    y = last_full_day(now_utc, tz)
    prev = y - timedelta(days=1)
    per_day: dict[str, float] = {}
    for days in (daily_spend or {}).values():
        for d, v in (days or {}).items():
            per_day[d] = per_day.get(d, 0.0) + float(v or 0)
    y_spend, prev_spend = per_day.get(y.isoformat(), 0.0), per_day.get(prev.isoformat(), 0.0)
    ratio = thresholds["spend_drop_ratio"]
    spend_ok = history_state == "ready"
    if history_state == "late":
        out.append(_alert(gid, "spend_data_late", "info", "Spend data is behind",
                          f"Ad data hasn't synced through {y.isoformat()} yet, so spend alerts were skipped.",
                          today))
    if not spend_ok:
        pass
    elif y_spend <= 0 and prev_spend > 0:
        out.append(_alert(gid, "spend_stopped", "critical", "No spend yesterday",
                          f"Spend was 0 on {y.isoformat()} after {_tidy(prev_spend)} the day before.", y.isoformat()))
    elif 0 < y_spend < prev_spend * ratio:
        out.append(_alert(gid, "spend_drop", "warning", "Spend dropped sharply",
                          f"Spend was {_tidy(y_spend)} on {y.isoformat()}, under {ratio:g} of the "
                          f"{_tidy(prev_spend)} the day before.", y.isoformat()))

    quiet_from = prev
    quiet = []
    for a in (ads if spend_ok else []):
        if not _is_active(a.get("effective_status")):
            continue
        created = _day(a.get("created_time"))
        if created and created > quiet_from:
            continue  # too new to have had two full days
        days = (daily_spend or {}).get(a.get("ad_id")) or {}
        if not any(float(days.get(d.isoformat()) or 0) > 0 for d in (y, prev)):
            quiet.append(a)
    if quiet:
        out.append(_alert(gid, "ad_no_spend", "warning",
                          f"{_plural(len(quiet), 'active ad has', 'active ads have')} not spent for "
                          f"{_NO_SPEND_DAYS}+ days", _ads_detail(quiet, lambda a: ""), prev.isoformat()))

    # -- prepaid, an estimate from the agency's top-ups
    prepaid = (((cfg.get("clients") or {}).get(gid)) or {}).get("prepaid") or {}
    entered = prepaid.get("topups") or []
    topups = usable_topups(entered)
    if entered and not topups:
        out.append(_alert(gid, "check_failed", "warning", "Prepaid top-ups can't be used",
                          "None of the top-ups entered has a valid date and amount, so no prepaid "
                          "estimate was made. Check the top-ups in setup.", today))
    elif not entered and prepaid.get("is_prepay_account") is True:
        out.append(_alert(gid, "prepaid_no_topups", "info", "Prepaid account has no top-ups entered",
                          "Meta says this is a prepaid account. The balance can't be estimated until "
                          "top-ups are entered in setup.", today))
    elif topups and prepaid.get("is_prepay_account") is not False and account_spend is not None:
        latest = max(t["date"] for t in topups)[:10]
        spend_since = sum(v for d, v in account_spend.items() if d >= latest)
        days_in_range = [y - timedelta(days=i) for i in range(3)]
        last3 = [float(account_spend.get(d.isoformat(), 0.0)) for d in days_in_range] \
            if all(d.isoformat() >= latest for d in days_in_range) else []
        est = prepaid_estimate(topups, spend_since, last3, daily_budgets or [])
        if est and est["days_left"] is not None and est["days_left"] <= thresholds["prepaid_days"]:
            n = est["days_left"]
            out.append(_alert(
                gid, "prepaid_low", "critical" if est["left"] <= 0 else "warning",
                f"Prepaid estimate: about {_tidy(n)} {'day' if n == 1 else 'days'} left",
                f"This is an estimate, not a Meta balance. Latest top-up on {latest} minus account spend "
                f"since leaves about {_tidy(est['left'])}, at about {_tidy(est['per_day'])} a day.", today))
    return out


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

async def _group_inputs(db, user_id: str, gid: str, since: str) -> tuple[list[dict], dict, list[float]]:
    ads = [a async for a in db[AD_STATE].find({"client_group_id": gid})]
    spend: dict[str, dict[str, float]] = {}
    async for row in db[AD_DAYS].find({"user_id": user_id, "client_group_id": gid}):
        day = str(row.get("date_start") or "")[:10]
        if not day or day < since:
            continue
        try:
            v = float((row.get("insight_data") or {}).get("spend") or 0)
        except (TypeError, ValueError):
            continue
        spend.setdefault(row["ad_id"], {})
        spend[row["ad_id"]][day] = spend[row["ad_id"]].get(day, 0.0) + v
    budgets = [s.get("daily_budget") or 0 async for s in db[ADSET_STATE].find(
        {"client_group_id": gid, "effective_status": "ACTIVE", "campaign_status": "ACTIVE"})]
    return ads, spend, [b for b in budgets if b]


async def _prefill_prepaid(db, agent: dict, gid: str, detected) -> None:
    """Remember Meta's is_prepay_account on the client, but only while the agency hasn't set
    prepaid. A choice already made is never overwritten. Readers must use the stored
    `is_prepay_account` flag, not the presence of `prepaid`."""
    if detected is None:
        return
    value = {"is_prepay_account": bool(detected), "topups": []}
    try:
        res = await db[AGENTS].update_one(
            {"_id": agent["_id"], f"settings.clients.{gid}.prepaid": None},
            {"$set": {f"settings.clients.{gid}.prepaid": value}})
    except Exception as e:  # noqa: BLE001 - a prefill is never worth failing the alerts for
        logger.warning("prepaid prefill failed for %s: %s", gid, e)
        return
    if res.modified_count:
        clients = agent.setdefault("settings", {}).get("clients")
        if not isinstance(clients, dict):
            clients = agent["settings"]["clients"] = {}
        clients.setdefault(gid, {})["prepaid"] = value


def _day_of(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


async def _history_state(db, gid: str, last_full: date, tz_name: str | None) -> str:
    """"ready" when the client's ad history is usable and was last synced after the end of the
    account's last full day, in the account's timezone. The sync time is the history doc's
    `last_topup_at` (naive UTC, set when a first backfill finishes and on every top-up). Not
    `newest_done`, which is the cron's UTC date, and not the newest ad row: Meta returns no row
    for a day with no spend. A missing sync time reads as "late"."""
    doc = await db[HISTORY].find_one({"_id": gid})
    if not is_ready(doc):
        return "unready"
    synced = doc.get("last_topup_at")
    if not isinstance(synced, datetime):
        return "late"
    local = synced.replace(tzinfo=timezone.utc).astimezone(_tz(tz_name)).date()
    return "ready" if local > last_full else "late"


def _topups_of(settings: dict, gid: str) -> list[dict]:
    prepaid = ((((settings.get("clients") or {}).get(gid)) or {}).get("prepaid")) or {}
    return usable_topups(prepaid.get("topups"))


async def run_alerts(db, agent: dict, token_for, now_utc: datetime, client_ids: list[str]) -> list[dict]:
    """Alerts for the given clients. `token_for(user_id)` is an async callable returning the Meta
    token. Each ad account's health, and its spend when a client on it has top-ups, is read once
    per call however many clients share it. A failed read becomes a `check_failed` alert for that
    client, never an exception."""
    user_id = agent["user_id"]
    out: list[dict] = []
    today = now_utc.date().isoformat()
    groups: dict[str, dict] = {}
    for gid in client_ids:
        groups[gid] = await db["client_groups"].find_one({"id": gid, "user_id": user_id}) or {}

    token, token_error = None, None
    try:
        token = await token_for(user_id)
    except Exception as e:  # noqa: BLE001 - reported as an alert
        token_error = e

    health_cache: dict[str, dict | Exception] = {}
    spend_cache: dict[str, dict | Exception] = {}

    # The earliest top-up per ad account, so its spend is read once for every client sharing it.
    settings0 = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    since_by_account: dict[str, date] = {}
    for gid, g in groups.items():
        acct = g.get("meta_ad_account_id")
        days = [d for d in (_day_of(t["date"]) for t in _topups_of(settings0, gid)) if d]
        if acct and days:
            since_by_account[acct] = min(days + [since_by_account.get(acct, min(days))])

    for gid in client_ids:
        group = groups[gid]
        name = group.get("name") or gid
        account = group.get("meta_ad_account_id")
        if not account:
            out.append(_alert(gid, "check_failed", "warning", f"Couldn't check {name}",
                              "This client has no Meta ad account connected.", today))
            continue
        if account not in health_cache:
            try:
                if token_error is not None or not token:
                    raise token_error or MetaTokenError("No valid Facebook token found")
                health_cache[account] = await fetch_account_health(account, token)
            except Exception as e:  # noqa: BLE001 - reported as an alert
                logger.warning("alert check failed for %s: %s", account, e)
                health_cache[account] = e
        health = health_cache[account]
        if isinstance(health, Exception):
            out.append(_alert(gid, "check_failed", "warning", f"Couldn't check {name}",
                              f"Meta could not be read: {health}", today))
            continue

        await _prefill_prepaid(db, agent, gid, health.get("is_prepay_account"))
        if health.get("timezone_name") and group.get(ACCOUNT_TZ_FIELD) != health["timezone_name"]:
            await db["client_groups"].update_one({"id": gid, "user_id": user_id},
                                                 {"$set": {ACCOUNT_TZ_FIELD: health["timezone_name"]}})
        cfg = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
        window_days = cfg["creative"]["data_window_days"]
        end = last_full_day(now_utc, health.get("timezone_name"))

        account_spend = None
        if account in since_by_account and ((cfg["clients"].get(gid) or {}).get("prepaid") or {}).get("is_prepay_account") is not False:
            if account not in spend_cache:
                try:
                    spend_cache[account] = await fetch_account_spend(account, token, since_by_account[account], end)
                except Exception as e:  # noqa: BLE001 - reported as an alert
                    logger.warning("account spend read failed for %s: %s", account, e)
                    spend_cache[account] = e
            got = spend_cache[account]
            if isinstance(got, Exception):
                out.append(_alert(gid, "check_failed", "warning", f"Couldn't check prepaid for {name}",
                                  f"Account spend could not be read from Meta: {got}", today))
            else:
                account_spend = got

        since = (end - timedelta(days=max(window_days, 3) + 1)).isoformat()
        ads, spend, budgets = await _group_inputs(db, user_id, gid, since)
        out.extend(health_alerts(
            health, ads, spend, cfg, gid, now_utc, daily_budgets=budgets, account_spend=account_spend,
            campaign_ids=group.get("meta_campaign_ids"), history_state=await _history_state(db, gid, end, health.get("timezone_name"))))
    return out
