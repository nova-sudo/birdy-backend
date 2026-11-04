from datetime import datetime, timezone

import pytest

from ai.agents.stock.candidates import build_candidates, creative_key, normalise_name
from ai.agents.stock.scoreboard import OUTCOMES, record_for
from ai.agents.stock.windows import full_day_window
from ai.agents.store import AD_DAYS

NOW = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
WIN = full_day_window(NOW, "UTC")  # ends 2026-11-02


def test_normalise_strips_copy_and_date():
    assert normalise_name("BS - Timelapse - 100926 - Win – Copy 2") == normalise_name("BS - Timelapse - 110326 - Win")
    assert normalise_name("BS - Timelapse - 100926 - Win – Copy 2") == "bs - timelapse - win"
    assert normalise_name("Win (Copy)") == "win"


def test_creative_key():
    assert creative_key({"name": "x", "creative": {"image_hash": "h1"}}) == "img:h1"
    assert creative_key({"name": "A - 100926 - B", "creative": {"video_id": "v"}}) == "name:a - b"


def _settings(**over):
    s = {"zones": {"currency": "GBP", "green_under": 10, "red_over": 15},
         "treatments": [{"id": "t1", "name": "HIFU"}],
         "clients": {"dest": {"treatment_ids": ["t1"]}, "src": {"treatment_ids": ["t1"]}}}
    s.update(over)
    return s


AGENT = {"_id": "ag1", "user_id": "u", "settings": _settings()}
DEST = {"id": "dest", "user_id": "u", "name": "Dest", "ad_account_currency": "GBP", "meta_campaign_ids": ["dc1"]}
SRC = {"id": "src", "user_id": "u", "name": "Src", "ad_account_currency": "GBP", "meta_campaign_ids": None}


async def _groups(db, *groups):
    await db["client_groups"].insert_many([dict(g) for g in groups])


async def _ad(db, gid, ad_id, name=None, status="ACTIVE", campaign="c1", img=None, vid=None, **kw):
    await db["meta_ad_state"].insert_one({"client_group_id": gid, "ad_id": ad_id, "name": name or ad_id,
                                          "campaign_id": campaign, "effective_status": status,
                                          "creative": {"image_hash": img, "video_id": vid, "body": "Words"}, **kw})


async def _days(db, gid, ad_id, spend, leads, day="2026-10-30", campaign="c1"):
    await db[AD_DAYS].insert_one({"user_id": "u", "client_group_id": gid, "ad_id": ad_id, "campaign_id": campaign,
                                  "date_start": day, "ad_name": ad_id,
                                  "insight_data": {"spend": str(spend),
                                                   "actions": [{"action_type": "lead", "value": str(leads)}]}})


async def _winner(db, ad_id="w1", leads=10, spend=80, **kw):
    await _ad(db, "src", ad_id, img=kw.pop("img", "H"), **kw)
    await _days(db, "src", ad_id, spend, leads)


def _ids(c, kind="add"):
    return [x["ad_id"] for x in c[kind]]


async def test_basic_add(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _winner(mock_db)
    c = await build_candidates(mock_db, AGENT, DEST, WIN)
    assert _ids(c) == ["w1"] and c["add"][0]["cpl"] == 8 and c["add"][0]["ratio"] == 0.8


async def test_excludes_ad_already_run_including_rejected_copy(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _winner(mock_db, "w1", img="H1")
    await _winner(mock_db, "w2", img="H2")
    await _winner(mock_db, "w3", img="H3")
    await _ad(mock_db, "dest", "d1", img="H1", status="PAUSED")
    await _ad(mock_db, "dest", "d2", img="H2", status="DISAPPROVED")
    await _ad(mock_db, "dest", "d3", img="H3", status="ARCHIVED", campaign="elsewhere")  # any campaign
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN)) == []


async def test_image_matches_by_hash_even_with_new_name(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _winner(mock_db, "w1", name="Fresh Name", img="H1")
    await _ad(mock_db, "dest", "d1", name="Totally different", img="H1")
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN)) == []


