"""
tests/test_onboarding_pairing.py
--------------------------------
Pairing GHL sub-accounts to Meta ad accounts on the onboarding review screen.

What is pinned here is the one-to-one property. Scoring each sub-account
against the full ad-account list independently — which is what this did — will
happily hand "Aura — Primary" to both "Aura Aesthetics" and "Aura Body",
because it genuinely is the best fuzzy match for each of them. Every duplicate
but one is wrong by construction, and the wrong ones are invisible: the row
looks answered, so nobody reads it, and a client gets imported reporting
another client's spend.

Also pinned: the gap between having an opinion and filling the box in. A
pre-filled wrong answer costs more than an empty dropdown, so a near-miss is
offered as a suggestion and never pre-selected.

The 30-day lead count is counted client-side over one newest-first page. The
test that matters there is the cap — an agency with more new leads than the
page holds must read as "lots", never as exactly the page size.
"""

from datetime import datetime, timedelta

from routers.onboarding import (
    MATCH_CONFIDENT,
    MATCH_FLOOR,
    RECENT_PAGE_LIMIT,
    _assign_fb_matches,
    _resolve_pairings,
    _score_fb_match,
    _match_word_counts,
)


def _loc(location_id, name):
    return {"location_id": location_id, "name": name}


def _acc(account_id, name):
    return {"id": account_id, "name": name, "currency": "GBP"}


def _score(location_name, account_name, others=()):
    accounts = [_acc("act_x", account_name), *others]
    return _score_fb_match(location_name, _acc("act_x", account_name), _match_word_counts(accounts))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_containment_scores_confident():
    """"Aura" inside "Aura — Primary" is the everyday case, and fuzzy ratio
    alone underrates it badly enough to leave it unfilled."""
    assert _score("Aura", "Aura — Primary") >= MATCH_CONFIDENT


def test_exact_name_scores_confident():
    assert _score("Bright Smile Dental", "Bright Smile Dental") >= MATCH_CONFIDENT


def test_shared_distinctive_word_is_a_suggestion_not_a_match():
    """One shared word is worth raising and not worth acting on by itself."""
    score = _score("Aura Aesthetics", "Aura Body Clinic")
    assert MATCH_FLOOR <= score < MATCH_CONFIDENT


def test_unrelated_names_score_below_the_floor():
    assert _score("Bright Smile Dental", "Northside Roofing") < MATCH_FLOOR


def test_shared_word_stops_counting_when_two_accounts_carry_it():
    """Two ad accounts leading with the same word is ambiguity, not a match —
    so the word bonus is withheld and the pair falls back to raw similarity."""
    with_rival = _score("Aura Aesthetics", "Aura Body", others=[_acc("act_y", "Aura Skin")])
    assert with_rival < MATCH_FLOOR


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

def test_one_ad_account_is_never_given_to_two_subaccounts():
    """The regression this file exists for."""
    locations = [_loc("loc_1", "Aura Aesthetics"), _loc("loc_2", "Aura Body")]
    accounts = [_acc("act_1", "Aura — Primary")]

    matches = _assign_fb_matches(locations, accounts)

    assigned = [m["account"]["id"] for m in matches.values()]
    assert len(assigned) == len(set(assigned))
    assert len(matches) <= 1


def test_the_stronger_pair_wins_a_contested_ad_account():
    """Settling the whole grid at once means the best pair is settled first,
    rather than whichever sub-account the loop happened to reach."""
    locations = [_loc("loc_weak", "Aura Body"), _loc("loc_strong", "Aura Aesthetics")]
    accounts = [_acc("act_1", "Aura Aesthetics")]

    matches = _assign_fb_matches(locations, accounts)

    assert "loc_strong" in matches
    assert matches["loc_strong"]["account"]["id"] == "act_1"


def test_each_subaccount_keeps_its_own_clear_match():
    locations = [_loc("loc_1", "Bright Smile Dental"), _loc("loc_2", "Northside Roofing")]
    accounts = [_acc("act_1", "Northside Roofing"), _acc("act_2", "Bright Smile Dental")]

    matches = _assign_fb_matches(locations, accounts)

    assert matches["loc_1"]["account"]["id"] == "act_2"
    assert matches["loc_2"]["account"]["id"] == "act_1"


