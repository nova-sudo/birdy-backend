"""
scripts/seed_lead_sources.py
----------------------------
Seed one agency whose five clients each sit in a different `lead_source`
state, so every branch of services/ad_leads.py is visible in the real UI.

A lead can now reach Birdy three ways, and a client can be in one of five
states as a result. They look identical from the outside — spend against a
number — so the only way to check the product tells them apart is to have one
of each in front of you:

    instant_form      Meta Instant Form leads. Meta hands us the rows.
    ghl_attribution   Landing page; GoHighLevel captured the ad id itself.
    mixed             Both, with the same people arriving twice — the dedupe case.
    pixel             Landing page; only Meta's conversion count. A number, no
                      people, so the Leads tab is empty *and that is correct*.
    none              Spend, and nothing reporting a lead back at all.

The "Both" client is the one worth looking at closely: it is seeded with 40
instant-form leads and 70 attributed contacts, of which 30 are the same people.
It must report 80 leads, not 110. If it ever reports 110, the match_keys dedupe
has broken and every mixed-source client in production is over-counting.

Everything is written under a dedicated demo account and tagged
`_seed: "lead_source_demo"`, so it is invisible to real dashboards (every query
in the app is scoped by user_id) and removable in one shot.

Run:
    python -m scripts.seed_lead_sources           # wipe old demo data, seed fresh
    python -m scripts.seed_lead_sources --clean   # remove it and exit
"""

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import bcrypt
from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME
from services.facebook_cache_shape import split_preset_data
from services.meta_service import update_preset_lead_counts
from utils.phone_normalize import compute_match_keys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
SEED_TAG = "lead_source_demo"
DEMO_EMAIL = "leadsources@birdy.demo"
DEMO_PASSWORD = "BirdyDemo123!"

NOW = datetime.utcnow()

# Collections this script owns. Anything tagged SEED_TAG in them is ours to drop.
SEEDED_COLLECTIONS = ("client_groups", "ghl_contacts", "facebook_leads")


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat() + "Z"


# ── The five clients ─────────────────────────────────────────────────────────
#
# instant_form  — leads only from Meta
# ghl_attribution — leads only from the CRM's own ad attribution
# mixed         — both, overlapping
# pixel         — no lead rows anywhere, but Meta reports conversions
# none          — spend and nothing else
CLIENTS = [
    {
        "name": "Demo - Instant Forms",
        "expect": "instant_form",
        "instant_form_leads": 60,
        "ghl_leads": 0,
        "overlap": 0,
        "spend": 812.40,
        "meta_results": 58,
    },
    {
        "name": "Demo - Landing Page",
        "expect": "ghl_attribution",
        "instant_form_leads": 0,
        "ghl_leads": 80,
        "overlap": 0,
        "spend": 1043.75,
        # Meta counted more conversions than the CRM attributed. That gap is
        # the coverage the tracking script exists to close, and it must never
        # be added to the resolved count.
        "meta_results": 104,
    },
    {
        "name": "Demo - Both",
        "expect": "mixed",
        "instant_form_leads": 40,
        "ghl_leads": 70,
        "overlap": 30,      # 40 + 70 - 30 = 80 people
        "spend": 640.00,
        "meta_results": 76,
    },
    {
        "name": "Demo - Pixel Only",
        "expect": "pixel",
        "instant_form_leads": 0,
        "ghl_leads": 0,
        "overlap": 0,
        "spend": 347.56,
        "meta_results": 45,
    },
    {
        "name": "Demo - Nothing Reporting",
        "expect": "none",
        "instant_form_leads": 0,
        "ghl_leads": 0,
        "overlap": 0,
        "spend": 210.00,
        "meta_results": 0,
    },
]

PRESETS = ("maximum", "last_30d", "last_7d")


# ── Meta cache ───────────────────────────────────────────────────────────────