async def test_video_matches_by_normalised_name(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _ad(mock_db, "src", "v1", name="BS - Clip - 100926 - Win", vid="V-src")
    await _days(mock_db, "src", "v1", 80, 10)
    await _ad(mock_db, "dest", "d1", name="BS - Clip - 110326 - Win – Copy", vid="V-dest", status="PAUSED")
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN)) == []


async def test_needs_shared_treatment_and_min_leads(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _winner(mock_db, "w7", leads=7, img="A")
    await _winner(mock_db, "w8", leads=8, img="B")
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN)) == ["w8"]
    agent = {**AGENT, "settings": _settings(clients={"dest": {"treatment_ids": ["t2"]},
                                                      "src": {"treatment_ids": ["t1"]}})}
    assert _ids(await build_candidates(mock_db, agent, DEST, WIN)) == []


async def test_client_without_treatment_gives_and_gets_nothing(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _winner(mock_db)
    await _ad(mock_db, "dest", "dw", img="DH")
    await _days(mock_db, "dest", "dw", 80, 10)
    no_dest = {**AGENT, "settings": _settings(clients={"dest": {}, "src": {"treatment_ids": ["t1"]}})}
    assert _ids(await build_candidates(mock_db, no_dest, DEST, WIN)) == []
    no_src = {**AGENT, "settings": _settings(clients={"dest": {"treatment_ids": ["t1"]}, "src": {}})}
    assert _ids(await build_candidates(mock_db, no_src, DEST, WIN)) == []
    missing = {**AGENT, "settings": _settings(clients={"dest": {"treatment_ids": ["t1"]}})}
    assert _ids(await build_candidates(mock_db, missing, DEST, WIN)) == []


async def test_rank_uses_cpl_over_source_green_limit_across_currencies(mock_db):
    usd = {"id": "usd", "user_id": "u", "name": "Usd", "ad_account_currency": "USD", "meta_campaign_ids": None}
    agent = {**AGENT, "settings": _settings(clients={
        "dest": {"treatment_ids": ["t1"]}, "src": {"treatment_ids": ["t1"]}, "usd": {
            "treatment_ids": ["t1"], "zones": {"green_under": 40, "red_over": 60}}})}
    await _groups(mock_db, DEST, SRC, usd)
    await _winner(mock_db, "gbp", spend=90, leads=10, img="A")      # 9 / 10 = 0.9
    await _ad(mock_db, "usd", "usd1", img="B")
    await _days(mock_db, "usd", "usd1", 200, 10)                    # 20 / 40 = 0.5, a bigger number but better
    c = await build_candidates(mock_db, agent, DEST, WIN)
    assert _ids(c) == ["usd1", "gbp"] and c["add"][0]["currency"] == "USD"


async def test_lost_twice_is_removed_and_winner_ranks_first(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _winner(mock_db, "best", spend=50, leads=10, img="A")
    await _winner(mock_db, "proven", spend=90, leads=10, img="B")
    await _winner(mock_db, "loser", spend=40, leads=10, img="C")
    rows = [{"agent_id": "ag1", "creative_key": "img:C", "treatment_ids": ["t1"], "result": "lost"}] * 2
    rows = [dict(r) for r in rows] + [{"agent_id": "ag1", "creative_key": "img:B", "treatment_ids": ["t1"],
                                        "result": "won"}]
    await mock_db[OUTCOMES].insert_many(rows)
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN)) == ["proven", "best"]
    # losses in another treatment do not count here
    await mock_db[OUTCOMES].update_many({"creative_key": "img:C"}, {"$set": {"treatment_ids": ["t9"]}})
    assert "loser" in _ids(await build_candidates(mock_db, AGENT, DEST, WIN))


