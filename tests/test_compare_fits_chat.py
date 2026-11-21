"""An agency-wide compare_periods must reach the chat model whole enough to answer from, never as a
truncation marker (the agent could only say the data was "too extensive")."""
import json

from ai.config import MAX_RESULT_CHARS, MAX_RESULT_ITEMS
from ai.tools.result_shaping import serialize_with_truncation, shape_for_chat

METRICS = ["spend", "impressions", "clicks", "reach", "results", "total_leads", "cpm", "cpc", "ctr", "cpl",
           "conversion_rate", "frequency", "cost_per_result"]


def _group(i):
    row = {m: 1234.5678 + i for m in METRICS}
    return {"group_id": f"g{i}", "group_name": f"Client number {i}", "currency": "GBP", "period_a": row, "period_b": row,
            "changes": {m: {"value_a": 1234.5678, "value_b": 2345.6789 + i, "delta": 1111.1111, "pct_change": 90.0}
                        for m in METRICS},
            "cpl_drivers": {"cpm": 1.0, "ctr": 2.0, "cvr": 3.0}}


def _payload(n):
    groups = [_group(i) for i in range(n)]
    return {"overall": {"period_a": groups[0]["period_a"], "period_b": groups[0]["period_b"],
                        "changes": groups[0]["changes"]},
            "groups": groups, "preset_a": "last_7d", "preset_b": "this_week_mon_today", "total_groups": 42}


def test_a_full_agency_comparison_fits_and_keeps_every_capped_client():
    raw = _payload(MAX_RESULT_ITEMS)
    assert len(json.dumps(raw)) > MAX_RESULT_CHARS                 # the case that used to be truncated
    out = json.loads(serialize_with_truncation(shape_for_chat("compare_periods", raw)))
    assert "_truncated" not in out
    assert len(out["groups"]) == MAX_RESULT_ITEMS and out["total_groups"] == 42
    assert out["groups"][3]["spend"] == [1234.5678, 2348.6789, 90.0]
    assert out["overall"] == raw["overall"] and "percent change" in out["_note"]


def test_a_small_comparison_and_other_tools_are_left_alone():
    small = _payload(2)
    assert shape_for_chat("compare_periods", small) is small
    big = _payload(MAX_RESULT_ITEMS)
    assert shape_for_chat("get_campaign_insights", big) is big
