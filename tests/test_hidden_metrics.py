"""
tests/test_hidden_metrics.py
----------------------------
The Metrics Hub's show/hide eye (users.hidden_metrics).

Exercised directly rather than over HTTP, same as tests/test_page_views.py:
the endpoints' only dependency is `get_mongo_client`, so patching that against
mongomock covers what matters — that a toggle is idempotent and that two
toggles can't clobber each other.
"""

import contextlib

import pytest
from fastapi import HTTPException

import routers.settings as settings
from core.models import HiddenMetricRequest

USER = "user@example.com"


@pytest.fixture
def hidden_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(settings, "get_mongo_client", fake_client)
    return settings


async def _set(metric_id, hidden):
    return await settings.set_hidden_metric(
        HiddenMetricRequest(metric_id=metric_id, hidden=hidden),
        current_user=USER,
    )


@pytest.mark.asyncio
async def test_starts_empty_for_a_user_with_no_document(hidden_api):
    assert await settings.get_hidden_metrics(current_user=USER) == {"hidden": []}


@pytest.mark.asyncio
async def test_hiding_then_showing_round_trips(hidden_api):
    assert await _set("meta_spend", True) == {"hidden": ["meta_spend"]}
    assert await settings.get_hidden_metrics(current_user=USER) == {"hidden": ["meta_spend"]}

    assert await _set("meta_spend", False) == {"hidden": []}


@pytest.mark.asyncio
async def test_hiding_twice_does_not_duplicate(hidden_api):
    await _set("meta_spend", True)
    result = await _set("meta_spend", True)

    assert result == {"hidden": ["meta_spend"]}


@pytest.mark.asyncio
async def test_showing_a_metric_that_was_never_hidden_is_a_no_op(hidden_api):
    assert await _set("ghl_contacts", False) == {"hidden": []}


@pytest.mark.asyncio
async def test_toggles_do_not_clobber_each_other(hidden_api):
    """The reason this writes with $addToSet instead of the whole array.

    Two tabs each hiding a different metric would, with a read-modify-write,
    leave only whichever landed second.
    """
    await _set("meta_spend", True)
    await _set("ghl_contacts", True)

    hidden = (await settings.get_hidden_metrics(current_user=USER))["hidden"]
    assert sorted(hidden) == ["ghl_contacts", "meta_spend"]

    await _set("meta_spend", False)
    assert (await settings.get_hidden_metrics(current_user=USER))["hidden"] == ["ghl_contacts"]


@pytest.mark.asyncio
async def test_blank_metric_id_is_rejected(hidden_api):
    with pytest.raises(HTTPException) as exc:
        await _set("   ", True)
    assert exc.value.status_code == 400
