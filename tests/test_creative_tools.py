import pytest

from ai.orchestrator import _PAGE_TOOLS
from ai.tools import creative_tools as t
from ai.tools.registry import ToolRegistry
from services.creatives import reads, store

U = "u1@example.com"
OTHER = "u2@example.com"


async def group(db, gid, name, act, user=U, ads=None, **extra):
    cache = {"last_30d": {"ads": ads or []}}
    await db["client_groups"].insert_one({
        "id": gid, "user_id": user, "name": name, "meta_ad_account_id": act,
        "ad_account_currency": "USD", "facebook_cache": cache, **extra})


def test_registers_five_read_tools():
    reg = ToolRegistry()
    t.register_creative_tools(reg)
    for name in t.CREATIVE_READ_TOOLS:
        assert name in reg.get_tool_names()


@pytest.mark.parametrize("page", ["campaigns", "dashboard"])
def test_read_tools_on_pages(page):
    assert set(t.CREATIVE_READ_TOOLS) <= set(_PAGE_TOOLS[page])


async def test_find_ads_owner_scoped_and_compact(mock_db):
    ad = {"id": "a1", "name": "Summer Hook", "spend": 100, "total_leads": 4,
          "creative_thumbnail": "thumb", "creative_image": "big"}
    await group(mock_db, "g1", "Acme", "act_1", ads=[ad])
    await group(mock_db, "g2", "Foreign", "act_9", user=OTHER, ads=[{**ad, "id": "a9"}])
    out = await t.find_ads(mock_db, U, q="summer")
    assert [a["ad_id"] for a in out["ads"]] == ["a1"]
    assert out["ads"][0]["thumbnail"] == "thumb" and "image" not in out["ads"][0]
    assert out["ads"][0]["metrics"]["cpl"] == 25
    assert (await t.find_ads(mock_db, U, q="", ad_account_id="act_9"))["error"]


async def test_list_ad_accounts_owner_scoped(mock_db):
    await group(mock_db, "g1", "Acme", "act_1")
    await group(mock_db, "g2", "Foreign", "act_9", user=OTHER)
    out = await t.list_ad_accounts(mock_db, U)
    assert [a["ad_account_id"] for a in out["ad_accounts"]] == ["act_1"]


async def test_list_adsets_flags_compat_and_rejects_foreign(mock_db, monkeypatch):
    await group(mock_db, "g1", "Acme", "act_1")
    await group(mock_db, "g2", "Foreign", "act_9", user=OTHER)

    async def token(user_id, _c):
        return {"access_token": "tok"}

    async def adsets(act, tok):
        return [{"id": "s1", "name": "A", "destination_type": "ON_AD",
                 "campaign": {"id": "c", "name": "C", "objective": "OUTCOME_LEADS"}},
                {"id": "s2", "name": "B", "campaign": {"id": "c", "objective": "OUTCOME_TRAFFIC"}}]

    monkeypatch.setattr(reads, "get_facebook_token", token)
    monkeypatch.setattr(reads, "list_adsets", adsets)
    out = await t.list_adsets(mock_db, U, None, ad_account_id="act_1", destination="lead_form")
    assert [(a["id"], a["compatible"]) for a in out["adsets"]] == [("s1", True), ("s2", False)]
    assert "error" in await t.list_adsets(mock_db, U, None, ad_account_id="act_9", destination="lead_form")
    assert "error" in await t.list_adsets(mock_db, U, None, ad_account_id="act_1", destination="x")


async def make_test(db, user, **kw):
    return await store.create_test(db, user, name="Hook", origin="new",
                                   creative={"format": "image", "destination": "website", "media": {"image_url": "u"}},
                                   targets={}, **kw)


