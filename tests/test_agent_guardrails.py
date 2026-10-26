"""
tests/test_agent_guardrails.py
------------------------------
check_guardrails: permissions, protected tags, last live ad, and the per-client
daily pause cap (local-midnight reset, DST-safe, counts every actor).
"""

import copy
from datetime import datetime, timedelta

from ai.agents.defaults import DEFAULT_GUARDRAILS, DEFAULT_PERMISSIONS
from ai.agents.guardrails import check_guardrails
from ai.agents.meta_state import AD_STATE
from ai.suggestions import store

GID = "cg1"


def _agent(tz="Europe/London", **over):
    agent = {
        "_id": "ag1",
        "user_id": "u1",
        "scope": {"client_group_ids": [GID], "schedule": {"time": "07:00", "tz": tz}},
        "permissions": copy.deepcopy(DEFAULT_PERMISSIONS),
        "guardrails": copy.deepcopy(DEFAULT_GUARDRAILS),
        "client_caps": {},
    }
    agent.update(over)
    return agent


def _doc(targets, atype="pause_ads"):
    return {
        "_id": "sug_1", "user_id": "u1", "client_group_id": GID, "agent_id": "ag1",
        "status": "open", "action": {"type": atype, "targets": targets, "params": {}},
    }


def _ad_target(ad_id="a1", name="Ad one"):
    return {"object_id": ad_id, "object_type": "ad", "name": name}


async def _seed_ads(db, rows):
    for ad_id, adset_id, status, name in rows:
        await db[AD_STATE].insert_one({
            "client_group_id": GID, "ad_id": ad_id, "adset_id": adset_id,
            "effective_status": status, "name": name,
        })


async def _seed_pauses(db, times, gid=GID, actor=store.ACTOR_USER):
    for i, t in enumerate(times):
        await db[store.ACTIVITY].insert_one({
            "_id": f"act_{gid}_{actor}_{i}_{t.isoformat()}", "user_id": "u1", "actor": actor,
            "kind": store.KIND_ACTION_APPLIED, "applied_action_type": "pause_ads",
            "client_group_id": gid, "created_at": t,
        })


NOW = datetime(2026, 10, 20, 12, 0)  # naive UTC


async def test_allows_plain_pause(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "Ad one"), ("a2", "s1", "ACTIVE", "Ad two")])
    r = await check_guardrails(mock_db, _agent(), _doc([_ad_target()]), now_utc=NOW)
    assert r.ok and r.reason is None


async def test_blocks_protected_tag(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "x"), ("a2", "s1", "ACTIVE", "y")])
    doc = _doc([_ad_target(name="Spring EVERGREEN video")])
    r = await check_guardrails(mock_db, _agent(), doc, now_utc=NOW)
    assert not r.ok and r.reason == "protected_tag"
    assert "Evergreen" in r.message


async def test_adset_target_blocked_when_a_contained_ad_is_protected(mock_db):
    # C1: the ad set's own name is clean; one of its ads is Evergreen.
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "Spring static"),
                              ("a2", "s1", "ACTIVE", "Evergreen – video"),
                              ("a3", "s2", "ACTIVE", "Other set ad")])
    doc = _doc([{"object_id": "s1", "object_type": "adset", "name": "Prospecting set"}])
    r = await check_guardrails(mock_db, _agent(), doc, now_utc=NOW)
    assert not r.ok and r.reason == "protected_tag"
    assert "Evergreen – video" in r.message

    clean = _doc([{"object_id": "s2", "object_type": "adset", "name": "Retargeting set"}])
    assert (await check_guardrails(mock_db, _agent(), clean, now_utc=NOW)).ok


async def test_protected_tag_uses_meta_state_name_when_target_has_none(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "evergreen hero"), ("a2", "s1", "ACTIVE", "y")])
    doc = _doc([{"object_id": "a1", "object_type": "ad"}])
    r = await check_guardrails(mock_db, _agent(), doc, now_utc=NOW)
    assert r.reason == "protected_tag"


async def test_blocks_last_live_ad(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "Ad one"), ("a2", "s1", "PAUSED", "Ad two"),
                              ("a3", "s2", "ACTIVE", "Other set")])
    r = await check_guardrails(mock_db, _agent(), _doc([_ad_target("a1")]), now_utc=NOW)
    assert not r.ok and r.reason == "last_live_ad"

    # Turned off → allowed.
    agent = _agent()
    agent["guardrails"]["protect_last_live_ad"] = False
    r2 = await check_guardrails(mock_db, agent, _doc([_ad_target("a1")]), now_utc=NOW)
    assert r2.ok


