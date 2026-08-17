"""
tests/test_meta_incremental_leads.py
------------------------------------
The incremental Meta lead sync, and the regression it shipped with.

Between 2026-07-16 and the fix, one account-wide watermark was handed to every
ad and the first ad that reached it ended the scan for the whole account. These
tests pin the two properties that prevent that: a per-ad watermark, and a
"reached known leads" signal that stops one ad and nothing else.
"""

import httpx
import pytest

from integrations.facebook_utils.meta_incremental_refresh import (
    _api_get_with_retry,
    _collect_new_leads,
    _per_ad_watermarks,
)


def lead(lead_id, created):
    return {"id": lead_id, "created_time": created, "field_data": []}


class FakeAggregate:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, _length):
        return self._rows


class FakeLeads:
    """Stands in for the facebook_leads collection."""

    def __init__(self, rows):
        self._rows = rows
        self.pipeline = None

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return FakeAggregate(self._rows)


# ── the collector ────────────────────────────────────────────────────────────

def test_stamps_each_lead_with_its_ad():
    """Without ad_id on the row, the next run cannot build a watermark for
    this ad and re-walks its whole history."""
    leads, _ = _collect_new_leads(
        [lead("l1", "2026-08-01T09:00:00+0000")], "Summer promo", "ad_123", None, None
    )

    assert leads[0]["ad_id"] == "ad_123"
    assert leads[0]["ad_name"] == "Summer promo"


def test_stops_at_the_lead_we_already_have():
    rows = [
        lead("new_2", "2026-08-03T09:00:00+0000"),
        lead("new_1", "2026-08-02T09:00:00+0000"),
        lead("known", "2026-08-01T09:00:00+0000"),
        lead("older", "2026-07-30T09:00:00+0000"),
    ]

    collected, hit = _collect_new_leads(rows, "ad", "ad_1", "known", None)

    assert [c["id"] for c in collected] == ["new_2", "new_1"]
    assert hit is True


def test_stops_on_created_time_when_the_id_is_unknown():
    rows = [
        lead("new", "2026-08-03T09:00:00+0000"),
        lead("old", "2026-07-01T09:00:00+0000"),
    ]

    collected, hit = _collect_new_leads(rows, "ad", "ad_1", None, "2026-08-01T00:00:00+0000")

    assert [c["id"] for c in collected] == ["new"]
    assert hit is True


def test_takes_everything_when_the_ad_has_no_watermark():
    """A new ad — and, after the regression, any ad whose stored rows carry no
    ad_id — is walked in full. Writes are upserts, so this cannot duplicate."""
    rows = [lead("a", "2026-08-03T09:00:00+0000"), lead("b", "2026-07-01T09:00:00+0000")]

    collected, hit = _collect_new_leads(rows, "ad", "ad_1", None, None)

    assert len(collected) == 2
    assert hit is False


# ── the watermark map ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watermarks_are_keyed_per_ad():
    col = FakeLeads([
        {"_id": "ad_1", "lead_id": "l10", "created_time": "2026-08-10T09:00:00+0000"},
        {"_id": "ad_2", "lead_id": "l20", "created_time": "2026-08-02T09:00:00+0000"},
    ])

    marks = await _per_ad_watermarks(col, "u", "act_1", "g1")

    assert marks == {
        "ad_1": ("l10", "2026-08-10T09:00:00+0000"),
        "ad_2": ("l20", "2026-08-02T09:00:00+0000"),
    }


@pytest.mark.asyncio
async def test_watermark_query_ignores_rows_without_an_ad_id():
    """Rows written by the broken path have no ad_id. Treating them as a
    watermark would pin an ad to a lead we cannot attribute to it."""
    col = FakeLeads([])

    await _per_ad_watermarks(col, "u", "act_1", "g1")

    match = col.pipeline[0]["$match"]
    assert match["lead_data.ad_id"] == {"$nin": [None, ""]}
    assert match["user_id"] == "u"
    assert match["ad_account_id"] == "act_1"
    assert match["client_group_id"] == "g1"


@pytest.mark.asyncio
async def test_an_ad_with_no_stored_leads_is_simply_absent():
    col = FakeLeads([{"_id": "ad_1", "lead_id": "l1", "created_time": "2026-08-01T09:00:00+0000"}])

    marks = await _per_ad_watermarks(col, "u", "act_1", "g1")

    # The caller falls back to (None, None), which walks the ad in full.
    assert marks.get("ad_never_seen", (None, None)) == (None, None)


# ── pagination ───────────────────────────────────────────────────────────────

PAGING_URL = "https://graph.facebook.com/v25.0/ad_1/leads?access_token=TOKEN&after=CURSOR"


async def _capture_url(params):
    """Issue a paged request through the retry helper and report the URL that
    actually went out."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await _api_get_with_retry(client, PAGING_URL, params)
    return seen["url"]


@pytest.mark.asyncio
async def test_paging_keeps_the_token_and_cursor_meta_put_in_the_url():
    url = await _capture_url(None)

    assert "access_token=TOKEN" in url
    assert "after=CURSOR" in url


@pytest.mark.asyncio
async def test_an_empty_params_dict_would_strip_them():
    """Documents the bug rather than the fix: httpx builds URL(url, params),
    which replaces the query string, so `{}` sent the request bare and Meta
    answered code 104. Every lead past page 1 was lost to this."""
    url = await _capture_url({})

    assert "access_token" not in url
    assert "after" not in url