def _meta_cache(gid: str, spend: float, results: int) -> dict:
    """
    A Meta cache with two ads, so per-ad tables have something to draw and the
    resolver's identity enrichment has somewhere to read ad set ids from.

    Both shapes are written. `entities` is what
    ad_leads._entity_index reads for identity; the flat per-preset buckets are
    what facebook_cache_shape.read_preset falls back to for metrics.
    """
    campaign_id = f"cmp_{gid}"
    adset_id = f"as_{gid}"
    ads = [
        {"id": f"ad_{gid}_a", "name": "Winning Creative - Evergreen",
         "campaign_id": campaign_id, "adset_id": adset_id, "status": "Active"},
        {"id": f"ad_{gid}_b", "name": "Static Concept - Testing",
         "campaign_id": campaign_id, "adset_id": adset_id, "status": "Active"},
    ]
    adsets = [{"id": adset_id, "name": "Women 30-50", "campaign_id": campaign_id, "status": "Active"}]
    campaigns = [{"id": campaign_id, "name": "Body Sculpting - Lead Gen", "status": "Active"}]

    def bucket(preset: str, share: float) -> dict:
        preset_spend = round(spend * share, 2)
        preset_results = int(results * share)
        return {
            "date_preset": preset,
            "campaigns": campaigns,
            "adsets": adsets,
            "ads": ads,
            "metrics": {
                "total_campaigns": 1, "total_adsets": 1, "total_ads": len(ads),
                "insights": {
                    "spend": preset_spend,
                    "impressions": int(preset_spend * 45),
                    "clicks": int(preset_spend * 2.1),
                    "reach": int(preset_spend * 30),
                    "results": preset_results,
                    "cpm": 22.2, "cpc": 0.48, "ctr": 2.1,
                    # Deliberately left wrong-but-plausible: the whole point of
                    # the seed is to watch update_preset_lead_counts replace
                    # these with the resolved figures.
                    "cost_per_result": 0,
                    "total_leads": 0,
                },
            },
        }

    preset_data = {
        preset: bucket(preset, share)
        for preset, share in zip(PRESETS, (1.0, 0.55, 0.18))
    }

    # Build the split shape with the same helper the real refresh uses, rather
    # than hand-rolling it. update_preset_lead_counts dual-writes the patched
    # lead count into facebook_cache.presets.<preset>, and read_preset prefers
    # that bucket whenever `entities` is present — so a seed that wrote only
    # the legacy buckets got a presets.<preset> containing nothing but
    # total_leads and cost_per_result, and every other figure read as zero.
    entities, split = split_preset_data(preset_data)

    cache = {
        "ad_account_id": f"act_{gid}",
        "name": "Demo Ad Account",
        "currency": "GBP",
        "entities": entities,
        "presets": split,
        # The legacy flat copies, still written alongside in production.
        "ads": ads, "adsets": adsets, "campaigns": campaigns,
    }
    cache.update(preset_data)
    return cache


# ── People ───────────────────────────────────────────────────────────────────

def _person(gid: str, i: int) -> tuple[str, str, str, str]:
    """Stable identity for person `i` of a group, so overlap is reproducible."""
    return (
        f"Demo{i:03d}", "Client",
        f"demo{i:03d}.{gid}@example.com",
        f"07700{900000 + i:06d}"[:11],
    )


def _ghl_contact(gid: str, name: str, i: int, ad_id: str, won: bool) -> dict:
    first, last, email, phone = _person(gid, i)
    return {
        "_seed": SEED_TAG,
        "user_id": DEMO_EMAIL,
        "client_group_id": gid,
        "client_group_name": name,
        "location_id": f"loc_{gid}",
        "location_name": name,
        "contact_id": f"seedc_{uuid.uuid4().hex[:12]}",
        "lead_type": "lead",
        "match_keys": compute_match_keys(email, phone),
        "contact_data": {
            "firstName": first, "lastName": last,
            "email": email, "phone": phone,
            "dateAdded": _iso(i % 28),
            "tags": ["demo", "bodysculpt"],
            "opportunities": (
                [{"status": "won", "monetaryValue": 1800}] if won
                else [{"status": "open", "monetaryValue": 0}] if i % 3 == 0
                else []
            ),
            # The field this whole feature turns on.
            "attributionSource": {
                "utmSource": "facebook",
                "sessionSource": "Paid Social",
                "medium": "facebook",
                "campaign": "Body Sculpting - Lead Gen",
                "campaignId": f"cmp_{gid}",
                "utmMedium": "Women 30-50",
                "utmContent": "Winning Creative - Evergreen",
                "adId": ad_id,
                "adSource": "facebook",
            },
        },
        "created_at": NOW, "updated_at": NOW,
    }


def _instant_form_lead(gid: str, name: str, i: int, ad_id: str) -> dict:
    first, last, email, phone = _person(gid, i)
    lead_id = f"seedl_{uuid.uuid4().hex[:12]}"
    return {
        "_seed": SEED_TAG,
        # Top level, not just inside lead_data: facebook_leads has a unique
        # index on (user_id, ad_account_id, lead_id), so rows without it all
        # collide on null.
        "lead_id": lead_id,
        "user_id": DEMO_EMAIL,
        "client_group_id": gid,
        "client_group_name": name,
        "ad_account_id": f"act_{gid}",
        "match_keys": compute_match_keys(email, phone),
        "lead_data": {
            "id": lead_id,
            "full_name": f"{first} {last}",
            "email": email,
            "phone_number": phone,
            "created_time": _iso(i % 28),
            "ad_id": ad_id,
            "ad_name": "Winning Creative - Evergreen",
            "adset_id": f"as_{gid}",
            "adset_name": "Women 30-50",
            "campaign_id": f"cmp_{gid}",
            "campaign_name": "Body Sculpting - Lead Gen",
            "platform": "fb",
            "field_data": {"what treatment": "Fat freezing"},
        },
        "created_at": NOW, "updated_at": NOW,
    }


