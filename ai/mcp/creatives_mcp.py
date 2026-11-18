"""
ai/mcp/creatives_mcp.py
------------------------
Creative tests: find ads, list ad accounts and ad sets, list and read tests (read), draft one (write, never posts
to Meta), and post or scale one (live, two-step: a preview and a confirm_code first). They wrap
ai/tools/creative_tools.py, the same code Ask Birdy uses; user_id always comes from the token.
"""

from typing import Literal

from ai.mcp.access import LIVE, READ, WRITE
from ai.mcp.cards import card
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import creative_tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services.creatives import store

Destination = Literal["lead_form", "website", "whatsapp"]
NOT_FOUND = "Creative test not found"


def _db():
    return get_db(get_shared_mongo_client())


@mcp.tool(**READ)
async def find_ads(q: str = "", ad_account_id: str | None = None, limit: int = 10) -> dict:
    """Search the owner's cached Meta ads across their ad accounts by name, ad code (CT-0042) or text, with 30-day spend, leads and CPL.

    Use it to find an ad to revive as a creative test; pass the ad's ad_id to draft_creative_test as source_ad_id.

    Args:
        q: Name, code or text to search for. Empty lists the top spenders.
        ad_account_id: Limit to one ad account (act_...).
        limit: Max results (1-25, default 10).
    """
    return await creative_tools.find_ads(_db(), _current_user_id(), mongo_client=get_shared_mongo_client(),
                                         q=q, ad_account_id=ad_account_id, limit=limit)


@mcp.tool(**READ)
async def list_ad_accounts() -> dict:
    """The owner's Meta ad accounts, with the client names that share each one.

    Use it to get the ad_account_ids that draft_creative_test and scale_creative_test need.
    """
    return await creative_tools.list_ad_accounts(_db(), _current_user_id(), mongo_client=get_shared_mongo_client())


@mcp.tool(**READ)
async def list_adsets(ad_account_id: str, destination: Destination) -> dict:
    """The ad sets of one ad account, each flagged with whether it can serve a destination (lead_form, website or whatsapp).

    Use it to offer the owner a choice when draft_creative_test or scale_creative_test reports that an ad set is needed.

    Args:
        ad_account_id: The ad account (act_...), from list_ad_accounts.
        destination: lead_form, website or whatsapp.
    """
    return await creative_tools.list_adsets(_db(), _current_user_id(), mongo_client=get_shared_mongo_client(),
                                            ad_account_id=ad_account_id, destination=destination)


@mcp.tool(**READ)
async def list_creative_tests(status: str | None = None) -> dict:
    """The owner's creative tests with code, status, verdict and summary, newest first. Archived tests are hidden unless status is archived.

    Use it to find a test's code (CT-0042) or id before get_creative_test, post_creative_test or scale_creative_test.

    Args:
        status: Optional filter: draft, posting, testing, winner, loser, inconclusive or archived.
    """
    return await creative_tools.list_creative_tests(_db(), _current_user_id(), mongo_client=get_shared_mongo_client(),
                                                    status=status)


@mcp.tool(**READ, meta=card("creative_test"))
async def get_creative_test(code_or_id: str) -> dict:
    """One creative test in full: its targets, creative and per-account ads with post state, Meta status and score.

    Use it to check how a test is doing or to see a draft before posting it.

    Args:
        code_or_id: A code like CT-0042, or a test id.
    """
    return await creative_tools.get_creative_test(_db(), _current_user_id(), mongo_client=get_shared_mongo_client(),
                                                  code_or_id=code_or_id)


@mcp.tool(**WRITE)
async def draft_creative_test(name: str | None = None, origin: Literal["new", "revive"] | None = None,
                              destination: Destination | None = None, ad_account_ids: list[str] | None = None,
                              source_ad_id: str | None = None, primary_text: str | None = None,
                              headline: str | None = None, cta_type: str | None = None,
                              website_url: str | None = None, adset_ids: dict[str, str] | None = None,
                              page_ids: dict[str, str] | None = None, lead_form_id: str | None = None,
                              targets: dict | None = None, test_id: str | None = None) -> dict:
    """Create or update a DRAFT creative test in Birdy. It never posts anything to Meta; post_creative_test does that.

    Use origin "revive" with source_ad_id (from find_ads) to copy an existing ad. Origin "new" needs media attached
    in the Birdy app, which cannot be attached here, so use revive. Pass test_id to change an existing draft. It
    picks the ad set, page and lead form automatically when exactly one fits; otherwise the result has `needs` with
    the options. Ask the owner, then call again with adset_ids, page_ids or lead_form_id. The result's `draft.ready`
    says whether it can be posted.

    Args:
        name: Test name (required for a new draft).
        origin: revive (copy an existing ad) or new.
        destination: lead_form, website or whatsapp.
        ad_account_ids: Ad accounts to post to (from list_ad_accounts).
        source_ad_id: Revive only: the ad_id from find_ads.
        primary_text: Ad primary text.
        headline: Ad headline.
        cta_type: Meta call to action, e.g. LEARN_MORE, SIGN_UP, WHATSAPP_MESSAGE.
        website_url: Landing page (website destination).
        adset_ids: {ad_account_id: adset_id} for accounts where the owner picked one.
        page_ids: {ad_account_id: page_id} for accounts where the owner picked one.
        lead_form_id: Lead form id (lead_form destination).
        targets: {"cpl": 25} shorthand, or {mode, metrics:{cpl:{op:"lte",value}}, min_spend, min_days, win_share}.
        test_id: Existing draft id or code to update.
    """
    out = await creative_tools.draft_creative_test(
        _db(), _current_user_id(), mongo_client=get_shared_mongo_client(), name=name, origin=origin,
        destination=destination, ad_account_ids=ad_account_ids, source_ad_id=source_ad_id,
        primary_text=primary_text, headline=headline, cta_type=cta_type, website_url=website_url,
        adset_ids=adset_ids, page_ids=page_ids, lead_form_id=lead_form_id, targets=targets, test_id=test_id, created_via="mcp")
    out.pop("creative_block", None)  # markup for Ask Birdy's chat UI only
    return out


