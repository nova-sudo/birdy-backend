"""The stock agent's editable Slack templates (spec section 8): defaults, validation, rendering,
buttons added by code, and the routes."""

import copy
from datetime import datetime, timedelta

import pytest

import services.report_blocks as rb
import services.report_template as rt
from ai.agents.stock import templates as tpl
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
from ai.agents.store import AGENTS, TYPE_STOCK, create_agent
from test_stock_router import OTHER, api  # noqa: F401 - api is a fixture
from test_stock_runner import USER, _setup, fakes  # noqa: F401 - fakes is a fixture

NOW = datetime(2026, 11, 3, 8)
CHANNEL = {"type": "section", "text": {"type": "mrkdwn", "text": "<!channel>"}}


def _card(**over):
    card = {"_id": "c1", "run_id": "r1", "client_name": "Glow", "status": "open", "tag": "changes", "zone": "red",
            "currency": "GBP", "previous_cpl": 9.0, "current_cpl": 12.5, "thought_process": "CPL rose.",
            "next_steps": "Review Friday.", "additional_changes": "", "window": {"start": "2026-10-27", "end": "2026-11-02"},
            "lines": [{"id": "l1", "kind": "remove", "ad_name": "Ad A", "mode": "ask", "state": "proposed", "flags": []}]}
    card.update(over)
    return card


def _line(state, **over):
    return {"id": "l1", "kind": "remove", "ad_name": "Ad A", "mode": "ask", "state": state, "flags": [], **over}


def _ctx(**kw):
    return tpl.make_context("daily", now_utc=NOW, sample=True, **kw)


def _texts(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for v in node:
            yield from _texts(v)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _texts(v)


def _ids(buttons):
    return [e["action_id"] for e in (buttons or {}).get("elements", [])]


# ---------------------------------------------------------------------------
# Defaults and validation
# ---------------------------------------------------------------------------

def test_defaults_validate_and_contain_playbook_sections():
    assert set(tpl.DEFAULT_TEMPLATES) == {"card", "summary", "weekly"}
    for kind, template in tpl.DEFAULT_TEMPLATES.items():
        clean = tpl.validate_template(kind, template)
        assert clean["blocks"], kind
    card = " ".join(_texts(tpl.DEFAULT_TEMPLATES["card"])).lower()
    for section in ("{{client}}", "{{window}}", "{{changes}}", "{{additional_changes}}", "{{thought_process}}",
                    "{{next_steps}}", "{{follow_ups}}", "{{zone}}", "{{previous_cpl}}", "{{current_cpl}}"):
        assert section in card
    summary = tpl.DEFAULT_TEMPLATES["summary"]
    assert any(b.get("birdy_data") == {"source": "agent_run"} for b in summary["blocks"])
    # the binding is kept in the stored template
    assert any(b.get("birdy_data") == {"source": "agent_run"} for b in tpl.validate_template("summary", summary)["blocks"])


def test_unknown_placeholder_returns_error():
    bad = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Hi {{nonsense}}"}}]}
    with pytest.raises(tpl.TemplateError) as e:
        tpl.validate_template("card", bad)
    assert "nonsense" in str(e.value)


def test_placeholders_belong_to_their_template():
    weekly_token = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "{{wins}}"}}]}
    with pytest.raises(tpl.TemplateError):
        tpl.validate_template("card", weekly_token)
    assert tpl.validate_template("weekly", weekly_token)


def test_unknown_kind_is_refused():
    with pytest.raises(tpl.TemplateError):
        tpl.validate_template("nope", tpl.DEFAULT_TEMPLATES["card"])


def test_empty_blocks_returns_error():
    with pytest.raises(tpl.TemplateError):
        tpl.validate_template("card", {"blocks": []})
    with pytest.raises(tpl.TemplateError):
        tpl.validate_template("card", {})


def test_alert_block_is_refused_with_slack_reason():
    with pytest.raises(tpl.TemplateError) as e:
        tpl.validate_template("card", {"blocks": [{"type": "alert", "text": "x"}]})
    assert "modal" in str(e.value)


