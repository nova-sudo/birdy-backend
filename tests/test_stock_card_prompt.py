"""The card chat's system prompt (BirdyHub redesign, spec section 3.2)."""

from ai.prompts import stock_card_chat
from ai.prompts.stock_agent import TRUST_BOUNDARY

CARD = {"id": "c1", "client_name": "Glow Clinic", "status": "open", "can_act": True, "preview": False, "zone": "red",
        "window": {"start": "2026-10-27", "end": "2026-11-02"}, "currency": "GBP", "previous_cpl": 9.0,
        "current_cpl": 14.5, "leads": 4, "spend": 58.0, "ads_live": 3, "thought_process": "CPL is rising.",
        "additional_changes": "", "next_steps": "Check Friday.",
        "lines": [{"id": "l1", "kind": "remove", "ad_name": "Summer <b>promo</b>", "mode": "ask", "state": "proposed",
                   "spend": 27.0, "leads": 0, "cpl": None, "flags": []},
                  {"id": "l2", "kind": "revive", "ad_name": "Old winner", "mode": "flag", "state": "flagged",
                   "spend": 10.0, "leads": 3, "cpl": 3.33, "flags": []}]}


def test_prompt_contains_lines_and_lessons():
    p = stock_card_chat.system_prompt({"card": CARD, "zones": {"green_under": 8, "red_over": 10},
                                       "lessons": ["Never pause on Fridays", {"text": "Keep one video live"}],
                                       "alerts": [{"code": "billing", "title": "Card on file: Expired"}]})
    for needle in ("Glow Clinic", "\"l1\"", "\"l2\"", "Old winner", "Never pause on Fridays", "Keep one video live",
                   "CPL is rising.", "Check Friday.", "green_under", "Card on file: Expired", "58.0"):
        assert needle in p, needle
    assert TRUST_BOUNDARY in p
    assert "<b>" not in p   # untrusted text can't open or close a block
    for rule in ("Never say you applied", "Before acting on a vague instruction", "\"always\" or \"never\"",
                 "Flag", "card_closed", "conflict"):
        assert rule in p, rule


def test_prompt_without_card_says_so():
    assert "couldn't be loaded" in stock_card_chat.system_prompt({"card": None})
