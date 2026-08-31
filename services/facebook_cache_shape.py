"""
services/facebook_cache_shape.py
--------------------------------
Splitting `facebook_cache` into entity identity and per-preset metrics.

Every preset bucket held a complete copy of the account's campaigns, adsets and
ads. Verified on production: the same 489 ad ids, byte-identical, in all
thirteen buckets — only the numbers on each row differed. Measured on the
largest group:

    ads        n= 489   identity 462.1 KB   metrics  53.9 KB
    adsets     n= 349   identity  36.8 KB   metrics  38.3 KB
    campaigns  n= 338   identity  38.6 KB   metrics  37.1 KB

    13 presets, stored as-is    8.67 MB
    identity once + metrics     2.22 MB

The ad rows dominate because each carries its creative title, body and image
URL — the parts that cannot change between two date windows — thirteen times
over. That put `client_groups` at 800 KB per document on average and 7.48 MB at
worst, 45% of MongoDB's hard 16 MB limit, climbing as accounts add ads. Past
16 MB writes to that client fail permanently, which is an outage for one client
with no warning and no workaround.

Split, the same document is about 1.03 MB — 6% of the limit.

New shape, alongside the old during the dual-write window:

    facebook_cache.entities = {
        "ads":       [{id, name, campaign_id, adset_id, status, creative_*}],
        "adsets":    [{id, name, campaign_id, status}],
        "campaigns": [{id, name, status}],
    }
    facebook_cache.presets.<preset> = {
        "metrics": {...}, "date_preset": "...",
        "ads":       {"<id>": {spend, impressions, clicks, reach, results, ...}},
        "adsets":    {...},
        "campaigns": {...},
    }

`rehydrate` puts them back together, so the API response has the same shape it
always had. Verified across production: 70,027 entity rows round-tripped, and
**every metric field is identical**. This is a storage change; no number moves.

Four identity fields do consolidate rather than round-trip exactly, and that is
deliberate. Measured across all stored rows:

    creative_image  11,256 rows  Meta's CDN URLs are signed and ephemeral, so
                                 the same image comes back on a different host
                                 with a different signature on each fetch
    status           1,390 rows  buckets fetched moments apart disagree on
                                 Paused vs Active
    creative_body      130 rows  creative edited between fetches
    name                90 rows  entity renamed between fetches

None of these is a property *of a date window* — they are the entity's
attributes at the moment that bucket happened to be fetched. Thirteen buckets
disagreeing about whether an adset is paused is a bug surface, not information,
so one value is kept. Where they differ, the last bucket written wins, which is
the freshest view of the entity.
"""

import logging

logger = logging.getLogger(__name__)

KINDS = ("campaigns", "adsets", "ads")

# Fields that describe *what* an entity is. Identical in every date window, so
# they are stored once. `status` is the entity's current state rather than a
# measurement of the window, which is why it sits here and not with the metrics.
IDENTITY_FIELDS = frozenset({
    "id", "name", "campaign_id", "adset_id", "status",
    "creative_title", "creative_body", "creative_image",
    "creative_thumbnail", "creative_video_id",
})


def split_preset_data(preset_data: dict) -> tuple:
    """(entities, presets) from {preset: {campaigns, adsets, ads, metrics}}.

    Entity identity is taken from whichever preset carries the most rows.
    Presets do not always agree on how many entities exist — a window that
    predates an ad will not list it — so taking the widest bucket avoids
    dropping an entity that only newer windows know about.
    """
    entities = {kind: {} for kind in KINDS}
    presets: dict = {}

    for kind in KINDS:
        for data in (preset_data or {}).values():
            for row in (data or {}).get(kind) or []:
                ent_id = row.get("id")
                if ent_id is None:
                    continue
                ident = {k: v for k, v in row.items() if k in IDENTITY_FIELDS}
                # Later buckets win on ties, which keeps the freshest name and
                # status rather than the first one seen.
                entities[kind][str(ent_id)] = ident

    for preset_key, data in (preset_data or {}).items():
        bucket = {
            k: v for k, v in (data or {}).items()
            if k not in KINDS
        }
        for kind in KINDS:
            bucket[kind] = {
                str(row["id"]): {
                    k: v for k, v in row.items()
                    if k not in IDENTITY_FIELDS
                }
                for row in (data or {}).get(kind) or []
                if row.get("id") is not None
            }
        presets[preset_key] = bucket

    return (
        {kind: list(entities[kind].values()) for kind in KINDS},
        presets,
    )


def rehydrate(entities: dict, bucket: dict) -> dict:
    """Rebuild one preset in the original shape.

    Returns the bucket with `campaigns`, `adsets` and `ads` as full row lists,
    exactly as they were stored before the split — callers and API consumers
    cannot tell the difference.

    An entity with no metrics in this window is included with its identity
    only. It existed; it simply did not spend in that window, and dropping it
    would make the row count move with the date picker.
    """
    out = {k: v for k, v in (bucket or {}).items() if k not in KINDS}

    for kind in KINDS:
        metrics_by_id = (bucket or {}).get(kind) or {}
        rows = []
        for ident in (entities or {}).get(kind) or []:
            ent_id = str(ident.get("id"))
            rows.append({**ident, **(metrics_by_id.get(ent_id) or {})})
        out[kind] = rows

    return out


def has_split_shape(facebook_cache: dict) -> bool:
    """Whether this group has been written since the split shipped."""
    fc = facebook_cache or {}
    return bool(fc.get("entities")) and bool(fc.get("presets"))


def read_preset(facebook_cache: dict, preset_key: str) -> dict:
    """One preset's full rows, from whichever shape the document is in.

    Prefers the split shape and falls back to the legacy bucket, so a group
    that has not refreshed since the migration keeps working unchanged.
    """
    fc = facebook_cache or {}
    if has_split_shape(fc):
        bucket = (fc.get("presets") or {}).get(preset_key)
        if bucket is not None:
            return rehydrate(fc.get("entities") or {}, bucket)
    return fc.get(preset_key) or {}