def test_client_table_only_in_the_summary_and_only_agent_run():
    table = {"type": "table", "birdy_data": {"source": "agent_run"}}
    with pytest.raises(tpl.TemplateError):
        tpl.validate_template("card", {"blocks": [table]})
    with pytest.raises(tpl.TemplateError):
        tpl.validate_template("summary", {"blocks": [{"type": "table", "birdy_data": {"source": "agent_run", "columns": ["x"]}}]})
    assert tpl.validate_template("summary", {"blocks": [{**table, "birdy_data": {"source": "agent_run", "columns": ["zone"]}}]})


def test_report_placeholders_still_work():
    template = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Spend {{spend}} for {{client}}"}}]}
    clean = tpl.validate_template("card", template)
    blocks, _ = tpl.render_kind("card", clean, {"client": "Glow"}, None, _ctx())
    assert "Glow" in blocks[0]["text"]["text"] and "142" in blocks[0]["text"]["text"]


def test_agent_tokens_are_refused_in_reports():
    with pytest.raises(rt.TemplateError):
        rt.validate({"frequency": "daily", "body": "Hello {{thought_process}}"})
    with pytest.raises(rb.TemplateError):
        rb.validate({"frequency": "daily", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "{{client}}"}}]})
    # existing report tokens still pass
    assert rt.validate({"frequency": "daily", "body": "Hello {{spend}} {{event}}"})["body"]


def test_report_render_is_unchanged():
    ctx = rt.Context(frequency="daily", groups=[], currency="GBP", now_utc=NOW, alerts=[], flagged=[], sample=True)
    ctx.event = {"event": "boom"}
    tmpl = rb.validate({"frequency": "daily", "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                                                                                     "text": "*a* <!here> {{event}} {{spend}}"}}]})
    blocks, _ = rb.render(tmpl, ctx)
    assert blocks[0]["text"]["text"] == "*a* <!here> boom £142"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_values_never_ping_a_channel():
    template = {"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "{{client}}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "{{changes}} & {{thought_process}}"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": "{{follow_ups}}"}]},
        {"type": "context", "elements": [{"type": "plain_text", "text": "{{next_steps}}"}]},
        {"type": "markdown", "text": "{{additional_changes}}"},
        {"type": "table", "rows": [[{"type": "raw_text", "text": "{{client}}"}]]}]}
    clean = tpl.validate_template("card", template)
    evil = "<!channel> <@U1> <https://x.test|go>"
    values = {k: evil for k in ("client", "changes", "thought_process", "follow_ups", "next_steps", "additional_changes")}
    blocks, fallback = tpl.render_kind("card", clean, values, None, _ctx())
    for text in _texts(blocks):
        assert "<!channel>" not in text and "<@U1>" not in text and "<https" not in text
    assert "<!channel>" not in fallback and "<@U1>" not in fallback
    assert "&lt;!channel&gt;" in blocks[1]["text"]["text"] and "&amp;" in blocks[1]["text"]["text"]


def test_tag_mention_is_the_one_value_that_stays_a_mention():
    clean = tpl.validate_template("summary", {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "cc {{tag}}"}}]})
    values = tpl.summary_values({"local_date": "2026-11-03", "summary": {}, "alerts": []}, [], tag_user_id="U123")
    blocks, fallback = tpl.render_kind("summary", clean, values, [], _ctx())
    assert blocks[0]["text"]["text"] == "cc <@U123>" and "<@U123>" in fallback
    bad = tpl.summary_values({"local_date": "2026-11-03", "summary": {}, "alerts": []}, [], tag_user_id="<!channel>")
    assert bad["tag"] == ""


def test_client_table_rows_come_from_run_not_caches():
    cards = [_card(client_name="<!channel> Dental", previous_cpl=9, current_cpl=12.5, zone="red"),
             _card(_id="c2", client_name="Bright", previous_cpl=None, current_cpl=6, zone="green", tag="green", lines=[])]
    rows = tpl.rows_from_cards(cards)
    blocks, fallback = tpl.render_kind("summary", tpl.DEFAULT_TEMPLATES["summary"], {}, rows, _ctx())
    table = next(b for b in blocks if b["type"] == "table")
    assert "birdy_data" not in table
    cells = [[c["text"] for c in row] for row in table["rows"]]
    assert cells[0] == ["Client", "Previous CPL", "Current CPL", "Zone"]
    assert cells[1][0] == "&lt;!channel&gt; Dental" and cells[1][1:3] == ["£9.00", "£12.50"]
    assert cells[2][1] == "no data" and "Bright" in cells[2][0]
    assert "<!channel>" not in fallback and "Bright" in fallback
    # the stored template still holds the binding
    assert any(b.get("birdy_data") for b in tpl.DEFAULT_TEMPLATES["summary"]["blocks"])