async def test_creative_tests_owner_scoped_and_resolve_code(mock_db):
    mine = await make_test(mock_db, U)
    theirs = await make_test(mock_db, OTHER)
    assert mine["code"] == theirs["code"] == "CT-0001"
    rows = (await t.list_creative_tests(mock_db, U))["tests"]
    assert [r["id"] for r in rows] == [mine["id"]]
    by_code = await t.get_creative_test(mock_db, U, code_or_id="CT-0001")
    assert by_code["id"] == mine["id"] and by_code["thumbnail"] == "u"
    assert (await t.get_creative_test(mock_db, U, code_or_id="ct-0001"))["id"] == mine["id"]
    assert (await t.get_creative_test(mock_db, U, code_or_id=mine["id"]))["code"] == "CT-0001"
    assert "error" in await t.get_creative_test(mock_db, U, code_or_id=theirs["id"])
    assert "error" in await t.get_creative_test(mock_db, U, code_or_id="CT-0099")


async def test_get_creative_test_lists_ads(mock_db):
    mine = await make_test(mock_db, U)
    await store.add_test_ads(mock_db, U, mine["id"], [{"ad_account_id": "act_1", "adset_name": "A",
                                                        "meta": {"ad_id": "m1"}}])
    out = await t.get_creative_test(mock_db, U, code_or_id="CT-0001")
    assert out["ads"][0]["ad_id"] == "m1" and "'_id'" not in str(out)


# ── Task 27: write tools ─────────────────────────────────────────────────────

from ai.orchestrator import _PAGE_TOOLS as PAGE_TOOLS  # noqa: E402
from ai.prompts.birdy import get_system_prompt  # noqa: E402
from ai.prompts.creatives import CREATIVE_TESTS_GUIDE  # noqa: E402
from services.creatives import posting  # noqa: E402


class Boom:
    """A db that fails on any access: proves a gated tool did nothing at all."""
    def __getitem__(self, name):
        raise AssertionError(f"db touched: {name}")

    __getattr__ = __getitem__


@pytest.fixture
def meta(monkeypatch):
    """Fake Meta reads. Set meta.adsets / meta.pages / meta.forms per test."""
    class M:
        adsets = {"act_1": [], "act_2": []}
        pages = {"act_1": [{"id": "p1", "name": "Page"}], "act_2": [{"id": "p2", "name": "Page 2"}]}
        forms = [{"id": "f1", "name": "Form", "status": "ACTIVE"}]
    m = M()

    async def token(user_id, _c):
        return {"access_token": "tok"}

    async def adsets(act, _t):
        return m.adsets[act]

    async def pages(act, _t):
        return m.pages[act]

    async def forms(page_id, _t):
        return m.forms

    monkeypatch.setattr(reads, "get_facebook_token", token)
    monkeypatch.setattr(reads, "list_adsets", adsets)
    monkeypatch.setattr(t, "list_promote_pages", pages)
    monkeypatch.setattr(t, "list_lead_forms", forms)
    return m


def adset(i, objective="OUTCOME_TRAFFIC", camp="c1"):
    return {"id": f"s{i}", "name": f"Set {i}", "campaign": {"id": camp, "objective": objective}}


async def seed_session(db, session_id, user, attachments):
    await db[t.SESSIONS].insert_one({"_id": session_id, "user_id": user, "session_attachments": attachments})


ATT = {"blob_url": "https://x.public.blob.vercel-storage.com/a.png", "mime": "image/png", "bytes": 10,
       "width": 1080, "height": 1350}
DRAFT_ARGS = dict(name="Summer Hook", origin="new", destination="website", website_url="https://a.test",
                  ad_account_ids=["act_1"], targets={"cpl": 25}, attachment_index=0, session_id="s")


async def test_draft_autopicks_single_adset_and_builds_card(mock_db, meta):
    await group(mock_db, "g1", "Acme", "act_1", meta_campaign_ids=["c1"])
    await seed_session(mock_db, "s", U, [ATT])
    meta.adsets["act_1"] = [adset(1), adset(2, "OUTCOME_AWARENESS")]
    out = await t.draft_creative_test(mock_db, U, None, **DRAFT_ARGS)
    assert "needs" not in out and out["draft"]["ready"] is True
    card = out["card"]
    assert card["code"] == "CT-0001" and card["accounts"] == [{"name": "Acme", "adset_name": "Set 1"}]
    assert card["destination_text"] == "Website: https://a.test" and "25" in card["targets_text"]
    assert card["posts_as"] == "Paused, then live together once all 1 accept"
    assert out["creative_block"].startswith(":::creative {") and out["creative_block"].endswith("}:::")
    test = await store.get_test(mock_db, U, card["test_id"])
    assert test["status"] == "draft" and test["created_via"] == "chat"
    assert test["creative"]["media"]["blob_url"] == ATT["blob_url"]
    assert test["draft_accounts"][0]["adset_id"] == "s1" and test["draft_accounts"][0]["page_id"] == "p1"
    assert test["draft_accounts"][0]["client_group_id"] == "g1"