# ── Seed / clean ─────────────────────────────────────────────────────────────

async def clean(db) -> None:
    for coll in SEEDED_COLLECTIONS:
        result = await db[coll].delete_many({"_seed": SEED_TAG})
        if result.deleted_count:
            logger.info("Removed %d seeded docs from %s", result.deleted_count, coll)
    await db["users"].delete_one({"user_id": DEMO_EMAIL, "_seed": SEED_TAG})


async def seed(db, mongo_client) -> None:
    await clean(db)

    await db["users"].replace_one(
        {"user_id": DEMO_EMAIL},
        {
            "_seed": SEED_TAG,
            "user_id": DEMO_EMAIL,
            "name": "Lead Source Demo",
            "agency_name": "Lead Source Demo",
            "password": bcrypt.hashpw(DEMO_PASSWORD.encode(), bcrypt.gensalt()).decode(),
            "role": "user",
            "default_currency": "GBP",
            "integrations": {},
            # Without this, billing_middleware.require_active_subscription
            # answers 402 and the demo account can't reach the features it
            # exists to demonstrate.
            "subscription": {
                "status": "active",
                "price_id": os.getenv("PADDLE_PRICE_SCALE", "") or os.getenv("WHOP_PLAN_SCALE", ""),
            },
            "onboarding": {"completed": True, "completed_at": NOW},
            "created_at": NOW, "updated_at": NOW,
        },
        upsert=True,
    )

    groups, contacts, leads = [], [], []

    for spec in CLIENTS:
        gid = f"seed_{uuid.uuid4().hex[:12]}"
        name = spec["name"]
        ad_a, ad_b = f"ad_{gid}_a", f"ad_{gid}_b"

        groups.append({
            "_seed": SEED_TAG,
            "id": gid,
            "user_id": DEMO_EMAIL,
            "name": name,
            "ghl_location_id": f"loc_{gid}",
            "meta_ad_account_id": f"act_{gid}",
            "hotprospector_group_id": None,
            "call_log_provider": "none",
            "notes": f"Seeded to demonstrate lead_source = {spec['expect']}",
            "status": "active",
            "client_status": "Active",
            "ad_account_currency": "GBP",
            "facebook_cache": _meta_cache(gid, spec["spend"], spec["meta_results"]),
            "gohighlevel_cache": {}, "hotprospector_cache": {},
            "hotprospector_call_cache": {},
            "last_ghl_refresh": NOW, "last_meta_refresh": NOW, "last_hp_refresh": None,
            "created_at": NOW, "updated_at": NOW,
        })

        # Instant-form leads occupy person indexes 0..n-1.
        for i in range(spec["instant_form_leads"]):
            leads.append(_instant_form_lead(gid, name, i, ad_a if i % 2 == 0 else ad_b))

        # Attributed contacts start where the overlap says: the first
        # `overlap` of them reuse the instant-form people, so they dedupe.
        start = spec["instant_form_leads"] - spec["overlap"]
        for j in range(spec["ghl_leads"]):
            i = start + j
            contacts.append(_ghl_contact(gid, name, i, ad_a if i % 2 == 0 else ad_b, won=(j % 8 == 0)))

    if groups:
        await db["client_groups"].insert_many(groups)
    if contacts:
        await db["ghl_contacts"].insert_many(contacts)
    if leads:
        await db["facebook_leads"].insert_many(leads)

    # Run the real cache-patching path rather than writing the answers by hand,
    # so the seed exercises the production code and the numbers on screen are
    # the ones the app actually computes.
    for group in groups:
        await update_preset_lead_counts(group["id"], DEMO_EMAIL, mongo_client, presets=list(PRESETS))

    logger.info(
        "Seeded %d clients, %d attributed contacts, %d instant-form leads.",
        len(groups), len(contacts), len(leads),
    )
    logger.info("Log in as %s / %s", DEMO_EMAIL, DEMO_PASSWORD)

    # Report what each client actually resolved to, so a broken run is obvious
    # here rather than three screens deep in the UI.
    logger.info("")
    logger.info("%-28s %-16s %-16s %8s %8s", "client", "expected", "got", "leads", "CPL")
    for spec, group in zip(CLIENTS, groups):
        doc = await db["client_groups"].find_one({"id": group["id"]}, {"facebook_cache": 1})
        cache = doc.get("facebook_cache") or {}
        insights = ((cache.get("maximum") or {}).get("metrics") or {}).get("insights") or {}
        got = cache.get("lead_source", "?")
        flag = "" if got == spec["expect"] else "   <-- MISMATCH"
        logger.info(
            "%-28s %-16s %-16s %8s %8s%s",
            spec["name"], spec["expect"], got,
            insights.get("total_leads", 0), insights.get("cost_per_result", 0), flag,
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="remove the demo data and exit")
    args = parser.parse_args()

    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    try:
        if args.clean:
            await clean(db)
            logger.info("Demo data removed.")
        else:
            await seed(db, client)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