def test_a_live_subaccount_wins_a_tie_against_a_dormant_one():
    """Same name, same score, one ad account. The dormant client is the one
    nobody is waiting on, and it keeps the dropdown either way."""
    locations = [_loc("loc_dormant", "Aura Clinic"), _loc("loc_live", "Aura Clinic")]
    accounts = [_acc("act_1", "Aura Clinic")]

    matches = _assign_fb_matches(locations, accounts, active_location_ids={"loc_live"})

    assert "loc_live" in matches
    assert "loc_dormant" not in matches


def test_activity_never_outranks_a_materially_better_name():
    """The tie-break is a tie-break. A dormant exact match still beats a live
    near-miss, because the name is the evidence and activity is not."""
    locations = [_loc("loc_dormant", "Aura Clinic"), _loc("loc_live", "Aura Body")]
    accounts = [_acc("act_1", "Aura Clinic")]

    matches = _assign_fb_matches(locations, accounts, active_location_ids={"loc_live"})

    assert matches["loc_dormant"]["account"]["id"] == "act_1"


def test_near_miss_is_flagged_unconfident():
    locations = [_loc("loc_1", "Aura Aesthetics")]
    accounts = [_acc("act_1", "Aura Body Clinic")]

    matches = _assign_fb_matches(locations, accounts)

    assert matches["loc_1"]["confident"] is False


def test_pairs_below_the_floor_are_not_matched_at_all():
    locations = [_loc("loc_1", "Bright Smile Dental")]
    accounts = [_acc("act_1", "Northside Roofing")]

    assert _assign_fb_matches(locations, accounts) == {}


def test_empty_sides_are_handled():
    assert _assign_fb_matches([], [_acc("act_1", "Aura")]) == {}
    assert _assign_fb_matches([_loc("loc_1", "Aura")], []) == {}


def test_assignment_is_stable_across_repeated_calls():
    """The wizard polls this endpoint while the prep job runs. A table that
    reshuffles between two polls of identical data reads as a bug."""
    locations = [_loc("loc_1", "Aura Clinic"), _loc("loc_2", "Aura Clinic")]
    accounts = [_acc("act_1", "Aura Clinic")]

    first = _assign_fb_matches(locations, accounts)
    second = _assign_fb_matches(list(reversed(locations)), accounts)

    assert list(first) == list(second)


# ---------------------------------------------------------------------------
# 30-day lead count
# ---------------------------------------------------------------------------

class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, *_args, **_kwargs):
        return _FakeResponse(self._payload)


def _contacts(count, days_ago):
    stamp = (datetime.utcnow() - timedelta(days=days_ago)).isoformat() + "Z"
    return [{"dateAdded": stamp} for _ in range(count)]


async def _probe(monkeypatch, contacts, total=None):
    from routers import onboarding

    payload = {"contacts": contacts, "total": total if total is not None else len(contacts)}
    monkeypatch.setattr(onboarding.httpx, "AsyncClient", lambda **_: _FakeClient(payload))
    return await onboarding._latest_contact("loc_1", "token")


async def test_counts_only_leads_inside_the_window(monkeypatch):
    contacts = _contacts(3, days_ago=5) + _contacts(4, days_ago=120)
    result = await _probe(monkeypatch, contacts)

    assert result["leads_30d"] == 3
    assert result["leads_30d_capped"] is False
    assert result["contact_count"] == 7


async def test_a_busy_account_reports_a_capped_count(monkeypatch):
    """More new leads than the probe reads. Reporting the page size as if it
    were the real number would understate a busy client's activity as an
    oddly round figure."""
    result = await _probe(monkeypatch, _contacts(RECENT_PAGE_LIMIT, days_ago=2), total=900)

    assert result["leads_30d"] == RECENT_PAGE_LIMIT
    assert result["leads_30d_capped"] is True


async def test_a_dormant_account_reports_zero_not_unknown(monkeypatch):
    result = await _probe(monkeypatch, _contacts(5, days_ago=200))

    assert result["leads_30d"] == 0
    assert result["last_lead_at"] is not None