async def test_draft_two_compatible_adsets_returns_needs(mock_db, meta):
    await group(mock_db, "g1", "Acme", "act_1")
    await seed_session(mock_db, "s", U, [ATT])
    meta.adsets["act_1"] = [adset(1), adset(2)]
    out = await t.draft_creative_test(mock_db, U, None, **DRAFT_ARGS)
    assert out["draft"]["ready"] is False
    assert out["needs"] == [{"ad_account_id": "act_1", "adsets": [
        {"id": "s1", "name": "Set 1", "campaign": None}, {"id": "s2", "name": "Set 2", "campaign": None}]}]
    # answering the need updates the same draft
    again = await t.draft_creative_test(
        mock_db, U, None, **{**DRAFT_ARGS, "test_id": out["draft"]["code"], "adset_ids": {"act_1": "s2"}})
    assert "needs" not in again and again["draft"]["test_id"] == out["draft"]["test_id"]
    assert (await store.get_test(mock_db, U, out["draft"]["test_id"]))["draft_accounts"][0]["adset_id"] == "s2"
    assert await mock_db[store.TESTS].count_documents({}) == 1


async def test_draft_rejects_foreign_account_and_foreign_adset(mock_db, meta):
    await group(mock_db, "g1", "Acme", "act_1")
    await group(mock_db, "g9", "Foreign", "act_9", user=OTHER)
    await seed_session(mock_db, "s", U, [ATT])
    meta.adsets["act_1"] = [adset(1), adset(2)]
    out = await t.draft_creative_test(mock_db, U, None, **{**DRAFT_ARGS, "ad_account_ids": ["act_9"]})
    assert "error" in out
    out = await t.draft_creative_test(mock_db, U, None, **{**DRAFT_ARGS, "adset_ids": {"act_1": "nope"}})
    assert "error" in out
    assert await mock_db[store.TESTS].count_documents({}) == 0


async def test_draft_new_without_attachment_errors(mock_db, meta):
    await group(mock_db, "g1", "Acme", "act_1")
    meta.adsets["act_1"] = [adset(1)]
    out = await t.draft_creative_test(mock_db, U, None, **{**DRAFT_ARGS, "attachment_index": 0})
    assert out["error"] == "no attachment"
    out = await t.draft_creative_test(mock_db, U, None, **{**DRAFT_ARGS, "attachment_index": None})
    assert out["error"] == "no attachment"
    # another user's session attachments are invisible
    await seed_session(mock_db, "s", OTHER, [ATT])
    out = await t.draft_creative_test(mock_db, U, None, **DRAFT_ARGS)
    assert out["error"] == "no attachment"
    assert await mock_db[store.TESTS].count_documents({}) == 0


async def test_draft_revive_copies_source_ad(mock_db, meta):
    ad = {"id": "a1", "name": "Old", "spend": 10, "creative_image": "img", "creative_thumbnail": "th",
          "creative_body": "Body", "creative_title": "Head"}
    await group(mock_db, "g1", "Acme", "act_1", ads=[ad])
    meta.adsets["act_1"] = [adset(1)]
    out = await t.draft_creative_test(mock_db, U, None, **{
        **DRAFT_ARGS, "origin": "revive", "source_ad_id": "a1", "attachment_index": None})
    test = await store.get_test(mock_db, U, out["draft"]["test_id"])
    assert test["source"] == {"ad_id": "a1", "ad_account_id": "act_1", "client_group_id": "g1"}
    assert test["creative"]["media"]["image_url"] == "img" and test["creative"]["primary_text"] == "Body"
    assert out["card"]["thumbnail"] == "th"
    miss = await t.draft_creative_test(mock_db, U, None, **{
        **DRAFT_ARGS, "origin": "revive", "source_ad_id": "zzz", "attachment_index": None})
    assert "error" in miss