def test_empty_rows_leave_a_note_not_a_table():
    blocks, _ = tpl.render_kind("summary", tpl.DEFAULT_TEMPLATES["summary"], {}, [], _ctx())
    assert not any(b["type"] == "table" for b in blocks)


def test_a_client_named_like_a_placeholder_stays_text():
    rows = tpl.rows_from_cards([_card(client_name="{{spend}}")])
    blocks, _ = tpl.render_kind("summary", tpl.DEFAULT_TEMPLATES["summary"], {}, rows, _ctx())
    table = next(b for b in blocks if b["type"] == "table")
    assert "142" not in table["rows"][1][0]["text"]


def test_default_card_renders_with_card_values():
    values = tpl.card_values(_card(lines=[_line("proposed", ad_name="Ad <b>"), _line("flagged", id="l2", mode="flag")]))
    assert values["client"] == "Glow" and values["current_cpl"] == "£12.50" and values["previous_cpl"] == "£9.00"
    assert "27 Oct" in values["window"] and "Ad <b>" in values["changes"]
    blocks, fallback = tpl.render_kind("card", tpl.DEFAULT_TEMPLATES["card"], values, None, _ctx())
    assert "Glow" in fallback and "&lt;b&gt;" in fallback


def test_preview_leaves_a_marker_for_missing_values():
    blocks, _ = tpl.render_kind("card", tpl.DEFAULT_TEMPLATES["card"], {}, None, _ctx(preview=True))
    assert any("‹" in t for t in _texts(blocks))


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

def test_buttons_always_present_after_fit():
    huge = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * 2900}}] * 60}
    card = _card()
    blocks, _ = tpl.render_kind("card", huge, {}, None, _ctx())
    assembled = tpl.assemble(blocks, tpl.card_buttons(card))
    assert assembled[-1]["type"] == "actions" and len(assembled) <= 50
    assert len(blocks) <= 49
    assert tpl.assemble([{"type": "divider"}], None) == [{"type": "divider"}]


def test_open_card_has_apply_edit_dismiss_with_id_only_values():
    buttons = tpl.card_buttons(_card())
    assert _ids(buttons) == ["agent_card_apply", "agent_card_edit", "agent_card_dismiss"]
    assert [e["value"] for e in buttons["elements"]] == ["c1", "c1", "c1"]


def test_flag_line_card_has_no_apply_button():
    flagged = _card(status="flagged", lines=[_line("flagged", mode="flag")])
    assert _ids(tpl.card_buttons(flagged)) == ["agent_card_edit", "agent_card_dismiss"]
    mixed = _card(lines=[_line("flagged", mode="flag"), _line("proposed", id="l2")])
    assert "agent_card_apply" in _ids(tpl.card_buttons(mixed))
    mode_changed = _card(lines=[_line("flagged", mode="flag")])
    assert "agent_card_apply" not in _ids(tpl.card_buttons(mode_changed))


@pytest.mark.parametrize("status", ["superseded", "closing"])
def test_closed_cards_get_no_buttons(status):
    assert tpl.card_buttons(_card(status=status, lines=[_line("applied", applied_at=NOW)]), now=NOW) is None


def test_preview_cards_get_no_buttons():
    assert tpl.card_buttons(_card(preview=True)) is None


def test_settled_card_shows_undo_only_when_possible():
    fresh = _card(status="applied", lines=[_line("applied", applied_at=NOW - timedelta(hours=2))])
    buttons = tpl.card_buttons(fresh, now=NOW)
    assert _ids(buttons) == ["agent_card_undo:l1"] and buttons["elements"][0]["value"] == "c1:l1"
    old = _card(status="applied", lines=[_line("applied", applied_at=NOW - timedelta(hours=30))])
    assert tpl.card_buttons(old, now=NOW) is None
    explicit = _card(status="applied", lines=[_line("applied", applied_at=NOW - timedelta(hours=30), undo_until=NOW + timedelta(hours=1))])
    assert _ids(tpl.card_buttons(explicit, now=NOW)) == ["agent_card_undo:l1"]
    for state in ("undone", "undoing", "failed"):
        assert tpl.card_buttons(_card(status="applied", lines=[_line(state, applied_at=NOW)]), now=NOW) is None
    flag_applied = _card(status="applied", lines=[_line("applied", kind="flag", applied_at=NOW)])
    assert tpl.card_buttons(flag_applied, now=NOW) is None


