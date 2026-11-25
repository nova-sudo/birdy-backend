"""services/slack_team_reply.py: the route line and proposal blocks a Slack team reply ends with."""

import pytest

from services import slack_team_reply as str_


def _route(*steps, seconds=6.0, credits=38.0):
    return {"seconds": seconds, "credits": credits,
            "steps": [{"agent": "birdy", "name": "Birdy", "status": "done"}, *steps]}


def test_route_line_alone():
    assert str_.route_line(_route()) == "Birdy · 6s · 38 credits"


def test_route_line_delegated_and_rounding():
    r = _route({"agent": "sales_coach", "name": "Sales Coach", "status": "done"}, seconds=11.6, credits=41.4)
    assert str_.route_line(r) == "Birdy asked Sales Coach · 12s · 41 credits"


def test_route_line_two_agents_failed_skipped_and_small_credits():
    r = _route({"agent": "media_buyer", "name": "Marketing Guru", "status": "done"},
               {"agent": "sales_coach", "name": "Sales Coach", "status": "failed"},
               {"agent": "sub:x", "name": "Scout", "status": "skipped"}, seconds=95, credits=0.44)
    assert str_.route_line(r) == "Birdy asked Marketing Guru, Sales Coach (no answer) · 1m 35s · 0.4 credits"


def test_route_line_omits_zero_credits_and_handles_missing_route():
    assert str_.route_line(_route(credits=0)) == "Birdy · 6s"
    assert str_.route_line(None) is None
    assert str_.route_line({"steps": []}) is None


def test_proposal_blocks_link_each_owner_and_cap_the_list():
    props = [{"agent": "media_buyer", "title": f"Change {i}", "detail": "**why** it helps"} for i in range(7)]
    props.append({"agent": "sales_coach", "title": "Coach the closers", "detail": ""})
    blocks = str_.proposal_blocks(props)
    assert blocks[0] == {"type": "divider"}
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == str_.MAX_PROPOSALS_SHOWN
    assert sections[0]["text"]["text"] == "*Proposal for Marketing Guru: Change 0*\n*why* it helps"
    note = blocks[-1]["elements"][0]["text"]
    assert "These wait for your approval in Birdy (3 more there)" in note
    assert "/media-buyer/suggestions|Open Marketing Guru's suggestions>" in note
    assert "/sales-coach/suggestions|Open Sales Coach's suggestions>" in note


def test_proposal_blocks_empty():
    assert str_.proposal_blocks(None) == []
    assert str_.proposal_blocks([{"agent": "media_buyer", "title": ""}]) == []


def test_build_team_reply_layout_and_block_limit():
    reply = "\n\n".join(f"Paragraph {i}" for i in range(80))   # would be cut at Slack's 50 blocks
    result = {"reply": reply, "session_id": "chat_1", "route": _route(),
              "proposals": [{"agent": "media_buyer", "title": "Pause it", "detail": "CPL doubled"}]}
    blocks, fallback, pending = str_.build_team_reply(result)
    assert len(blocks) <= 50
    assert [b.get("block_id") for b in blocks[-4:]] == [None, "team_proposal_0", "team_proposals_note", "team_route"]
    assert blocks[-4] == {"type": "divider"}
    assert "(1 proposal waiting for your approval in Birdy)" in fallback
    assert pending == []


def test_build_team_reply_without_route_is_the_plain_answer():
    from services.slack_block_formatter import build_blocks_from_reply
    result = {"reply": "Just text.", "session_id": "chat_1"}
    assert str_.build_team_reply(result)[0] == build_blocks_from_reply("Just text.", iid_prefix="chat_1")[0]


@pytest.mark.asyncio
async def test_prepare_reply_saves_each_ui_form(mock_db):
    from services.slack_interaction_store import get_pending_interaction
    reply = 'Pick one.\n:::ui\n[{"id": "p", "type": "select", "label": "Period", "options": [{"value": "w", "label": "Week"}]}]\n:::'
    result = {"reply": reply, "session_id": "chat_9", "route": _route(), "proposals": []}
    blocks, _ = await str_.prepare_reply(mock_db, result, team_id="T1", channel_id="C1", thread_ts="1.1",
                                         slack_user_id="U1", birdy_user_id="alice@example.com")
    assert blocks[-1]["block_id"] == "team_route"
    doc = await get_pending_interaction(mock_db, "chat_9:0")
    assert doc and doc["session_id"] == "chat_9" and doc["birdy_user_id"] == "alice@example.com"
