"""The remaining Ask Birdy tools on the MCP server (multi_window, hp, call_analysis, creatives, attribution)."""

from unittest.mock import AsyncMock

import pytest

from services.creatives import store

ALICE, BOB = "alice@example.com", "bob@example.com"

pytestmark = pytest.mark.asyncio

TAGS = {
    "get_daily_metrics": "read", "get_metrics_by_day_windows": "read", "get_call_center_stats": "read",
    "get_call_recordings_summary": "read", "get_closes_by_ad": "read", "find_ads": "read",
    "list_ad_accounts": "read", "list_adsets": "read", "list_creative_tests": "read", "get_creative_test": "read",
    "analyze_call_recordings": "write", "draft_creative_test": "write",
    "post_creative_test": "live", "scale_creative_test": "live",
}
LIVE_TOOLS = {"post_creative_test", "scale_creative_test"}
CODE_PHRASE = "Show the preview to the user and only call again with confirm_code after they say yes."


@pytest.fixture
def alice(mock_db, set_current_user):
    set_current_user(ALICE)


async def test_parity_tools_registered():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    assert set(TAGS) <= set(tools)


async def test_tags_and_docstrings():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    for name, kind in TAGS.items():
        tool = tools[name]
        assert tool.tags == ({"write", "live"} if kind == "live" else {kind}), name
        assert "user_id" not in tool.parameters.get("properties", {}), name
        if name in LIVE_TOOLS or name == "analyze_call_recordings":
            assert CODE_PHRASE in tool.description, name
            assert "confirm_code" in tool.parameters["properties"], name
    assert "confirmed" not in tools["analyze_call_recordings"].parameters["properties"]


# ---- delegation: one per module, user_id from the token ----------------------------------------------------

async def test_multi_window_delegates(alice, monkeypatch):
    from ai.mcp import multi_window_mcp as t
    daily = AsyncMock(return_value={"series": []})
    windows = AsyncMock(return_value={"windows": []})
    monkeypatch.setattr("ai.tools.multi_window_tools.get_daily_metrics", daily)
    monkeypatch.setattr("ai.tools.multi_window_tools.get_metrics_by_day_windows", windows)
    assert await t.get_daily_metrics.fn(days=14, group_ids=["g1"]) == {"series": []}
    assert daily.await_args.args[1] == ALICE and daily.await_args.kwargs["days"] == 14
    assert daily.await_args.kwargs["group_ids"] == ["g1"] and "mongo_client" in daily.await_args.kwargs
    await t.get_metrics_by_day_windows.fn(windows_days=[30, 60])
    assert windows.await_args.args[1] == ALICE


async def test_hp_delegates(alice, monkeypatch):
    from ai.mcp import hp_mcp as t
    m = AsyncMock(return_value={"call_center": []})
    monkeypatch.setattr("ai.tools.hp_tools.get_call_center_stats", m)
    await t.get_call_center_stats.fn(preset="last_30d")
    assert m.await_args.args[1] == ALICE and m.await_args.kwargs["preset"] == "last_30d"


async def test_attribution_delegates(alice, monkeypatch):
    from ai.mcp import attribution_mcp as t
    m = AsyncMock(return_value={"ok": True, "rows": []})
    monkeypatch.setattr("ai.tools.attribution_tools.get_closes_by_ad", m)
    await t.get_closes_by_ad.fn(level="adset", limit=5)
    assert m.await_args.args[1] == ALICE and m.await_args.kwargs["level"] == "adset"


async def test_creative_reads_delegate(alice, monkeypatch):
    from ai.mcp import creatives_mcp as t
    for name, kwargs in [("find_ads", {"q": "spring"}), ("list_ad_accounts", {}),
                         ("list_adsets", {"ad_account_id": "act_1", "destination": "website"}),
                         ("list_creative_tests", {"status": "testing"}), ("get_creative_test", {"code_or_id": "CT-0001"})]:
        m = AsyncMock(return_value={"ok": name})
        monkeypatch.setattr(f"ai.tools.creative_tools.{name}", m)
        assert await getattr(t, name).fn(**kwargs) == {"ok": name}
        assert m.await_args.args[1] == ALICE, name