def _accounts_preview(accounts: list[dict]) -> list[dict]:
    return [{"ad_account_id": a.get("ad_account_id"), "adset": a.get("adset_name") or a.get("adset_id"),
             "page_id": a.get("page_id")} for a in accounts]


@mcp.tool(**LIVE)
async def post_creative_test(test_id: str, confirm_code: str | None = None) -> dict:
    """Post a drafted creative test to Meta. The ads are created paused in every account, then go live together once all accept. This acts on real ad accounts.

    Use it after draft_creative_test says the draft is ready. The first call returns a preview and a confirm_code and
    posts nothing.
    Show the preview to the user and only call again with confirm_code after they say yes.
    If the test is missing, already posted or not ready, you get that answer and no code.

    Args:
        test_id: Test id or code (CT-0042).
        confirm_code: The code from the preview, after the user agreed.
    """
    user_id, db = _current_user_id(), _db()
    test = await creative_tools.resolve_test(db, user_id, test_id)
    if test is None:
        return {"error": NOT_FOUND}
    if test.get("status") != store.DRAFT:
        return {"error": "This test is already posted; only a draft can be posted"}
    accounts, refusal = creative_tools.draft_readiness(test)
    if refusal:
        return refusal

    # Bound to the canonical id, so a code for one test never works for another.
    args = {"test_id": test["id"]}

    async def preview() -> dict:
        return {"action": "Post creative test to Meta", "code": test.get("code"), "name": test.get("name"),
                "destination": (test.get("creative") or {}).get("destination"), "targets": test.get("targets"),
                "accounts": _accounts_preview(accounts),
                "result": f"Ads are created paused, then go live together once all {len(accounts)} accept."}

    async def execute() -> dict:
        return await creative_tools.post_creative_test(
            db, user_id, mongo_client=get_shared_mongo_client(), test_id=test["id"], confirmed=True)

    # What the preview showed: a draft edited after it (draft_creative_test) needs a fresh yes.
    state = {"name": test.get("name"), "creative": test.get("creative"), "targets": test.get("targets"),
             "accounts": accounts}
    return await two_step(db, user_id, "post_creative_test", args, confirm_code, preview, execute, state=state)


@mcp.tool(**LIVE)
async def scale_creative_test(test_id: str, ad_account_ids: list[str], adset_ids: dict[str, str] | None = None,
                              page_ids: dict[str, str] | None = None, lead_form_id: str | None = None,
                              confirm_code: str | None = None) -> dict:
    """Add more ad accounts to a creative test that is already posted. This acts on real ad accounts.

    The first call returns a preview of the ads it would create and a confirm_code, and posts nothing.
    Show the preview to the user and only call again with confirm_code after they say yes. Pass the same arguments.
    If the test is missing, still a draft, or an account still needs an ad set, page or lead form, you get that answer (with the
    options) and no code; ask the owner, then call again with adset_ids, page_ids or lead_form_id.

    Args:
        test_id: Test id or code (CT-0042) of a posted test.
        ad_account_ids: Ad accounts to add (from list_ad_accounts).
        adset_ids: {ad_account_id: adset_id} where the owner picked one.
        page_ids: {ad_account_id: page_id} where the owner picked one.
        lead_form_id: Lead form id (lead_form destination).
        confirm_code: The code from the preview, after the user agreed.
    """
    user_id, db, client = _current_user_id(), _db(), get_shared_mongo_client()
    test = await creative_tools.resolve_test(db, user_id, test_id)
    if test is None:
        return {"error": NOT_FOUND}
    acts = list(dict.fromkeys(ad_account_ids or []))
    if not acts:
        return {"error": "ad_account_ids is required"}
    if test.get("status") in (store.DRAFT, store.ARCHIVED):
        return {"error": "Accounts can only be added to a posted, unarchived test"}
    # Order-insensitive, so a reordered list is not refused as a replay.
    args = {"test_id": test["id"], "ad_account_ids": sorted(acts), "adset_ids": dict(sorted((adset_ids or {}).items())),
            "page_ids": dict(sorted((page_ids or {}).items())), "lead_form_id": lead_form_id}
    destination = (test.get("creative") or {}).get("destination")
    accounts: list[dict] = []
    if not confirm_code:
        if destination not in creative_tools.DESTINATIONS:
            return {"error": "This test has no destination, so ad sets cannot be matched"}
        try:
            accounts, needs = await creative_tools.resolve_accounts(
                db, client, user_id, acts, destination, adset_ids, page_ids, lead_form_id)
        except creative_tools.reads.ReadError as e:
            return {"error": e.message}
        if needs:
            return {"needs": needs, "message": "Nothing was posted. Ask the user to pick these, "
                    "then call scale_creative_test again."}

    async def preview() -> dict:
        return {"action": "Add ad accounts to a posted creative test", "code": test.get("code"),
                "name": test.get("name"), "accounts": _accounts_preview(accounts),
                "result": "Ads are created paused and go live as they are accepted."}

    async def execute() -> dict:
        return await creative_tools.scale_creative_test(
            db, user_id, mongo_client=client, test_id=test["id"], ad_account_ids=acts, confirmed=True,
            adset_ids=adset_ids, page_ids=page_ids, lead_form_id=lead_form_id)

    return await two_step(db, user_id, "scale_creative_test", args, confirm_code, preview, execute)