async def test_last_live_ad_counts_every_target_in_the_set(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "one"), ("a2", "s1", "ACTIVE", "two")])
    r = await check_guardrails(mock_db, _agent(), _doc([_ad_target("a1"), _ad_target("a2", "two")]),
                               now_utc=NOW)
    assert r.reason == "last_live_ad"


async def test_blocks_campaign_without_permission(mock_db):
    doc = _doc([{"object_id": "c1", "object_type": "campaign", "name": "Camp"}])
    r = await check_guardrails(mock_db, _agent(), doc, now_utc=NOW)
    assert not r.ok and r.reason == "no_permission"

    agent = _agent()
    agent["permissions"]["ads"] = False
    r2 = await check_guardrails(mock_db, agent, _doc([_ad_target()], atype="enable_ads"), now_utc=NOW)
    assert r2.reason == "no_permission"


async def test_permission_checked_before_protected_tag(mock_db):
    doc = _doc([{"object_id": "c1", "object_type": "campaign", "name": "Evergreen camp"}])
    r = await check_guardrails(mock_db, _agent(), doc, now_utc=NOW)
    assert r.reason == "no_permission"


async def test_pause_cap_counts_all_actors_since_local_midnight(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "one"), ("a2", "s1", "ACTIVE", "two")])
    # New York is UTC-4 on 2026-10-20. Local midnight = 04:00 UTC.
    now = datetime(2026, 10, 20, 16, 0)  # 12:00 local
    yesterday_2330_local = datetime(2026, 10, 20, 3, 30)
    await _seed_pauses(mock_db, [yesterday_2330_local] * 3)
    agent = _agent(tz="America/New_York")

    r = await check_guardrails(mock_db, agent, _doc([_ad_target()]), now_utc=now)
    assert r.ok, "yesterday's pauses must not count"

    # Today: two by the user, one auto-run by Birdy → cap of 3 reached.
    await _seed_pauses(mock_db, [datetime(2026, 10, 20, 4, 30), datetime(2026, 10, 20, 9, 0)])
    await _seed_pauses(mock_db, [datetime(2026, 10, 20, 10, 0)], actor=store.ACTOR_BIRDY)
    # Another client's pauses never count.
    await _seed_pauses(mock_db, [datetime(2026, 10, 20, 11, 0)] * 5, gid="other")

    r2 = await check_guardrails(mock_db, agent, _doc([_ad_target()]), now_utc=now)
    assert not r2.ok and r2.reason == "pause_cap"

    # The cap only applies to pauses.
    r3 = await check_guardrails(mock_db, agent, _doc([_ad_target()], atype="enable_ads"), now_utc=now)
    assert r3.ok


async def test_pause_cap_dst_boundary(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "one"), ("a2", "s1", "ACTIVE", "two")])
    # 2026-11-01: New York falls back at 02:00. Midnight that day is still EDT
    # (UTC-4) → 04:00 UTC, not 05:00 UTC as today's EST offset would suggest.
    now = datetime(2026, 11, 1, 17, 0)  # 12:00 EST
    agent = _agent(tz="America/New_York")
    await _seed_pauses(mock_db, [datetime(2026, 11, 1, 3, 59)] * 3)  # 23:59 EDT Oct 31
    r = await check_guardrails(mock_db, agent, _doc([_ad_target()]), now_utc=now)
    assert r.ok

    # 00:30 EDT on Nov 1 → today, even though it is before 05:00 UTC.
    await _seed_pauses(mock_db, [datetime(2026, 11, 1, 4, 30)] * 3)
    r2 = await check_guardrails(mock_db, agent, _doc([_ad_target()]), now_utc=now)
    assert r2.reason == "pause_cap"


async def test_client_cap_overrides_global(mock_db):
    await _seed_ads(mock_db, [("a1", "s1", "ACTIVE", "one"), ("a2", "s1", "ACTIVE", "two")])
    await _seed_pauses(mock_db, [NOW - timedelta(hours=1)])
    agent = _agent(client_caps={GID: 1})
    r = await check_guardrails(mock_db, agent, _doc([_ad_target()]), now_utc=NOW)
    assert r.reason == "pause_cap"

    agent2 = _agent(client_caps={GID: 5})
    agent2["guardrails"]["max_pauses_per_client_day"] = 0
    r2 = await check_guardrails(mock_db, agent2, _doc([_ad_target()]), now_utc=NOW)
    assert r2.ok