async def test_draft_delegates_and_drops_chat_markup(alice, monkeypatch):
    from ai.mcp import creatives_mcp as t
    m = AsyncMock(return_value={"draft": {"test_id": "x"}, "card": {}, "creative_block": ":::creative {}:::"})
    monkeypatch.setattr("ai.tools.creative_tools.draft_creative_test", m)
    out = await t.draft_creative_test.fn(name="N", origin="revive", destination="website", ad_account_ids=["act_1"],
                                         source_ad_id="ad1", targets={"cpl": 20})
    assert out["draft"] == {"test_id": "x"} and "creative_block" not in out
    assert m.await_args.args[1] == ALICE and m.await_args.kwargs["ad_account_ids"] == ["act_1"]


# ---- call analysis ----------------------------------------------------------------------------------------

def _summary(**kw):
    return {"group_id": "g1", "group_name": "Acme", "matching_calls": 12, "with_recording": 9,
            "already_transcribed": 2, "total_recorded_minutes": 31.5, "max_calls_per_analysis": 15, **kw}


async def test_call_recordings_summary_delegates(alice, monkeypatch):
    from ai.mcp import call_analysis_mcp as t
    m = AsyncMock(return_value=_summary())
    monkeypatch.setattr("ai.tools.call_analysis_tools.get_call_recordings_summary", m)
    await t.get_call_recordings_summary.fn(group_id="g1", preset="last_7d")
    assert m.await_args.args[1] == ALICE and m.await_args.kwargs["group_id"] == "g1"


async def test_analyze_two_step_runs_with_confirmed_true(alice, monkeypatch):
    from ai.mcp import call_analysis_mcp as t
    monkeypatch.setattr("ai.tools.call_analysis_tools.get_call_recordings_summary", AsyncMock(return_value=_summary()))
    run = AsyncMock(return_value={"analyzed": 5})
    monkeypatch.setattr("ai.tools.call_analysis_tools.analyze_call_recordings", run)
    first = await t.analyze_call_recordings.fn(group_id="g1", focus="why no bookings", limit=5)
    assert first["needs_confirmation"] and first["confirm_code"]
    assert "credit" in str(first["preview"]).lower() and first["preview"]["calls_to_transcribe"] == 5
    run.assert_not_awaited()
    out = await t.analyze_call_recordings.fn(group_id="g1", focus="why no bookings", limit=5, confirm_code=first["confirm_code"])
    assert out == {"analyzed": 5}
    assert run.await_args.args[1] == ALICE and run.await_args.kwargs["confirmed"] is True
    assert run.await_args.kwargs["limit"] == 5 and run.await_args.kwargs["group_id"] == "g1"


async def test_analyze_with_no_recordings_issues_no_code(alice, monkeypatch, mock_db):
    from ai.mcp import call_analysis_mcp as t
    monkeypatch.setattr("ai.tools.call_analysis_tools.get_call_recordings_summary", AsyncMock(return_value=_summary(with_recording=0)))
    run = AsyncMock()
    monkeypatch.setattr("ai.tools.call_analysis_tools.analyze_call_recordings", run)
    out = await t.analyze_call_recordings.fn(group_id="g1", focus="x")
    assert "confirm_code" not in out and out["ok"] is False and "no call recordings" in out["error"].lower()
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    run.assert_not_awaited()


async def test_analyze_unknown_group_issues_no_code(alice, monkeypatch, mock_db):
    from ai.mcp import call_analysis_mcp as t
    monkeypatch.setattr("ai.tools.call_analysis_tools.get_call_recordings_summary",
                        AsyncMock(side_effect=ValueError("Client group 'zz' not found.")))
    out = await t.analyze_call_recordings.fn(group_id="zz", focus="x")
    assert out["ok"] is False and "not found" in out["error"] and "confirm_code" not in out
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0


