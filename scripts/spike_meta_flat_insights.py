"""
Spike: does a "flat daily insights" fetch match — and cost less than — the
current 13x nested per-preset fetch for the Meta refresh?

Read-only diff tool. Does NOT modify any production data or state. Writes
one JSON report per run and prints a summary to stdout.

Background
----------
Today, services/meta_service.py::_fetch_meta_campaigns_for_preset is called
13 times per client group per refresh cycle — once per date preset — with
each call re-fetching the entire campaign→adset→ad object graph (names,
statuses, hierarchy, creative) alongside a preset-scoped insights block.
Only the insights numbers actually differ between presets; the graph is
identical.

Proposed alternative (from the ClickUp ticket):
  1. Fetch the object graph once — call the max preset via the existing
     nested-fields endpoint. Zero behavior change.
  2. Fetch daily-granularity insights across the 12 non-max presets via
     three level-scoped calls (level=campaign / adset / ad) with
     time_increment=1 & date_preset=maximum.
  3. Bucket daily rows into each preset's window in Python — same
     pattern already used for GHL/HP in this repo.

Two unknowns block the implementation (per the ticket):
  1. Does the flat-insights endpoint's lead-count field shape match what
     our parser expects? (`results` / `actions` shape.)
  2. Does pagination get *worse* for large accounts? Many ads × many days
     could produce more pages than the current 13-per-preset shape.

What this script does
---------------------
For a chosen (user_id, ad_account_id) pair:

  A. Fetches level={campaign,adset,ad} insights at daily granularity,
     `date_preset=maximum`. Counts requests, pages, bytes.

  B. Bucketises the daily rows into each preset window using
     core.constants.ghl_date_bounds — same helper the rest of the codebase
     uses so we're not inventing new date math.

  C. Compares the bucketised totals to whatever's currently stored under
     client_groups.facebook_cache.<preset>.{campaigns,adsets,ads,metrics}
     (populated by the current per-preset fetcher on the last cron cycle).
     This is the "shadow-mode diff against production facebook_cache" the
     ticket asks for.

  D. Emits a JSON report with:
        - api_cost:   requests/pages/bytes broken down by mode
        - lead_field: what the flat endpoint returns for lead counts
                      (empty? actions? results? per-action-type?) — this
                      is unknown #1
        - pagination: pages per level for this account — this is unknown #2
        - diffs:      per-preset, per-entity mismatch between bucketised
                      candidate and Mongo baseline

Usage
-----
    # List groups this user has with Meta connected
    python -m scripts.spike_meta_flat_insights --user-id abdelrahman@… --list-groups

    # Run against one group
    python -m scripts.spike_meta_flat_insights --group-id <group_id>

    # Restrict to specific presets (default: all 13)
    python -m scripts.spike_meta_flat_insights --group-id <id> --presets today,last_7d,last_month

    # Write full report to file
    python -m scripts.spike_meta_flat_insights --group-id <id> --output /tmp/spike.json

Cost note
---------
Every run makes 3 real Graph API calls (one per level), potentially paginated.
Empirically the ad-level call is the biggest — proportional to (num_ads ×
account_age_days). Estimate before running: an ad account with 100 ads and
2 years of history returns ~73k ad-daily rows. Meta paginates at ~1000
rows/page so that's ~73 pages. Adjust `--only-levels` if that's too much
for a first look (e.g. `--only-levels campaign` for a cheap smoke test).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

# So `python -m scripts.spike_meta_flat_insights` works from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME
from core.constants import META_CACHE_PRESETS, ghl_date_bounds
from core.utils import get_result_value
from integrations.facebook_utils.facebook import get_facebook_token


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("spike")


MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
GRAPH_BASE = "https://graph.facebook.com/v25.0"


# ─── Counted HTTP client ────────────────────────────────────────────

class CountedClient:
    """
    httpx wrapper that records requests, pages, and response bytes so the
    spike's API-cost report is honest. Every real Meta call goes through
    exactly one instance of this class.
    """

    def __init__(self, timeout: float = 120.0):
        self._client = httpx.AsyncClient(timeout=timeout)
        self.requests = 0
        self.total_bytes = 0

    async def get(self, url: str, params: dict | None = None) -> httpx.Response:
        self.requests += 1
        r = await self._client.get(url, params=params)
        if r.content:
            self.total_bytes += len(r.content)
        return r

    async def close(self) -> None:
        await self._client.aclose()


# ─── Flat insights fetcher (the proposed approach) ──────────────────

INSIGHT_FIELDS = (
    "spend,impressions,clicks,reach,cpm,cpc,ctr,actions,results"
)


async def fetch_flat_insights(
    client: CountedClient,
    ad_account_id: str,
    access_token: str,
    level: str,
    since: str | None = None,
) -> tuple[list[dict], int]:
    """
    Fetch daily-granularity insights for one level (campaign|adset|ad) via
    /{ad_account}/insights?level=X&time_increment=1. Returns (rows, pages).

    `since` optional — pass yyyy-mm-dd to cap history. With no since, uses
    date_preset=maximum (all-time). The bucketiser only needs enough
    history to cover the longest bounded preset (~1 year), so callers can
    save API budget by passing since=(today - 400 days). Not enforced
    here — the spike deliberately runs against `maximum` first to gauge
    real pagination cost.
    """
    if level not in {"campaign", "adset", "ad"}:
        raise ValueError(f"bad level {level!r}")

    url = f"{GRAPH_BASE}/{ad_account_id}/insights"
    params: dict[str, Any] = {
        "access_token": access_token,
        "level": level,
        "time_increment": 1,
        "fields": _fields_for_level(level),
        "limit": 500,
    }
    if since:
        params["time_range"] = json.dumps({"since": since, "until": date.today().isoformat()})
    else:
        params["date_preset"] = "maximum"

    rows: list[dict] = []
    pages = 0
    next_url: str | None = None
    next_params: dict | None = params

    while True:
        pages += 1
        resp = await client.get(next_url or url, params=next_params)
        if resp.status_code != 200:
            body = resp.text[:400]
            logger.error(
                "Meta insights %s HTTP %s on page %d: %s",
                level, resp.status_code, pages, body,
            )
            break

        data = resp.json()
        rows.extend(data.get("data", []) or [])
        paging = data.get("paging", {}) or {}
        next_url = paging.get("next")
        next_params = None  # `next` is a fully-qualified URL
        if not next_url:
            break

        # Cheap safety cap — spike shouldn't ever produce >200 pages per
        # level on any account we have. If we hit this, the finding is
        # itself the answer to unknown #2 (pagination catastrophic).
        if pages >= 200:
            logger.warning(
                "Hit 200-page cap on %s insights — collected %d rows; "
                "further pages exist. Flagging pagination as unbounded.",
                level, len(rows),
            )
            break

    return rows, pages


def _fields_for_level(level: str) -> str:
    """
    ID + name fields differ per level so the row can be joined back to
    the object graph. Insight numeric fields are identical across levels.
    """
    if level == "campaign":
        return f"campaign_id,campaign_name,{INSIGHT_FIELDS}"
    if level == "adset":
        return f"adset_id,adset_name,campaign_id,{INSIGHT_FIELDS}"
    if level == "ad":
        return f"ad_id,ad_name,adset_id,campaign_id,{INSIGHT_FIELDS}"
    raise ValueError(level)


# ─── Bucketiser ─────────────────────────────────────────────────────

def _preset_bounds(preset: str) -> tuple[str | None, str | None]:
    """(start_iso, end_iso) inclusive, or (None, None) for 'maximum'."""
    return ghl_date_bounds(preset)


def _in_window(date_str: str, start: str | None, end: str | None) -> bool:
    if start and date_str < start:
        return False
    if end and date_str > end:
        return False
    return True


def _sum_actions_lead(actions: list | None) -> int:
    """
    Same shape the current code expects: sum values across common lead-like
    action_types. Returns 0 when nothing matches. Kept close to the
    existing get_result_value helper so results are comparable.
    """
    if not actions:
        return 0
    total = 0
    lead_like = {
        "lead",
        "leadgen_grouped",
        "leadgen_other",
        "onsite_conversion.lead_grouped",
        "offsite_conversion.fb_pixel_lead",
        "submit_application_total",
        "onsite_web_lead",
        "complete_registration",
    }
    for a in actions:
        if a.get("action_type") in lead_like:
            try:
                total += int(float(a.get("value") or 0))
            except (ValueError, TypeError):
                pass
    return total


def _extract_leads_from_row(row: dict) -> int:
    """
    Lead-count field shape check (spike unknown #1).

    The nested-per-preset endpoint returns lead counts inside `insights[0].results`
    or `insights[0].actions` (depending on campaign objective). The flat
    endpoint at `time_increment=1` returns them at the row level, either as
    `row.results[*].value` for that day, or as `row.actions[*].value` with
    an action_type in the lead-like set.

    Extract both spellings so the report can show what's actually populated.
    """
    # Meta docs claim `results` mirrors the Ads Manager "Results" column;
    # empirically it's only populated on campaigns with a lead objective.
    results = row.get("results") or []
    if results:
        for r in results:
            try:
                return int(float(r.get("value") or 0))
            except (ValueError, TypeError):
                pass

    # Fall back to summing lead-like action_types.
    return _sum_actions_lead(row.get("actions"))


def bucketise_daily_to_presets(
    rows: list[dict],
    level: str,
    presets: list[str],
) -> dict[str, dict[str, dict]]:
    """
    rows: daily insights rows for one level.
    Returns { preset: { entity_id: aggregated_row } } for each requested preset.

    Non-additive fields (cpm, cpc, ctr) are recomputed after summation to
    match what the current per-preset fetch does (`spend/impressions * 1000`
    for cpm, etc.).
    """
    id_field = {"campaign": "campaign_id", "adset": "adset_id", "ad": "ad_id"}[level]
    name_field = {"campaign": "campaign_name", "adset": "adset_name", "ad": "ad_name"}[level]

    # Preset bounds cached for the row loop.
    bounds = {p: _preset_bounds(p) for p in presets}

    out: dict[str, dict[str, dict]] = {p: {} for p in presets}

    for row in rows:
        date_start = row.get("date_start") or ""  # 'yyyy-mm-dd'
        if not date_start:
            continue

        entity_id = row.get(id_field)
        if not entity_id:
            continue

        # Numeric parsing once per row.
        try:
            spend = float(row.get("spend") or 0)
        except (ValueError, TypeError):
            spend = 0.0
        try:
            impressions = int(row.get("impressions") or 0)
        except (ValueError, TypeError):
            impressions = 0
        try:
            clicks = int(row.get("clicks") or 0)
        except (ValueError, TypeError):
            clicks = 0
        try:
            reach = int(row.get("reach") or 0)
        except (ValueError, TypeError):
            reach = 0
        leads = _extract_leads_from_row(row)

        for preset, (start, end) in bounds.items():
            if not _in_window(date_start, start, end):
                continue
            bucket = out[preset].setdefault(entity_id, {
                "id": entity_id,
                "name": row.get(name_field) or "",
                "spend": 0.0,
                "impressions": 0,
                "clicks": 0,
                "reach": 0,
                "results": 0,
            })
            bucket["spend"] += spend
            bucket["impressions"] += impressions
            bucket["clicks"] += clicks
            # NOTE: reach dedup'd per day, but summing across days
            # overcounts unique-people at the preset window. The
            # current nested endpoint does the "right" reach for a
            # preset in one call — we can't recover exact preset-window
            # reach from daily rows without a separate reach call.
            # Reported in the diff so we know the ceiling.
            bucket["reach"] += reach
            bucket["results"] += leads

    # Recompute derived fields once at the end.
    for preset, per_entity in out.items():
        for e in per_entity.values():
            imp = e["impressions"] or 0
            clk = e["clicks"] or 0
            e["spend"] = round(e["spend"], 2)
            e["cpm"] = round(e["spend"] / imp * 1000, 2) if imp else 0
            e["cpc"] = round(e["spend"] / clk, 2) if clk else 0
            e["ctr"] = round(clk / imp * 100, 2) if imp else 0

    return out


# ─── Baseline: read the current path's OUTPUT from Mongo ─────────────

async def load_baseline(
    db, group_id: str, presets: list[str],
) -> dict[str, dict[str, dict[str, dict]]]:
    """
    Returns { preset: { level: { entity_id: baseline_row } } }
    from client_groups.facebook_cache.<preset>. Missing presets show as {}.
    """
    group = await db["client_groups"].find_one(
        {"id": group_id},
        {"facebook_cache": 1},
    )
    if not group:
        raise SystemExit(f"No client group with id={group_id}")

    fb_cache = (group or {}).get("facebook_cache") or {}
    out: dict[str, dict[str, dict[str, dict]]] = {}
    for preset in presets:
        preset_data = fb_cache.get(preset) or {}
        out[preset] = {
            "campaign": {c.get("id"): _normalize_baseline(c) for c in (preset_data.get("campaigns") or []) if c.get("id")},
            "adset":    {a.get("id"): _normalize_baseline(a) for a in (preset_data.get("adsets")    or []) if a.get("id")},
            "ad":       {a.get("id"): _normalize_baseline(a) for a in (preset_data.get("ads")       or []) if a.get("id")},
        }
    return out


def _normalize_baseline(row: dict) -> dict:
    """
    Baseline rows (from _fetch_meta_campaigns_for_preset) already have the
    fields we compare on; this just makes them shape-parallel with the
    candidate rows.
    """
    return {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "spend": round(float(row.get("spend") or 0), 2),
        "impressions": int(row.get("impressions") or 0),
        "clicks": int(row.get("clicks") or 0),
        "reach": int(row.get("reach") or 0),
        "results": int(row.get("results") or 0),
        "cpm": round(float(row.get("cpm") or 0), 2),
        "cpc": round(float(row.get("cpc") or 0), 2),
        "ctr": round(float(row.get("ctr") or 0), 2),
    }


# ─── Diff ────────────────────────────────────────────────────────────

_COMPARE_FIELDS_INT = ("impressions", "clicks", "reach", "results")
_COMPARE_FIELDS_FLOAT = ("spend", "cpm", "cpc", "ctr")


def diff_entity(baseline: dict, candidate: dict, *, float_tol: float = 0.05) -> dict:
    """
    Field-level diff between one baseline entity and its candidate. Returns
    a dict of {field: (baseline_val, candidate_val, delta)} for fields that
    differ beyond `float_tol` (absolute) for floats or by more than 0 for ints.
    Empty dict when identical.
    """
    d = {}
    for f in _COMPARE_FIELDS_INT:
        b = int(baseline.get(f) or 0)
        c = int(candidate.get(f) or 0)
        if b != c:
            d[f] = {"baseline": b, "candidate": c, "delta": c - b}
    for f in _COMPARE_FIELDS_FLOAT:
        b = float(baseline.get(f) or 0)
        c = float(candidate.get(f) or 0)
        if abs(c - b) > float_tol:
            d[f] = {"baseline": round(b, 4), "candidate": round(c, 4), "delta": round(c - b, 4)}
    return d


def build_preset_diff(
    baseline_by_level: dict[str, dict[str, dict]],
    candidate_by_level: dict[str, dict[str, dict]],
) -> dict:
    """
    Compare baseline vs candidate for one preset, returning a summary
    dict with:
      - matched_entities per level
      - baseline_only / candidate_only entity ids
      - per-level distributions of diff magnitudes
    """
    out: dict[str, Any] = {}
    for level in ("campaign", "adset", "ad"):
        bl = baseline_by_level.get(level, {}) or {}
        cd = candidate_by_level.get(level, {}) or {}
        bl_ids = set(bl.keys())
        cd_ids = set(cd.keys())
        common = bl_ids & cd_ids

        entity_diffs: list[dict] = []
        for eid in common:
            d = diff_entity(bl[eid], cd[eid])
            if d:
                entity_diffs.append({
                    "id": eid,
                    "name": bl[eid].get("name") or cd[eid].get("name"),
                    "diffs": d,
                })

        # Annotate baseline_only with baseline spend/impressions to distinguish
        # zero-activity ghosts (harmless) from real coverage gaps in the flat approach.
        baseline_only_ids = sorted(bl_ids - cd_ids)
        baseline_only_annotated = []
        baseline_only_zero_activity = 0
        for bid in baseline_only_ids[:50]:
            b = bl[bid]
            has_activity = (b.get("spend", 0) > 0 or b.get("impressions", 0) > 0 or b.get("clicks", 0) > 0 or b.get("results", 0) > 0)
            if not has_activity:
                baseline_only_zero_activity += 1
            baseline_only_annotated.append({
                "id": bid,
                "name": b.get("name"),
                "spend": b.get("spend"),
                "impressions": b.get("impressions"),
                "results": b.get("results"),
                "zero_activity": not has_activity,
            })

        out[level] = {
            "matched_entities": len(common),
            "baseline_only_total": len(baseline_only_ids),
            "baseline_only_zero_activity": baseline_only_zero_activity,
            "baseline_only": baseline_only_annotated[:20],  # cap for report readability
            "candidate_only": sorted(cd_ids - bl_ids)[:20],
            "diffs": entity_diffs[:30],  # cap; the point is a signal, not an exhaustive list
            "diffs_total": len(entity_diffs),
        }
    return out


# ─── Reporting ───────────────────────────────────────────────────────

def summarise(report: dict) -> str:
    """Human-readable summary printed to stdout after the JSON is written."""
    lines = []
    lines.append("=" * 72)
    lines.append("Meta flat-insights spike — summary")
    lines.append("=" * 72)
    lines.append(f"Group:      {report['group_id']} ({report['group_name']})")
    lines.append(f"Ad account: {report['ad_account_id']}")
    lines.append("")
    lines.append("API cost — proposed (flat) approach:")
    for level, s in report["api_cost"]["candidate"].items():
        lines.append(
            f"  {level:8s}: {s['requests']:4d} request(s), "
            f"{s['pages']:4d} page(s), "
            f"{s['bytes']/1024:7.1f} KiB, "
            f"{s['rows']:6d} rows"
        )
    lines.append(
        f"  total   : {report['api_cost']['candidate_total']['requests']} requests, "
        f"{report['api_cost']['candidate_total']['pages']} pages, "
        f"{report['api_cost']['candidate_total']['bytes']/1024:.1f} KiB"
    )
    lines.append("")
    lines.append(
        "API cost — current (per-preset nested): NOT called this run "
        "(baseline read from Mongo). Current approach is fixed at 13 "
        "requests per group, one per preset, page count varies by campaign count."
    )
    lines.append("")
    lines.append("Unknown #1 — lead-count field shape at flat endpoint:")
    lf = report["lead_field_shape"]
    lines.append(f"  rows with `results`: {lf['rows_with_results']}")
    lines.append(f"  rows with `actions`: {lf['rows_with_actions']}")
    lines.append(f"  rows with a lead-like action_type: {lf['rows_with_lead_action']}")
    lines.append(f"  sample action_types seen: {lf['sample_action_types'][:8]}")
    lines.append("")
    lines.append("Per-preset diff summary (baseline = client_groups.facebook_cache.<preset>):")
    for preset in report["presets"]:
        p = report["diffs_by_preset"].get(preset, {})
        if not p:
            lines.append(f"  {preset:24s}: (no data)")
            continue
        camp = p.get("campaign", {})
        ad = p.get("ad", {})
        c_bo_tot = camp.get("baseline_only_total", 0)
        c_bo_zero = camp.get("baseline_only_zero_activity", 0)
        c_co = len(camp.get("candidate_only", []))
        a_bo_tot = ad.get("baseline_only_total", 0)
        a_bo_zero = ad.get("baseline_only_zero_activity", 0)
        a_co = len(ad.get("candidate_only", []))
        lines.append(
            f"  {preset:24s}: "
            f"camps match={camp.get('matched_entities', 0):3d} diffs={camp.get('diffs_total', 0):3d} "
            f"bl_only={c_bo_tot:3d}(zero={c_bo_zero:3d}) cd_only={c_co:3d} | "
            f"ads match={ad.get('matched_entities', 0):4d} diffs={ad.get('diffs_total', 0):4d} "
            f"bl_only={a_bo_tot:4d}(zero={a_bo_zero:4d}) cd_only={a_co:4d}"
        )
    lines.append("")
    lines.append("Full JSON report written to: " + report.get("output_path", "(not written)"))
    lines.append("=" * 72)
    return "\n".join(lines)


# ─── CLI / main ──────────────────────────────────────────────────────

async def list_groups(db, user_id: str) -> None:
    """Print all client groups this user has with a Meta ad account attached."""
    async for g in db["client_groups"].find(
        {"user_id": user_id, "meta_ad_account_id": {"$exists": True, "$ne": None}},
        {"id": 1, "name": 1, "meta_ad_account_id": 1, "last_meta_refresh": 1, "_id": 0},
    ):
        print(
            f"  {g['id']:32s}  "
            f"{g.get('name', ''):40s}  "
            f"{g.get('meta_ad_account_id', ''):20s}  "
            f"last_refresh={g.get('last_meta_refresh')}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", help="User email — required with --list-groups.")
    parser.add_argument("--group-id", help="Client group id to spike against.")
    parser.add_argument("--list-groups", action="store_true",
                        help="List groups this user has (needs --user-id).")
    parser.add_argument("--presets", default=",".join(META_CACHE_PRESETS),
                        help="Comma-separated preset keys to bucket into (default: all 13).")
    parser.add_argument("--only-levels", default="campaign,adset,ad",
                        help="Levels to fetch (default: all three). Use `campaign` alone for a cheap smoke test.")
    parser.add_argument("--since", default=None,
                        help="yyyy-mm-dd. Cap flat-insights time_range instead of date_preset=maximum. "
                             "Saves API pages when you know your longest preset only needs N days back.")
    parser.add_argument("--output", default=None,
                        help="Path to write the full JSON report. Default: /tmp/spike_meta_<group>_<ts>.json.")
    args = parser.parse_args()

    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    if args.list_groups:
        if not args.user_id:
            parser.error("--list-groups requires --user-id")
        await list_groups(db, args.user_id)
        return

    if not args.group_id:
        parser.error("--group-id is required (or use --list-groups to pick one)")

    presets = [p.strip() for p in args.presets.split(",") if p.strip()]
    levels = [l.strip() for l in args.only_levels.split(",") if l.strip()]
    for lv in levels:
        if lv not in {"campaign", "adset", "ad"}:
            parser.error(f"--only-levels: unknown level {lv!r}")

    group = await db["client_groups"].find_one(
        {"id": args.group_id},
        {"id": 1, "name": 1, "user_id": 1, "meta_ad_account_id": 1, "_id": 0},
    )
    if not group:
        raise SystemExit(f"No client group with id={args.group_id}")

    user_id = group["user_id"]
    ad_account_id = group["meta_ad_account_id"]
    logger.info("Spike target: %s (%s) → %s", args.group_id, group.get("name"), ad_account_id)

    token = await get_facebook_token(user_id, client)
    if not token or not token.get("access_token"):
        raise SystemExit(f"No Facebook token for user {user_id}")

    started = time.monotonic()
    counted = CountedClient()

    # ─── Fetch flat insights per level ─────────────────────────
    per_level_rows: dict[str, list[dict]] = {}
    per_level_stats: dict[str, dict] = {}
    for level in levels:
        logger.info("Fetching flat insights: level=%s", level)
        pre_reqs, pre_bytes = counted.requests, counted.total_bytes
        rows, pages = await fetch_flat_insights(
            counted, ad_account_id, token["access_token"],
            level=level, since=args.since,
        )
        per_level_rows[level] = rows
        per_level_stats[level] = {
            "requests": counted.requests - pre_reqs,
            "pages": pages,
            "bytes": counted.total_bytes - pre_bytes,
            "rows": len(rows),
        }
        logger.info("  → %d rows, %d pages", len(rows), pages)

    await counted.close()

    # ─── Lead field shape probe (unknown #1) ────────────────────
    lead_field_shape = _analyze_lead_shape(per_level_rows)

    # ─── Bucketise + baseline load ──────────────────────────────
    logger.info("Bucketising daily rows into %d presets…", len(presets))
    candidate: dict[str, dict[str, dict]] = {p: {} for p in presets}
    for level, rows in per_level_rows.items():
        bucketed = bucketise_daily_to_presets(rows, level, presets)
        for preset in presets:
            candidate[preset][level] = bucketed[preset]

    logger.info("Loading baseline from client_groups.facebook_cache…")
    baseline = await load_baseline(db, args.group_id, presets)

    # ─── Diff ───────────────────────────────────────────────────
    diffs_by_preset = {
        preset: build_preset_diff(baseline[preset], candidate[preset])
        for preset in presets
    }

    # ─── Report ─────────────────────────────────────────────────
    elapsed = time.monotonic() - started
    total_requests = sum(s["requests"] for s in per_level_stats.values())
    total_pages = sum(s["pages"] for s in per_level_stats.values())
    total_bytes = sum(s["bytes"] for s in per_level_stats.values())

    ts = int(time.time())
    output_path = args.output or f"/tmp/spike_meta_{args.group_id}_{ts}.json"

    report = {
        "group_id": args.group_id,
        "group_name": group.get("name"),
        "ad_account_id": ad_account_id,
        "user_id": user_id,
        "presets": presets,
        "levels_fetched": levels,
        "since": args.since or "maximum",
        "elapsed_sec": round(elapsed, 2),
        "api_cost": {
            "candidate": per_level_stats,
            "candidate_total": {
                "requests": total_requests,
                "pages": total_pages,
                "bytes": total_bytes,
            },
            "current_note": (
                "Not re-fetched this run. Current approach is 13 nested "
                "requests per group per refresh (one per preset), page count "
                "grows linearly with campaign count."
            ),
        },
        "lead_field_shape": lead_field_shape,
        "diffs_by_preset": diffs_by_preset,
        "output_path": output_path,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    except Exception:
        pass
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(summarise(report))

    client.close()


def _analyze_lead_shape(per_level_rows: dict[str, list[dict]]) -> dict:
    """
    Look at what the flat endpoint actually returned for lead-count fields.
    Answers unknown #1.
    """
    rows_with_results = 0
    rows_with_actions = 0
    rows_with_lead_action = 0
    sample_action_types: set[str] = set()

    for level, rows in per_level_rows.items():
        for r in rows:
            if r.get("results"):
                rows_with_results += 1
            actions = r.get("actions") or []
            if actions:
                rows_with_actions += 1
                for a in actions:
                    at = a.get("action_type")
                    if at:
                        sample_action_types.add(at)
                        if at in {
                            "lead", "leadgen_grouped", "leadgen_other",
                            "onsite_conversion.lead_grouped",
                            "offsite_conversion.fb_pixel_lead",
                            "submit_application_total",
                            "onsite_web_lead", "complete_registration",
                        }:
                            rows_with_lead_action += 1

    return {
        "rows_with_results": rows_with_results,
        "rows_with_actions": rows_with_actions,
        "rows_with_lead_action": rows_with_lead_action,
        "sample_action_types": sorted(sample_action_types)[:24],
    }


if __name__ == "__main__":
    asyncio.run(main())