async def test_a_failed_probe_degrades_to_no_activity_known(monkeypatch):
    from routers import onboarding

    def _boom(**_):
        raise RuntimeError("network down")

    monkeypatch.setattr(onboarding.httpx, "AsyncClient", _boom)
    result = await onboarding._latest_contact("loc_1", "token")

    assert result["last_lead_at"] is None
    assert result["leads_30d"] == 0


# ---------------------------------------------------------------------------
# Folding similarity and the AI pass together
# ---------------------------------------------------------------------------

def _similarity(location_id, account, score):
    return {location_id: {"account": account, "score": score, "confident": score >= MATCH_CONFIDENT}}


def test_the_ai_pass_cannot_reuse_an_ad_account_similarity_already_took():
    """The two passes are computed at different moments against different free
    lists, so the model can return an ad account that similarity has since
    placed. Both writing it would put one ad account on two rows."""
    account = _acc("act_1", "Aura Clinic")
    matches = _similarity("loc_sure", account, 0.95)

    pairings = _resolve_pairings(matches, {"loc_guess": "act_1"}, {"act_1": account})

    assert pairings["loc_sure"][0]["id"] == "act_1"
    assert "loc_guess" not in pairings


def test_confident_similarity_outranks_the_ai_pass_for_one_subaccount():
    strong = _acc("act_1", "Aura Clinic")
    other = _acc("act_2", "Something Else")
    matches = _similarity("loc_1", strong, 0.95)

    pairings = _resolve_pairings(
        matches, {"loc_1": "act_2"}, {"act_1": strong, "act_2": other}
    )

    assert pairings["loc_1"][0]["id"] == "act_1"


def test_the_ai_pass_fills_a_row_similarity_was_unsure_of():
    """A near-miss is not an answer, so the model's is allowed to become one."""
    near = _acc("act_1", "Aura Body")
    ai = _acc("act_2", "Aura Clinic Ltd")
    matches = _similarity("loc_1", near, 0.7)

    pairings = _resolve_pairings(matches, {"loc_1": "act_2"}, {"act_1": near, "act_2": ai})

    fb_match, suggestion = pairings["loc_1"]
    assert fb_match["id"] == "act_2"
    assert suggestion is None


def test_a_near_miss_is_returned_as_a_suggestion_not_a_match():
    near = _acc("act_1", "Aura Body")
    matches = _similarity("loc_1", near, 0.7)

    fb_match, suggestion = _resolve_pairings(matches, {}, {"act_1": near})["loc_1"]

    assert fb_match is None
    assert suggestion["id"] == "act_1"
    assert suggestion["score"] == 0.7


def test_a_suggestion_is_withheld_once_its_ad_account_is_spoken_for():
    """Offering it would invite the user to create the duplicate by hand."""
    account = _acc("act_1", "Aura Clinic")
    matches = {
        **_similarity("loc_sure", account, 0.95),
        **_similarity("loc_maybe", account, 0.7),
    }

    pairings = _resolve_pairings(matches, {}, {"act_1": account})

    assert pairings["loc_sure"][0]["id"] == "act_1"
    assert pairings["loc_maybe"] == (None, None)


def test_an_ai_match_for_an_ad_account_that_no_longer_exists_is_dropped():
    assert _resolve_pairings({}, {"loc_1": "act_gone"}, {}) == {}


def test_no_ad_account_appears_on_two_rows():
    """The property, stated directly."""
    a1, a2 = _acc("act_1", "Aura Clinic"), _acc("act_2", "Plush Beauty")
    matches = {
        **_similarity("loc_1", a1, 0.95),
        **_similarity("loc_2", a1, 0.72),
        **_similarity("loc_3", a2, 0.88),
    }

    pairings = _resolve_pairings(matches, {"loc_4": "act_2"}, {"act_1": a1, "act_2": a2})

    chosen = [m["id"] for m, _ in pairings.values() if m]
    offered = [s["id"] for _, s in pairings.values() if s]
    assert len(chosen) == len(set(chosen))
    assert not set(chosen) & set(offered)
