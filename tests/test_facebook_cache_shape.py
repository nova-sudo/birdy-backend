"""
tests/test_facebook_cache_shape.py
----------------------------------
Splitting entity identity out of the per-preset buckets.

Every preset held a full copy of the account's campaigns, adsets and ads — the
same 489 ad ids, byte-identical, in all thirteen buckets, with only the numbers
differing. Each ad row carries its creative title, body and image URL, none of
which can differ between two date windows, so 462 KB of the 516 KB of ad data
in each bucket was identity repeated thirteen times.

That put client_groups at 7.48 MB on the largest group — 45% of MongoDB's hard
16 MB limit, climbing as accounts add ads. Split, the same document is ~1.03 MB.

The property that matters most here is the round trip: this is a storage change,
so a rehydrated preset must carry the same numbers as what was stored before it.
Verified across production — 70,027 entity rows, zero differing metric fields.

Identity is a different matter. Four fields drift between buckets because each
was fetched at a slightly different moment, and those consolidate to one value
rather than round-tripping exactly. See TestIdentityDrift.
"""

import pytest

from services.facebook_cache_shape import (
    has_split_shape,
    read_preset,
    rehydrate,
    split_preset_data,
)


def ad(i, spend):
    return {
        "id": f"ad{i}", "name": f"Ad {i}", "campaign_id": "c1", "adset_id": "as1",
        "status": "Active", "creative_title": "T", "creative_body": "B",
        "creative_image": "http://img",
        "spend": spend, "impressions": 100, "clicks": 5, "reach": 80,
        "results": 2, "cpm": 1.0, "cpc": 0.5, "ctr": 5.0,
    }


def preset(spend):
    return {
        "date_preset": "last_7d",
        "metrics": {"total_ads": 2, "insights": {"spend": spend}},
        "campaigns": [{"id": "c1", "name": "Camp", "status": "Active", "spend": spend}],
        "adsets": [{"id": "as1", "name": "Set", "campaign_id": "c1", "status": "Active", "spend": spend}],
        "ads": [ad(1, spend), ad(2, spend)],
    }


DATA = {"last_7d": preset(10.0), "maximum": preset(99.0)}


class TestSplit:
    def test_identity_is_stored_once_not_per_preset(self):
        entities, _ = split_preset_data(DATA)
        assert [a["id"] for a in entities["ads"]] == ["ad1", "ad2"]
        assert entities["ads"][0]["creative_body"] == "B"

    def test_identity_carries_no_metrics(self):
        """The whole point — a metric here would be stored per entity AND per
        preset, which is the duplication being removed."""
        entities, _ = split_preset_data(DATA)
        for row in entities["ads"]:
            for field in ("spend", "impressions", "clicks", "cpm", "ctr"):
                assert field not in row

    def test_metrics_are_keyed_by_id_per_preset(self):
        _, presets = split_preset_data(DATA)
        assert presets["last_7d"]["ads"]["ad1"]["spend"] == 10.0
        assert presets["maximum"]["ads"]["ad1"]["spend"] == 99.0

    def test_metrics_carry_no_identity(self):
        _, presets = split_preset_data(DATA)
        assert "creative_body" not in presets["last_7d"]["ads"]["ad1"]
        assert "name" not in presets["last_7d"]["ads"]["ad1"]

    def test_non_entity_fields_survive(self):
        _, presets = split_preset_data(DATA)
        assert presets["last_7d"]["metrics"]["insights"]["spend"] == 10.0
        assert presets["last_7d"]["date_preset"] == "last_7d"

    def test_entities_are_unioned_across_presets(self):
        """A window predating an ad does not list it. Taking one bucket as the
        source of truth would drop entities only newer windows know about."""
        data = {
            "old": {"ads": [ad(1, 1.0)]},
            "new": {"ads": [ad(1, 2.0), ad(2, 3.0)]},
        }
        entities, _ = split_preset_data(data)
        assert sorted(a["id"] for a in entities["ads"]) == ["ad1", "ad2"]

    def test_rows_without_an_id_are_skipped(self):
        entities, presets = split_preset_data({"p": {"ads": [{"name": "orphan", "spend": 5}]}})
        assert entities["ads"] == []
        assert presets["p"]["ads"] == {}

    def test_empty_input(self):
        entities, presets = split_preset_data({})
        assert entities == {"campaigns": [], "adsets": [], "ads": []}
        assert presets == {}