async def test_analyze_code_replayed_with_other_args_is_refused(alice, monkeypatch):
    from ai.mcp import call_analysis_mcp as t
    monkeypatch.setattr("ai.tools.call_analysis_tools.get_call_recordings_summary", AsyncMock(return_value=_summary()))
    run = AsyncMock()
    monkeypatch.setattr("ai.tools.call_analysis_tools.analyze_call_recordings", run)
    first = await t.analyze_call_recordings.fn(group_id="g1", focus="f", limit=3)
    out = await t.analyze_call_recordings.fn(group_id="g1", focus="f", limit=15, confirm_code=first["confirm_code"])
    assert "Nothing was changed" in out["error"]
    run.assert_not_awaited()


# ---- creatives: post / scale ------------------------------------------------------------------------------

async def _draft(db, user=ALICE, name="Spring", **kw):
    test = await store.create_test(db, user, name=name, origin="revive", creative={"destination": "website"}, targets={})
    fields = {"draft_accounts": [{"ad_account_id": "act_1", "adset_id": "as1", "page_id": "p1"}],
              "requested_accounts": ["act_1"], **kw}
    return await store.update_test(db, user, test["id"], fields)


@pytest.fixture
def enqueue(monkeypatch):
    m = AsyncMock(return_value={"status": "posting"})
    monkeypatch.setattr("services.creatives.reads.enqueue_accounts", m)
    return m


async def test_post_creative_test_needs_confirmation(mock_db, alice, enqueue):
    from ai.mcp import creatives_mcp as t
    test = await _draft(mock_db)
    first = await t.post_creative_test.fn(test_id=test["code"])
    assert first["needs_confirmation"] and first["confirm_code"]
    assert first["preview"]["code"] == test["code"] and first["preview"]["accounts"][0]["ad_account_id"] == "act_1"
    enqueue.assert_not_awaited()
    out = await t.post_creative_test.fn(test_id=test["code"], confirm_code=first["confirm_code"])
    assert out == {"status": "posting"}
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args[1:3] == (ALICE, test["id"])
    again = await t.post_creative_test.fn(test_id=test["code"], confirm_code=first["confirm_code"])
    assert "Nothing was changed" in again["error"] and enqueue.await_count == 1


async def test_post_code_replayed_with_other_test_is_refused(mock_db, alice, enqueue):
    from ai.mcp import creatives_mcp as t
    a, b = await _draft(mock_db, name="A"), await _draft(mock_db, name="B")
    first = await t.post_creative_test.fn(test_id=a["id"])
    out = await t.post_creative_test.fn(test_id=b["id"], confirm_code=first["confirm_code"])
    assert "Nothing was changed" in out["error"]
    enqueue.assert_not_awaited()


async def test_post_issues_no_code_when_nothing_would_happen(mock_db, alice, enqueue):
    from ai.mcp import creatives_mcp as t
    out = await t.post_creative_test.fn(test_id="CT-9999")
    assert out["error"] == "Creative test not found" and "confirm_code" not in out
    posted = await _draft(mock_db, name="done", status=store.TESTING)
    out = await t.post_creative_test.fn(test_id=posted["id"])
    assert "already posted" in out["error"] and "confirm_code" not in out
    empty = await _draft(mock_db, name="empty", draft_accounts=[])
    out = await t.post_creative_test.fn(test_id=empty["id"])
    assert "confirm_code" not in out and out["error"]
    needy = await _draft(mock_db, name="needy", draft_needs=[{"ad_account_id": "act_2"}])
    out = await t.post_creative_test.fn(test_id=needy["id"])
    assert out["error"] == "draft_not_ready" and "confirm_code" not in out
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    enqueue.assert_not_awaited()