async def test_update_draft_refused_once_posted(mock_db, meta):
    test = await make_test(mock_db, U)
    await store.update_test(mock_db, U, test["id"], {"status": "testing"})
    out = await t.draft_creative_test(mock_db, U, None, **{**DRAFT_ARGS, "test_id": test["id"]})
    assert "error" in out


@pytest.mark.parametrize("tool,args", [
    (t.post_creative_test, {"test_id": "x"}),
    (t.scale_creative_test, {"test_id": "x", "ad_account_ids": ["act_1"]}),
    (t.pause_creative_ads, {"test_id": "x"}),
])
@pytest.mark.parametrize("confirmed", [None, False, "true", "True", 1, "yes", {}, [1]])
async def test_gate_blocks_everything_unless_literally_true(tool, args, confirmed, monkeypatch):
    called = []

    async def spy(*a, **k):
        called.append(1)

    monkeypatch.setattr(posting, "enqueue_post", spy)
    monkeypatch.setattr(posting, "pause_ads", spy)
    kwargs = dict(args) if confirmed is None else {**args, "confirmed": confirmed}
    out = await tool(Boom(), U, None, **kwargs)
    assert out["error"] == "confirmation_required" and out["message"]
    assert called == []


async def test_post_with_confirmed_enqueues_drafted_accounts(mock_db, meta, monkeypatch):
    await group(mock_db, "g1", "Acme", "act_1")
    await seed_session(mock_db, "s", U, [ATT])
    meta.adsets["act_1"] = [adset(1)]
    draft = await t.draft_creative_test(mock_db, U, None, **DRAFT_ARGS)
    seen = {}

    real = posting.enqueue_post

    async def spy(db, user_id, test_id, **kw):
        seen.update(user_id=user_id, test_id=test_id, **kw)
        return await real(db, user_id, test_id, **kw)

    monkeypatch.setattr(posting, "enqueue_post", spy)
    out = await t.post_creative_test(mock_db, U, None, test_id=draft["draft"]["code"], confirmed=True)
    assert "error" not in out, out
    assert seen["user_id"] == U and seen["add_accounts"] is False
    assert seen["accounts"][0]["adset_id"] == "s1"
    assert (await store.get_test(mock_db, U, draft["draft"]["test_id"]))["status"] == "posting"
    # a second post is a 409 from the service, surfaced as an error
    assert "error" in await t.post_creative_test(mock_db, U, None, test_id=draft["draft"]["test_id"], confirmed=True)


async def test_post_is_owner_scoped(mock_db, meta, monkeypatch):
    mine = await make_test(mock_db, U)
    await store.update_test(mock_db, U, mine["id"], {"draft_accounts": [
        {"ad_account_id": "act_1", "adset_id": "s", "page_id": "p"}]})
    called = []

    async def spy(*a, **k):
        called.append(1)

    monkeypatch.setattr(posting, "enqueue_post", spy)
    out = await t.post_creative_test(mock_db, OTHER, None, test_id=mine["id"], confirmed=True)
    assert out["error"] == "Creative test not found" and called == []


async def test_post_without_accounts_errors(mock_db):
    mine = await make_test(mock_db, U)
    assert "error" in await t.post_creative_test(mock_db, U, None, test_id=mine["id"], confirmed=True)


async def test_scale_uses_add_accounts_path(mock_db, meta, monkeypatch):
    await group(mock_db, "g1", "Acme", "act_1")
    await group(mock_db, "g2", "Bolt", "act_2")
    mine = await make_test(mock_db, U)
    await store.update_test(mock_db, U, mine["id"], {"status": "testing"})
    meta.adsets["act_2"] = [adset(5)]
    seen = {}

    async def spy(db, user_id, test_id, **kw):
        seen.update(user_id=user_id, test_id=test_id, **kw)
        return {"ok": True}

    monkeypatch.setattr(posting, "enqueue_post", spy)
    out = await t.scale_creative_test(mock_db, U, None, test_id="CT-0001", ad_account_ids=["act_2"],
                                      confirmed=True)
    assert out == {"ok": True} and seen["add_accounts"] is True
    assert seen["accounts"][0]["ad_account_id"] == "act_2" and seen["accounts"][0]["adset_id"] == "s5"