def test_partly_applied_card_keeps_apply_dismiss_and_undo():
    card = _card(status="partly_applied", lines=[_line("applied", applied_at=NOW), _line("proposed", id="l2"),
                                                  _line("failed", id="l3")])
    assert _ids(tpl.card_buttons(card, now=NOW)) == ["agent_card_apply", "agent_card_dismiss", "agent_card_undo:l1"]
    assert "agent_card_apply" not in _ids(tpl.card_buttons(card, now=NOW, has_newer=True))
    assert "agent_card_edit" not in _ids(tpl.card_buttons(card, now=NOW))   # a partly applied card can't be edited


@pytest.mark.parametrize("state", ["queued", "applying", "undoing"])
def test_busy_lines_hide_edit_and_dismiss(state):
    card = _card(lines=[_line(state), _line("proposed", id="l2")])
    assert _ids(tpl.card_buttons(card, now=NOW)) == ["agent_card_apply"]


@pytest.mark.parametrize("state", ["needs_file", "blocked", "failed", "flagged"])
def test_waiting_lines_have_no_apply_by_themselves(state):
    card = _card(lines=[_line(state)])
    assert "agent_card_apply" not in _ids(tpl.card_buttons(card, now=NOW))


def test_dismissed_card_keeps_undo_only():
    card = _card(status="dismissed", lines=[_line("applied", applied_at=NOW)])
    assert _ids(tpl.card_buttons(card, now=NOW)) == ["agent_card_undo:l1"]
    assert tpl.card_buttons(_card(status="applied", lines=[]), now=NOW) is None


def test_summary_buttons():
    run = {"_id": "r1", "kind": "batch", "summary": {"changes": 2}}
    buttons = tpl.summary_buttons(run, "https://app.test/")
    els = buttons["elements"]
    assert [e["action_id"] for e in els] == ["agent_run_apply_all", "agent_open_report"]
    assert els[0]["value"] == "r1" and els[1]["url"] == "https://app.test/hub/agents/media-buying?run=r1"
    quiet = tpl.summary_buttons({"_id": "r1", "kind": "batch", "summary": {"changes": 0}}, "https://app.test")
    assert [e["action_id"] for e in quiet["elements"]] == ["agent_open_report"]
    preview = tpl.summary_buttons({"_id": "r1", "kind": "preview", "summary": {"changes": 3}}, "https://app.test")
    assert [e["action_id"] for e in preview["elements"]] == ["agent_open_report"]
    assert tpl.summary_buttons(run, "https://app.test", waiting=0)["elements"][0]["action_id"] == "agent_open_report"


def test_template_link_buttons_cannot_shadow_ours():
    template = {"blocks": [{"type": "actions", "elements": [
        {"type": "button", "text": "Apply", "url": "https://x.test", "action_id": "agent_card_apply"}]}]}
    with pytest.raises(tpl.TemplateError):
        tpl.validate_template("card", template)
    ok = {"blocks": [{"type": "actions", "elements": [{"type": "button", "text": "Docs", "url": "https://x.test"}]}]}
    assert tpl.validate_template("card", ok)


# ---------------------------------------------------------------------------
# Settings merge and routes
# ---------------------------------------------------------------------------

def test_merge_settings_removes_a_template_set_to_null():
    cur = merge_settings(copy.deepcopy(DEFAULT_SETTINGS), {"slack": {"templates": {"card": {"blocks": [1]}, "summary": {"blocks": [2]}}}})
    out = merge_settings(cur, {"slack": {"templates": {"card": None}}})
    assert out["slack"]["templates"] == {"summary": {"blocks": [2]}}


def _url(aid, kind="card", tail=""):
    return f"/api/agents/{aid}/slack-templates/{kind}{tail}"