async def test_scale_creative_test_two_step(mock_db, alice, enqueue, monkeypatch):
    from ai.mcp import creatives_mcp as t
    test = await _draft(mock_db, name="live", status=store.TESTING)
    resolved = [{"ad_account_id": "act_2", "adset_id": "as2", "adset_name": "Set 2", "page_id": "p2"}]
    monkeypatch.setattr("ai.tools.creative_tools._resolve_accounts", AsyncMock(return_value=(resolved, [])))
    first = await t.scale_creative_test.fn(test_id=test["id"], ad_account_ids=["act_2"])
    assert first["needs_confirmation"] and first["preview"]["accounts"][0]["ad_account_id"] == "act_2"
    enqueue.assert_not_awaited()
    other = await t.scale_creative_test.fn(test_id=test["id"], ad_account_ids=["act_3"], confirm_code=first["confirm_code"])
    assert "Nothing was changed" in other["error"]
    first = await t.scale_creative_test.fn(test_id=test["id"], ad_account_ids=["act_2"])
    await t.scale_creative_test.fn(test_id=test["id"], ad_account_ids=["act_2"], confirm_code=first["confirm_code"])
    assert enqueue.await_args.args[1:3] == (ALICE, test["id"]) and enqueue.await_args.kwargs["add_accounts"] is True


async def test_scale_issues_no_code_for_draft_or_open_needs(mock_db, alice, enqueue, monkeypatch):
    from ai.mcp import creatives_mcp as t
    draft = await _draft(mock_db)
    out = await t.scale_creative_test.fn(test_id=draft["id"], ad_account_ids=["act_2"])
    assert out["error"] and "confirm_code" not in out
    live = await _draft(mock_db, name="live", status=store.TESTING)
    monkeypatch.setattr("ai.tools.creative_tools._resolve_accounts",
                        AsyncMock(return_value=([], [{"ad_account_id": "act_2", "adsets": []}])))
    out = await t.scale_creative_test.fn(test_id=live["id"], ad_account_ids=["act_2"])
    assert out["needs"] and "confirm_code" not in out
    assert await t.scale_creative_test.fn(test_id="CT-9999", ad_account_ids=["act_2"]) == {"error": "Creative test not found"}
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0


# ---- cross-account + scope ---------------------------------------------------------------------------------

async def test_other_users_data_is_invisible(mock_db, set_current_user, enqueue):
    from ai.mcp import creatives_mcp, hp_mcp
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": ALICE, "name": "Acme",
                                               "hotprospector_call_cache": {"last_7d": {"total_calls": 4}}})
    test = await _draft(mock_db)
    set_current_user(ALICE)
    assert (await hp_mcp.get_call_center_stats.fn())["total"] == 1
    set_current_user(BOB)
    assert (await hp_mcp.get_call_center_stats.fn())["total"] == 0
    out = await creatives_mcp.post_creative_test.fn(test_id=test["id"])
    assert out == {"error": "Creative test not found"}
    assert await creatives_mcp.get_creative_test.fn(code_or_id=test["code"]) == {"error": "Creative test not found"}
    enqueue.assert_not_awaited()


async def test_user_comes_from_the_token_through_the_server(mcp_call, mock_db):
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": ALICE, "name": "Acme",
                                               "hotprospector_call_cache": {"last_7d": {"total_calls": 4}}})
    res = await mcp_call("get_call_center_stats", {}, user=ALICE, scope="birdy.read", token_type="mcp")
    assert not res.is_error and "Acme" in res.text
    res = await mcp_call("get_call_center_stats", {}, user=BOB, scope="birdy.read", token_type="mcp")
    assert not res.is_error and "Acme" not in res.text
    res = await mcp_call("get_call_center_stats", {"user_id": ALICE}, user=BOB, scope="birdy.read", token_type="mcp")
    assert res.is_error or "Acme" not in res.text


async def test_read_scope_cannot_draft_post_or_scale(mcp_call, mock_db):
    test = await _draft(mock_db)
    for tool, args in [("draft_creative_test", {"name": "N"}), ("post_creative_test", {"test_id": test["id"]}),
                       ("scale_creative_test", {"test_id": test["id"], "ad_account_ids": ["act_2"]}),
                       ("analyze_call_recordings", {"group_id": "g1", "focus": "x"})]:
        res = await mcp_call(tool, args, scope="birdy.read", token_type="mcp")
        assert res.is_error and "view-only" in res.text, tool
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    res = await mcp_call("post_creative_test", {"test_id": test["id"]}, scope="birdy.write", token_type="mcp")
    assert not res.is_error and "confirm_code" in res.text