async def test_scale_with_open_choice_returns_needs_and_writes_nothing(mock_db, meta, monkeypatch):
    await group(mock_db, "g2", "Bolt", "act_2")
    mine = await make_test(mock_db, U)
    meta.adsets["act_2"] = [adset(5), adset(6)]

    async def spy(*a, **k):
        raise AssertionError("must not enqueue")

    monkeypatch.setattr(posting, "enqueue_post", spy)
    out = await t.scale_creative_test(mock_db, U, None, test_id=mine["id"], ad_account_ids=["act_2"],
                                      confirmed=True)
    assert out["needs"][0]["ad_account_id"] == "act_2"


async def test_pause_with_confirmed_calls_pause_ads(mock_db, monkeypatch):
    mine = await make_test(mock_db, U)
    seen = {}

    async def spy(db, mongo_client, user_id, test_id, ad_account_ids, **kw):
        seen.update(user_id=user_id, test_id=test_id, accounts=ad_account_ids)
        return {"paused": 1}

    monkeypatch.setattr(posting, "pause_ads", spy)
    out = await t.pause_creative_ads(mock_db, U, None, test_id="CT-0001", ad_account_ids=["act_1"], confirmed=True)
    assert out == {"paused": 1} and seen == {"user_id": U, "test_id": mine["id"], "accounts": ["act_1"]}
    assert (await t.pause_creative_ads(mock_db, OTHER, None, test_id=mine["id"], confirmed=True))["error"]


async def test_update_targets_merges_without_gate(mock_db):
    mine = await make_test(mock_db, U)
    out = await t.update_creative_targets(mock_db, U, None, test_id="CT-0001",
                                          targets={"cpl": 30, "min_days": 5})
    assert out["targets"]["metrics"]["cpl"] == {"op": "lte", "value": 30}
    assert out["targets"]["min_days"] == 5 and out["targets"]["min_spend"] == 150
    assert "error" in await t.update_creative_targets(mock_db, U, None, test_id=mine["id"], targets={"mode": "bad"})
    assert "error" in await t.update_creative_targets(mock_db, OTHER, None, test_id=mine["id"], targets={"cpl": 1})


def test_write_tools_registered_and_paged():
    reg = ToolRegistry()
    t.register_creative_tools(reg)
    assert set(t.CREATIVE_WRITE_TOOLS) <= set(reg.get_tool_names())
    assert set(t.CREATIVE_WRITE_TOOLS) <= set(PAGE_TOOLS["campaigns"])
    assert not set(t.CREATIVE_WRITE_TOOLS) & set(PAGE_TOOLS["dashboard"])
    schema = reg.get_schemas(["post_creative_test"])[0]["function"]["parameters"]
    assert "user_id" not in schema["properties"]


def test_guide_content():
    assert "confirmed=true" in CREATIVE_TESTS_GUIDE and ":::creative" in CREATIVE_TESTS_GUIDE
    assert CREATIVE_TESTS_GUIDE not in get_system_prompt()


class _Scripted:
    def __init__(self, responses):
        self.responses, self.seen, self.model = list(responses), [], "stub"

    async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3,
                              max_tokens=2048):
        self.seen.append(messages)
        return self.responses.pop(0)


@pytest.mark.parametrize("page,has_guide", [("campaigns", True), (None, True), ("dashboard", False)])
async def test_guide_only_on_campaigns_and_general_chat(mock_db, page, has_guide):
    from ai.orchestrator import run_chat
    from ai.providers.base import ProviderResponse
    provider = _Scripted([ProviderResponse(content="hi")])
    await run_chat(provider=provider, tool_registry=ToolRegistry(), db=mock_db, user_id=U,
                   message="hello", page=page)
    system = provider.seen[0][0]["content"]
    assert (CREATIVE_TESTS_GUIDE in system) is has_guide