class TestRoundTrip:
    def test_a_rehydrated_preset_equals_what_was_stored(self):
        """This is a storage change. If the round trip is not exact, every
        consumer of the API sees the migration."""
        entities, presets = split_preset_data(DATA)
        out = rehydrate(entities, presets["last_7d"])

        for kind in ("campaigns", "adsets", "ads"):
            assert sorted(out[kind], key=lambda r: r["id"]) == \
                   sorted(DATA["last_7d"][kind], key=lambda r: r["id"])
        assert out["metrics"] == DATA["last_7d"]["metrics"]

    def test_both_presets_round_trip_independently(self):
        entities, presets = split_preset_data(DATA)
        assert rehydrate(entities, presets["maximum"])["ads"][0]["spend"] == 99.0
        assert rehydrate(entities, presets["last_7d"])["ads"][0]["spend"] == 10.0

    def test_an_entity_with_no_metrics_keeps_its_identity(self):
        """It existed, it just did not spend in this window. Dropping it would
        make the row count move with the date picker."""
        entities, presets = split_preset_data(DATA)
        presets["last_7d"]["ads"].pop("ad2")

        rows = rehydrate(entities, presets["last_7d"])["ads"]

        assert len(rows) == 2
        ad2 = next(r for r in rows if r["id"] == "ad2")
        assert ad2["name"] == "Ad 2"
        assert "spend" not in ad2


class TestReadPreset:
    def test_prefers_the_split_shape(self):
        entities, presets = split_preset_data(DATA)
        fc = {"entities": entities, "presets": presets,
              "last_7d": {"ads": [], "metrics": {"stale": True}}}

        assert read_preset(fc, "last_7d")["metrics"]["insights"]["spend"] == 10.0

    def test_falls_back_to_the_legacy_bucket(self):
        """A group that has not refreshed since the split must keep working —
        otherwise the dashboard blanks for it."""
        fc = {"last_7d": DATA["last_7d"]}

        assert read_preset(fc, "last_7d") == DATA["last_7d"]

    def test_falls_back_when_the_split_lacks_that_preset(self):
        entities, presets = split_preset_data({"maximum": preset(99.0)})
        fc = {"entities": entities, "presets": presets, "last_7d": DATA["last_7d"]}

        assert read_preset(fc, "last_7d") == DATA["last_7d"]

    def test_a_missing_preset_is_an_empty_dict(self):
        assert read_preset({}, "last_7d") == {}
        assert read_preset(None, "last_7d") == {}

    def test_has_split_shape_needs_both_halves(self):
        entities, presets = split_preset_data(DATA)
        assert has_split_shape({"entities": entities, "presets": presets})
        assert not has_split_shape({"entities": entities})
        assert not has_split_shape({"presets": presets})
        assert not has_split_shape({})


class TestIdentityDrift:
    """Buckets can disagree about an entity's attributes, because each was
    fetched at a slightly different moment. Measured on production: 11,256 ad
    rows differ on creative_image (Meta's CDN URLs are signed and ephemeral),
    1,390 on status, 130 on creative_body, 90 on name. Zero metric fields
    differ. Identity consolidates to one value; the numbers are untouched."""

    def test_the_freshest_identity_wins(self):
        stale = {"ads": [{**ad(1, 1.0), "status": "Active", "name": "Old name"}]}
        fresh = {"ads": [{**ad(1, 2.0), "status": "Paused", "name": "New name"}]}

        entities, presets = split_preset_data({"a": stale, "b": fresh})

        assert entities["ads"][0]["status"] == "Paused"
        assert entities["ads"][0]["name"] == "New name"

    def test_consolidated_identity_is_served_for_every_preset(self):
        """The point of consolidating: one answer, not thirteen that disagree."""
        stale = {"ads": [{**ad(1, 1.0), "status": "Active"}]}
        fresh = {"ads": [{**ad(1, 2.0), "status": "Paused"}]}

        entities, presets = split_preset_data({"a": stale, "b": fresh})

        assert rehydrate(entities, presets["a"])["ads"][0]["status"] == "Paused"
        assert rehydrate(entities, presets["b"])["ads"][0]["status"] == "Paused"

    def test_metrics_are_never_consolidated(self):
        """Identity is shared; numbers are per-window and must stay that way.
        This is the property the whole migration rests on."""
        a = {"ads": [{**ad(1, 1.0), "status": "Active"}]}
        b = {"ads": [{**ad(1, 2.0), "status": "Paused"}]}

        entities, presets = split_preset_data({"a": a, "b": b})

        assert rehydrate(entities, presets["a"])["ads"][0]["spend"] == 1.0
        assert rehydrate(entities, presets["b"])["ads"][0]["spend"] == 2.0