async def test_record_for(mock_db):
    await mock_db[OUTCOMES].insert_many([
        {"agent_id": "a", "creative_key": "k", "treatment_ids": ["t1"], "result": "won"},
        {"agent_id": "a", "creative_key": "k", "treatment_ids": ["t1"], "result": "lost"},
        {"agent_id": "a", "creative_key": "k", "treatment_ids": ["t1"], "result": None},
        {"agent_id": "b", "creative_key": "k", "treatment_ids": ["t1"], "result": "won"}])
    assert await record_for(mock_db, "a", "k") == {"won": 1, "lost": 1, "moves": 3}
    assert await record_for(mock_db, "a", "none") == {"won": 0, "lost": 0, "moves": 0}


async def test_old_winner_needs_revive_after_days(mock_db):
    await _groups(mock_db, DEST, SRC)
    for ad_id, paused in (("old", "2026-08-01"), ("recent", "2026-10-20")):
        await _ad(mock_db, "dest", ad_id, status="PAUSED", campaign="dc1", img=ad_id, paused_at=paused)
        await _days(mock_db, "dest", ad_id, 80, 10, day="2026-07-20", campaign="dc1")
    c = await build_candidates(mock_db, AGENT, DEST, WIN)
    assert _ids(c, "revive") == ["old"]


async def test_revive_never_offers_archived_loser_or_duplicate_of_live(mock_db):
    await _groups(mock_db, DEST, SRC)
    cases = [("arch", "ARCHIVED", "A"), ("poor", "PAUSED", "B"), ("dup", "PAUSED", "C"), ("good", "PAUSED", "D")]
    for ad_id, status, img in cases:
        await _ad(mock_db, "dest", ad_id, status=status, campaign="dc1", img=img, paused_at="2026-06-01")
        await _days(mock_db, "dest", ad_id, 200 if ad_id == "poor" else 80, 10, day="2026-05-20", campaign="dc1")
    await _ad(mock_db, "dest", "live", status="ACTIVE", campaign="dc1", img="C")
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN), "revive") == ["good"]


async def test_removes_are_live_ads_in_scoped_campaigns(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _ad(mock_db, "dest", "in1", campaign="dc1")
    await _ad(mock_db, "dest", "paused", campaign="dc1", status="PAUSED")
    await _ad(mock_db, "dest", "out", campaign="other")
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN), "remove") == ["in1"]


async def test_shared_account_candidates_stay_in_group_campaigns(mock_db):
    """Review Focus 3: two groups share one ad account, each holding the whole account's ads."""
    shared_src = {**SRC, "meta_campaign_ids": ["sc1"]}
    await _groups(mock_db, DEST, shared_src)
    # destination's whole-account cache includes another group's campaign
    await _ad(mock_db, "dest", "mine", campaign="dc1", img="M")
    await _ad(mock_db, "dest", "sibling", campaign="sibling-c", img="S")
    await _ad(mock_db, "dest", "sibling-paused", campaign="sibling-c", status="PAUSED", img="SP", paused_at="2026-01-01")
    await _days(mock_db, "dest", "sibling-paused", 80, 10, day="2026-01-05", campaign="sibling-c")
    # source group: a winner in its own campaign, and one in the sibling's campaign
    await _ad(mock_db, "src", "s-own", campaign="sc1", img="O")
    await _days(mock_db, "src", "s-own", 80, 10, campaign="sc1")
    await _ad(mock_db, "src", "s-sib", campaign="sibling-c2", img="X")
    await _days(mock_db, "src", "s-sib", 80, 10, campaign="sibling-c2")
    c = await build_candidates(mock_db, AGENT, DEST, WIN)
    assert _ids(c, "add") == ["s-own"]
    assert _ids(c, "remove") == ["mine"]
    assert _ids(c, "revive") == []
    # but the whole destination account counts as "already run": a creative the sibling ran is excluded
    await _ad(mock_db, "src", "s-dup", campaign="sc1", img="S")
    await _days(mock_db, "src", "s-dup", 80, 10, campaign="sc1")
    assert "s-dup" not in _ids(await build_candidates(mock_db, AGENT, DEST, WIN), "add")


