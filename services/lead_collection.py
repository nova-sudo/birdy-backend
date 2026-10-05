"""
services/lead_collection.py
---------------------------
How each client collects their leads, and whether it is working.

An agency's clients don't all collect leads the same way, and until now Birdy
had no idea which way any of them did. That mattered the moment landing-page
clients appeared: the same zero in a leads column means "the ads produced
nothing" for one client and "nothing is wired up" for another, and those need
opposite reactions.

So each client group carries a `lead_collection` document declaring the method,
and this module owns reading it, writing it, and answering the only question the
setup screens actually need: what is the next thing that has to happen?

    instant_form    Meta Instant Forms. Nothing to install — Meta hands us the
                    rows. This is the case that always worked.
    form_to_ghl     A form tool that sends its leads, with the ad they came from,
                    to GoHighLevel — ROAS Forms on its own landing pages or
                    embedded in the client's site, GHL's own forms and funnels.
                    Nothing to install in Birdy: the tool writes the ad's
                    attribution onto the GHL contact, and Birdy attributes from
                    GHL (services/ad_leads.py). The only checks are whether
                    contacts are arriving and whether they carry an ad id.
    landing_page    The client's own landing page, with leads going to their own
                    records or database rather than GHL. Our script on the page
                    is the only way Birdy sees those leads, so it needs the
                    snippet and the Meta URL parameters.
    unknown         Nobody has said. The default, and it must never block
                    anything: an unconfigured client keeps behaving exactly as it
                    did before any of this existed.

"external_form" — a form tool on the client's page, wired to Birdy by webhook —
was the third option before form_to_ghl. Those tools can all send to GHL, which
is where attribution already lives, so the webhook route is retired and the
stored value reads as form_to_ghl. Webhook secrets already issued keep working
(routers/tracking.py checks whichever secret a client has).

`push_to_ghl` is opt-in per client. We hold `contacts.write`, so we *can* create
the contact in the client's CRM, but writing into someone's live CRM fires their
automations and can duplicate their records — that is their decision, not a
default.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from core.database import DB_NAME
from services.attribution_service import MATCHES, VISITORS
from services.tracked_leads import TRACKED_LEADS

logger = logging.getLogger(__name__)


METHOD_INSTANT_FORM = "instant_form"
METHOD_FORM_TO_GHL = "form_to_ghl"
METHOD_LANDING_PAGE = "landing_page"
METHOD_UNKNOWN = "unknown"
# Retired; read as METHOD_FORM_TO_GHL. See the module docstring.
METHOD_EXTERNAL_FORM = "external_form"
_LEGACY_METHODS = {METHOD_EXTERNAL_FORM: METHOD_FORM_TO_GHL}

METHODS = (METHOD_INSTANT_FORM, METHOD_FORM_TO_GHL, METHOD_LANDING_PAGE, METHOD_UNKNOWN)

# Which methods need our script on the page at all.
NEEDS_SCRIPT = (METHOD_LANDING_PAGE,)
# Which need a server-side webhook because the form can't be read in the
# browser. None any more: a form tool sends to GHL instead.
NEEDS_WEBHOOK = ()

FORM_PROVIDERS = ("roasform", "ghl", "typeform", "jotform", "custom", "other")

DEFAULT = {
    "method": METHOD_UNKNOWN,
    "form_provider": None,
    "push_to_ghl": False,
    "webhook_secret": None,
    # Extra hosts this client's form tool lives on, beyond the providers
    # everyone shares — a white-labelled form on book.theirbrand.com matches
    # nothing built in, and without this its submissions never carry a visitor
    # id. See services/tracker_script.render_tracker.
    "form_hosts": [],
    "configured_at": None,
}


def read(group: dict) -> dict:
    """The client's lead_collection, with every key present."""
    stored = (group or {}).get("lead_collection") or {}
    config = {**DEFAULT, **{k: v for k, v in stored.items() if k in DEFAULT}}
    config["method"] = normalize_method(config["method"])
    return config