async def test_run_chat_overrides_model_session_id(mock_db):
    import json
    from ai.orchestrator import run_chat
    from ai.providers.base import ProviderResponse, ToolCall
    got = {}

    async def stub(db, user_id, mongo_client=None, **kw):
        got.update(kw, user_id=user_id)
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(name="draft_creative_test", description="s",
                 parameters={"type": "object", "properties": {}}, executor=stub)
    provider = _Scripted([
        ProviderResponse(content=None, finish_reason="tool_calls", tool_calls=[ToolCall(
            id="1", name="draft_creative_test",
            arguments=json.dumps({"session_id": "chat_forged", "name": "x"}))]),
        ProviderResponse(content="done")])
    out = await run_chat(provider=provider, tool_registry=reg, db=mock_db, user_id=U,
                         message="draft", page="campaigns")
    assert got["session_id"] == out["session_id"] != "chat_forged"
    assert got["user_id"] == U


async def test_post_refuses_draft_with_open_needs_and_update_keeps_requested(mock_db, meta):
    await group(mock_db, "g1", "Acme", "act_1")
    await group(mock_db, "g2", "Bolt", "act_2")
    await seed_session(mock_db, "s", U, [ATT])
    meta.adsets["act_1"] = [adset(1)]
    meta.adsets["act_2"] = [adset(2), adset(3)]
    args = {**DRAFT_ARGS, "ad_account_ids": ["act_1", "act_2"]}
    out = await t.draft_creative_test(mock_db, U, None, **args)
    assert out["needs"][0]["ad_account_id"] == "act_2"
    assert out["card"]["posts_as"] == "Paused, then live together once all 2 accept"
    posted = []

    async def spy(*a, **k):
        posted.append(1)

    import services.creatives.posting as posting_mod
    orig = posting_mod.enqueue_post
    posting_mod.enqueue_post = spy
    try:
        refused = await t.post_creative_test(mock_db, U, None, test_id=out["draft"]["test_id"], confirmed=True)
        assert refused["error"] == "draft_not_ready" and refused["needs"][0]["ad_account_id"] == "act_2"
        # an update that omits ad_account_ids keeps act_2 requested (and unresolved)
        upd = await t.draft_creative_test(mock_db, U, None, **{
            k: v for k, v in args.items() if k not in ("ad_account_ids", "name")},
            name="Renamed", test_id=out["draft"]["code"])
        assert upd["needs"][0]["ad_account_id"] == "act_2"
        assert upd["card"]["posts_as"].endswith("all 2 accept")
        assert (await t.post_creative_test(
            mock_db, U, None, test_id=out["draft"]["test_id"], confirmed=True))["error"] == "draft_not_ready"
        assert posted == []
        # resolved: post goes through
        done = await t.draft_creative_test(mock_db, U, None, **{
            **{k: v for k, v in args.items() if k != "name"}, "test_id": out["draft"]["code"],
            "adset_ids": {"act_2": "s3"}, "name": "Renamed"})
        assert "needs" not in done
        await t.post_creative_test(mock_db, U, None, test_id=out["draft"]["test_id"], confirmed=True)
        assert posted == [1]
    finally:
        posting_mod.enqueue_post = orig


async def test_scale_requires_destination(mock_db, meta):
    await group(mock_db, "g2", "Bolt", "act_2")
    test = await store.create_test(mock_db, U, name="x", origin="new", creative={"format": "image"}, targets={})
    out = await t.scale_creative_test(mock_db, U, None, test_id=test["id"], ad_account_ids=["act_2"],
                                      confirmed=True)
    assert "destination" in out["error"]


async def test_pause_passes_empty_list_unchanged(mock_db, monkeypatch):
    mine = await make_test(mock_db, U)
    seen = {}

    async def spy(db, mc, user_id, test_id, ad_account_ids, **kw):
        seen["ids"] = ad_account_ids
        return {}

    monkeypatch.setattr(posting, "pause_ads", spy)
    await t.pause_creative_ads(mock_db, U, None, test_id=mine["id"], ad_account_ids=[], confirmed=True)
    assert seen["ids"] == []
    await t.pause_creative_ads(mock_db, U, None, test_id=mine["id"], confirmed=True)
    assert seen["ids"] is None
