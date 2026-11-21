"""GET /api/facebook-leads/filtered hands back the CRM contact a lead was matched to."""
from contextlib import asynccontextmanager

import pytest

import routers.meta as meta

ROW = {
    "lead_id": "L1", "full_name": "Leanne Turner", "email": "", "phone_number": "", "ad_id": "a1", "ad_name": "Ad",
    "adset_id": "s1", "adset_name": "Set", "campaign_id": "c1", "campaign_name": "Camp", "platform": "fb",
    "created_time": "2026-11-04T18:31:00Z", "client_group_id": "g1", "group_name": "Aura", "ad_account_id": "act_1",
    "field_data": {}, "lead_source": "instant_form", "ghl_matched": True, "ghl_contact_id": "ghl-4", "ghl_tags": [],
    "ghl_opportunity_status": "open", "ghl_opportunity_value": 0, "ghl_date_added": "", "match_keys": ["x"],
}


@pytest.mark.asyncio
async def test_filtered_leads_carry_the_matched_contact_id(monkeypatch):
    @asynccontextmanager
    async def no_client():
        yield object()

    async def rows(*a, **kw):
        return [dict(ROW), {**{k: v for k, v in ROW.items() if k != "ghl_contact_id"}, "lead_id": "L2", "ghl_matched": False}]

    monkeypatch.setattr(meta, "get_mongo_client", no_client)
    monkeypatch.setattr(meta, "fetch_ad_leads", rows)
    out = await meta.get_facebook_leads_filtered(start_date=None, end_date=None, groups=None, limit=5000, current_user="u1")
    leads = out["leads"] if "leads" in out else out["data"]
    assert [l["ghl_contact_id"] for l in leads] == ["ghl-4", None]
    assert "match_keys" not in leads[0]