def normalize_method(value) -> str:
    value = _LEGACY_METHODS.get(value, value)
    return value if value in METHODS else METHOD_UNKNOWN


def normalize_provider(value) -> str | None:
    return value if value in FORM_PROVIDERS else None


def new_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def normalize_hosts(value) -> list[str]:
    """
    Clean a customer-typed list of form hosts.

    People paste whole URLs, so take the host out of one when they do. Anything
    that isn't a usable host is dropped rather than stored — a bad entry here
    would silently never match and be invisible to debug.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    hosts = []
    for raw in value[:10]:
        if not isinstance(raw, str):
            continue
        host = raw.strip().lower()
        if not host:
            continue
        host = host.split("://")[-1].split("/")[0].strip()
        if host and "." in host or host.startswith("localhost"):
            hosts.append(host)
    return hosts


async def save(
    group_id: str,
    method: str,
    mongo_client,
    form_provider=None,
    push_to_ghl: bool = False,
    form_hosts=None,
) -> dict:
    """
    Write a client's lead-collection choice, minting a webhook secret if the
    chosen method needs one.

    The secret is minted once and kept — regenerating it on every save would
    silently break a webhook the customer had already wired into Typeform.
    """
    db = mongo_client[DB_NAME]
    group = await db["client_groups"].find_one(
        {"id": group_id}, {"lead_collection": 1, "_id": 0}
    )
    current = read(group or {})

    method = normalize_method(method)
    config = {
        "method": method,
        "form_provider": normalize_provider(form_provider),
        "push_to_ghl": bool(push_to_ghl),
        "webhook_secret": current["webhook_secret"],
        "form_hosts": (
            normalize_hosts(form_hosts) if form_hosts is not None
            else current.get("form_hosts") or []
        ),
        "configured_at": datetime.utcnow(),
    }
    if method in NEEDS_WEBHOOK and not config["webhook_secret"]:
        config["webhook_secret"] = new_webhook_secret()

    await db["client_groups"].update_one(
        {"id": group_id},
        {"$set": {"lead_collection": config, "updated_at": datetime.utcnow()}},
    )
    logger.info("Saved lead_collection for group %s: method=%s", group_id, method)
    return config


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

# How far back "is this working" looks. Long enough that a quiet weekend doesn't
# read as broken, short enough that a site taken down last month does.
LOOKBACK_DAYS = 14


async def diagnose(group: dict, mongo_client) -> dict:
    """
    The setup checklist: which stages have happened, and what to do about the
    first one that hasn't.

    Every stage carries its own sentence. A red cross with no explanation is
    worse than no checklist at all — the whole value here is that each failure
    has exactly one likely cause and one instruction, so nobody has to guess
    whether the problem is the tag, the ads, or the form.
    """
    db = mongo_client[DB_NAME]
    group_id = group["id"]
    config = read(group)
    since = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)

    # Instant-form clients need none of this, and showing them five red crosses
    # for a setup they were never asked to do would be simply wrong.
    if config["method"] == METHOD_INSTANT_FORM:
        return {
            "method": config["method"],
            "applicable": False,
            "complete": True,
            "stages": [],
            "next_step": None,
        }

    if config["method"] == METHOD_FORM_TO_GHL:
        return await _diagnose_form_to_ghl(db, group_id, config, since)

    script_seen = await db[VISITORS].count_documents(
        {"client_group_id": group_id, "last_seen_at": {"$gte": since}}, limit=1
    )
    ad_clicks = await db[VISITORS].count_documents(
        {
            "client_group_id": group_id,
            "last_seen_at": {"$gte": since},
            "last_paid_touch": {"$exists": True},
        },
        limit=1,
    )
    submissions = await db[TRACKED_LEADS].count_documents(
        {"client_group_id": group_id}, limit=1
    )
    attributed = await db[TRACKED_LEADS].count_documents(
        {"client_group_id": group_id, "ad_id": {"$gt": ""}}, limit=1
    )
    matched = await db[MATCHES].count_documents({"client_group_id": group_id}, limit=1)

    stages = [
        {
            "key": "script_installed",
            "label": "Tracking script installed",
            "done": bool(script_seen),
            "hint": (
                "We haven't seen a single page view. The snippet isn't on the page "
                "yet — or it is, but sits behind a cookie banner that blocks it "
                "until someone consents."
            ),
        },
        {
            "key": "ad_clicks_seen",
            "label": "Ad clicks arriving",
            "done": bool(ad_clicks),
            "hint": (
                "The script is running, but nobody has arrived from an ad carrying "
                "its identifiers. Add the tracking parameters to the Meta ads — "
                "without them we can see the visit but not which ad sent it."
            ),
        },
        {
            "key": "form_submissions",
            "label": "Form submissions captured",
            "done": bool(submissions),
            "hint": (
                "Visits are tracked but no form submission has reached us. Check "
                "the form is on the same page as the snippet. A form embedded "
                "from another site (an iframe) can't be read from the page — if "
                "that form tool sends its leads to GoHighLevel, choose that "
                "option instead."
            ),
        },
        {
            "key": "leads_attributed",
            "label": "Leads tied to an ad",
            "done": bool(attributed),
            "hint": (
                "Leads are arriving but without an ad id, so they can't be "
                "credited to anything. That is the Meta tracking parameters "
                "again — leads captured before they were added stay unattributed."
            ),
        },
        {
            "key": "crm_matched",
            "label": "Matched to the CRM",
            "done": bool(matched),
            "optional": True,
            "hint": (
                "None of these leads has been matched to a GoHighLevel contact "
                "yet. That is expected if the form doesn't feed GHL — the leads "
                "still count, they just won't carry opportunity status or revenue."
            ),
        },
    ]

    blocking = [s for s in stages if not s.get("optional")]
    next_step = next((s for s in blocking if not s["done"]), None)
    return {
        "method": config["method"],
        "applicable": True,
        "complete": all(s["done"] for s in blocking),
        "stages": stages,
        "next_step": next_step["key"] if next_step else None,
    }


async def _diagnose_form_to_ghl(db, group_id: str, config: dict, since: datetime) -> dict:
    """
    Nothing is installed for a form tool that sends to GoHighLevel, so the
    checklist only answers whether it is doing its half: are contacts reaching
    GHL, and do they carry the ad they came from? The second is the one that
    matters — a contact without an ad id is a lead Birdy can count but not
    credit to any ad, and the fix is on the form tool's side, not Birdy's.
    """
    recent = {"client_group_id": group_id, "contact_data.dateAdded": {"$gte": since.isoformat()}}
    contacts = await db["ghl_contacts"].count_documents(recent, limit=1)
    attributed = await db["ghl_contacts"].count_documents(
        {**recent, "contact_data.attributionSource.adId": {"$gt": ""}}, limit=1
    )
    tool = "ROAS Forms" if config.get("form_provider") == "roasform" else "the form tool"

    stages = [
        {
            "key": "ghl_contacts_arriving",
            "label": "Leads arriving in GoHighLevel",
            "done": bool(contacts),
            "hint": (
                f"No new contacts have reached this client's GoHighLevel in the "
                f"last {LOOKBACK_DAYS} days. Check {tool} is connected to the "
                "right GHL sub-account."
            ),
        },
        {
            "key": "ghl_contacts_attributed",
            "label": "Leads carry the ad they came from",
            "done": bool(attributed),
            "hint": (
                f"Contacts are arriving in GoHighLevel but none carries an ad id, "
                f"so they can't be credited to an ad. Turn on sending attribution "
                f"(UTM parameters and the ad id) to GoHighLevel in {tool}, and add "
                "the tracking parameters to the Meta ads so there is an ad id to send."
            ),
        },
    ]
    next_step = next((s["key"] for s in stages if not s["done"]), None)
    return {
        "method": config["method"],
        "applicable": True,
        "complete": next_step is None,
        "stages": stages,
        "next_step": next_step,
    }