async def test_template_routes_roundtrip(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    got = api.get(_url(aid)).json()
    assert got["customised"] is False and got["template"] == tpl.DEFAULT_TEMPLATES["card"]
    assert "client" in got["placeholders"] and got["defaults"] == tpl.DEFAULT_TEMPLATES["card"]
    mine = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "{{client}} is {{zone}}"}}]}
    r = api.put(_url(aid), json=mine)
    assert r.status_code == 200, r.text
    again = api.get(_url(aid)).json()
    assert again["customised"] is True and again["template"]["blocks"] == mine["blocks"]
    stored = await mock_db[AGENTS].find_one({"_id": aid})
    assert stored["settings"]["slack"]["templates"]["card"]["blocks"] == mine["blocks"]
    assert stored["settings"]["slack"]["channel_id"] is None   # the rest of Slack settings survives
    assert api.delete(_url(aid)).status_code == 200
    stored = await mock_db[AGENTS].find_one({"_id": aid})
    assert "card" not in stored["settings"]["slack"]["templates"]
    assert api.get(_url(aid)).json()["customised"] is False


async def test_template_put_reports_every_error(api, mock_db):
    agent = await _setup(mock_db, "g1")
    r = api.put(_url(agent["_id"]), json={"blocks": [{"type": "alert", "text": "x"},
                                                   {"type": "section", "text": {"type": "mrkdwn", "text": "{{bogus}}"}}]})
    assert r.status_code == 400
    assert len(r.json()["errors"]) == 2
    stored = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    assert not (stored["settings"].get("slack") or {}).get("templates")


async def test_unknown_kind_is_404(api, mock_db):
    agent = await _setup(mock_db, "g1")
    for call in (api.get(_url(agent["_id"], "nope")), api.put(_url(agent["_id"], "nope"), json={}),
                 api.delete(_url(agent["_id"], "nope")), api.post(_url(agent["_id"], "nope", "/preview"), json={})):
        assert call.status_code == 404


async def test_template_routes_are_owner_scoped_and_refuse_custom_agents(api, mock_db):
    agent = await _setup(mock_db, "g1")
    custom = await create_agent(mock_db, USER, type="custom", name="Rules")
    calls = [("get", ""), ("put", ""), ("delete", ""), ("post", "/preview")]
    for aid, wanted in ((agent["_id"], 404), (custom["_id"], 400)):
        api.state["user"] = OTHER if wanted == 404 else USER
        for method, tail in calls:
            kwargs = {"json": {"blocks": tpl.DEFAULT_TEMPLATES["card"]["blocks"]}} if method in ("put", "post") else {}
            assert getattr(api, method)(_url(aid, "card", tail), **kwargs).status_code == wanted, (method, tail)


async def test_preview_renders_sample_values_and_sends_nothing(api, mock_db, monkeypatch):
    agent = await _setup(mock_db, "g1")
    sent = []
    import slack_sdk.web.async_client as ac
    monkeypatch.setattr(ac.AsyncWebClient, "api_call", lambda *a, **k: sent.append(a), raising=False)
    before = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    r = api.post(_url(agent["_id"], "summary", "/preview"), json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["blocks"][-1]["type"] == "actions" and body["sample"] is True and body["text"]
    assert any(b["type"] == "table" and len(b["rows"]) > 1 for b in body["blocks"])
    assert sent == []
    assert await mock_db[AGENTS].find_one({"_id": agent["_id"]}) == before
    # an unsaved draft previews without being stored
    draft = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Hi {{client}}"}}]}
    body = api.post(_url(agent["_id"], "card", "/preview"), json=draft).json()
    assert "Hi" in body["blocks"][0]["text"]["text"]
    bad = api.post(_url(agent["_id"], "card", "/preview"), json={"blocks": [{"type": "alert"}]})
    assert bad.status_code == 400


async def test_settings_patch_validates_templates(api, mock_db):
    agent = await _setup(mock_db, "g1")
    r = api.patch(f"/api/agents/{agent['_id']}/settings",
                  json={"settings": {"slack": {"templates": {"card": {"blocks": [{"type": "alert"}]}}}}})
    assert r.status_code == 400
    r = api.patch(f"/api/agents/{agent['_id']}/settings", json={"settings": {"slack": {"templates": {"bogus": {"blocks": []}}}}})
    assert r.status_code == 400