async def test_empty_campaign_scope_means_no_ads(mock_db):
    await _groups(mock_db, {**DEST, "meta_campaign_ids": []}, SRC)
    await _ad(mock_db, "dest", "x", campaign="dc1")
    assert _ids(await build_candidates(mock_db, AGENT, {**DEST, "meta_campaign_ids": []}, WIN), "remove") == []


def test_normalise_keeps_middle_copy_and_non_date_numbers():
    assert normalise_name("Hook - Copy 1 - Win") != normalise_name("Hook - Copy 2 - Win")
    assert normalise_name("Promo 123456 A") != normalise_name("Promo 654321 A")
    assert normalise_name("Hook - Win - Copy 2") == normalise_name("Hook - Win – Copy") == "hook - win"
    assert normalise_name("Hook Win Copy 2") == "hook win"
    assert normalise_name("Hook - 20261103 - Win") == "hook - win"


def test_creative_key_video_uses_name_even_with_thumbnail_hash():
    ad = {"name": "Clip - 100926", "creative": {"image_hash": "THUMB", "video_id": "v1"}}
    assert creative_key(ad) == "name:clip"


def test_creative_key_nameless_falls_back_to_ad_id():
    a = {"ad_id": "1", "name": "", "creative": {"video_id": "v"}}
    b = {"ad_id": "2", "name": "100926", "creative": {"video_id": "v"}}
    assert creative_key(a) == "id:1" and creative_key(b) == "id:2"


async def test_nameless_ads_do_not_block_each_other(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _ad(mock_db, "src", "v1", name="100926", vid="V")
    await _days(mock_db, "src", "v1", 80, 10)
    await _ad(mock_db, "dest", "d1", name="110326", vid="W", status="PAUSED")
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN)) == ["v1"]


async def test_revive_with_null_paused_at_uses_last_spend_day(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _ad(mock_db, "dest", "old", status="PAUSED", campaign="dc1", img="A", paused_at=None)
    await _days(mock_db, "dest", "old", 80, 10, day="2026-07-20", campaign="dc1")
    await _ad(mock_db, "dest", "new", status="PAUSED", campaign="dc1", img="B", paused_at=None)
    await _days(mock_db, "dest", "new", 80, 10, day="2026-10-25", campaign="dc1")
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN), "revive") == ["old"]


async def test_revive_with_no_spend_and_no_paused_at_is_skipped(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _ad(mock_db, "dest", "x", status="PAUSED", campaign="dc1", img="A", paused_at=None)
    assert _ids(await build_candidates(mock_db, AGENT, DEST, WIN), "revive") == []


async def _worded_winner(db, ad_id, **creative):
    await db["meta_ad_state"].insert_one({"client_group_id": "src", "ad_id": ad_id, "name": ad_id, "campaign_id": "c1",
                                          "effective_status": "ACTIVE", "creative": {"image_hash": f"h-{ad_id}", **creative}})
    await _days(db, "src", ad_id, 40, 10)


@pytest.mark.parametrize("body,offered", [("Words", True), (None, False), ("", False), ("   ", False)])
async def test_a_winner_with_no_readable_words_isnt_offered(body, offered, mock_db):
    await _groups(mock_db, DEST, SRC)
    await _worded_winner(mock_db, "w1", body=body)
    res = await build_candidates(mock_db, AGENT, DEST, WIN)
    assert bool(res["add"]) is offered


async def test_a_wordless_winner_doesnt_take_a_good_ones_place(mock_db):
    await _groups(mock_db, DEST, SRC)
    await _worded_winner(mock_db, "blank", body=None)
    await _worded_winner(mock_db, "good", body="Words")
    assert [c["ad_id"] for c in (await build_candidates(mock_db, AGENT, DEST, WIN))["add"]] == ["good"]
