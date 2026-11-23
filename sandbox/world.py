"""
sandbox/world.py
----------------
One generated data set per sandbox account, shared by the three fakes. Pure and deterministic:
everything comes from the account id (the seed) and the calendar, nothing is stored, asking twice
gives the same answer, history never changes and "today" fills in as the day passes (Europe/London).

    w = world_for("tester@sandbox.birdy.test")          # now = the current time in London
    c = w.clients[0]                                    # 8 clients
    w.metrics(c, w.today - timedelta(days=6), w.today)  # DayMetrics total for the last 7 days
    w.leads(c, since, until)                            # the people behind those leads

See sandbox/README.md for the whole API. Provider-neutral: no Graph/LeadConnector/HP shapes here,
the fakes turn these objects into their provider's JSON.

How it is built
- Campaigns and ad sets are fixed from EPOCH. Ads rotate: each ad *slot* in an ad set runs a
  sequence of ad *generations* (21-70 days each); an ended generation is a PAUSED ad, the current
  one is ACTIVE. So the account always shows ~120 days of delivery, old ads paused along the way,
  and every date is absolute, so a past day never changes.
- Per ad and day: spend follows the ad set's daily budget, impressions/clicks follow from CPM/CTR,
  and the lead count is a number of *people*, each with a time of day. Insights count exactly the
  people whose time falls while the ad was delivering (and, today, before now).
- Writes (sandbox/state.py) are applied as Overrides: status, name and budget changes take effect
  from the moment they were made, in statuses and in metrics.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional, Union
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")
TIMEZONE = "Europe/London"
CURRENCY = "GBP"
HISTORY_DAYS = 120          # days of data up to and including today
EPOCH = date(2025, 6, 1)    # campaigns and ad sets exist from here; ads rotate from here on
MAX_LEADS_PER_AD_DAY = 10
NO_LEADS_DAYS = 4           # the "no_leads" client gets no leads for this many days, today included

ACTIVE, PAUSED = "ACTIVE", "PAUSED"
STAGES = ("New", "Contacted", "Booked", "Won", "Lost")
HEALTH_PLAN = ("healthy", "healthy", "middling", "middling", "middling", "over_cpl", "over_cpl", "no_leads")

# ---------------------------------------------------------------------------
# Deterministic numbers
# ---------------------------------------------------------------------------
_M64 = (1 << 64) - 1


def _h(*parts) -> int:
    """64-bit hash of anything printable (structure: called a few hundred times per world)."""
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")


def _mix(x: int) -> int:
    """splitmix64 finaliser (hot paths: per ad-day and per person)."""
    x = (x + 0x9E3779B97F4A7C15) & _M64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _M64
    return x ^ (x >> 31)


def _u(seed: int, *ints: int) -> float:
    """Uniform [0, 1) from a seed and some integers."""
    x = seed
    for i in ints:
        x = _mix(x ^ (i & _M64))
    return (_mix(x) >> 11) / float(1 << 53)


def _pick(seq, seed: int, *ints: int):
    return seq[int(_u(seed, *ints) * len(seq))]


def _between(lo: float, hi: float, seed: int, *ints: int) -> float:
    return lo + (hi - lo) * _u(seed, *ints)


_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _b62(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return "".join(reversed(out))


def _unb62(s: str) -> Optional[int]:
    n = 0
    for ch in s:
        i = _B62.find(ch)
        if i < 0:
            return None
        n = n * 62 + i
    return n


def _ghl_id(*parts) -> str:
    """A 20-character id in GoHighLevel's style."""
    return _b62(_h("ghl", *parts), 11) + _b62(_h("ghl2", *parts), 9)


def _digits(n: int, *parts) -> str:
    return str(_h("digits", *parts) % (10 ** n)).rjust(n, "0")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DayMetrics:
    """Delivery for one day (``date``) or a total over a range (``date`` = the first day)."""
    date: date
    spend: float = 0.0          # GBP, 2 decimals
    impressions: int = 0
    reach: int = 0              # summed across ads and days (not deduplicated)
    clicks: int = 0
    link_clicks: int = 0
    leads: int = 0              # == number of Lead people in the same scope
    days: int = 1

    def __add__(self, other: "DayMetrics") -> "DayMetrics":
        return DayMetrics(min(self.date, other.date), round(self.spend + other.spend, 2),
                          self.impressions + other.impressions, self.reach + other.reach,
                          self.clicks + other.clicks, self.link_clicks + other.link_clicks,
                          self.leads + other.leads, self.days)

    @property
    def ctr(self) -> float:
        return round(100.0 * self.clicks / self.impressions, 4) if self.impressions else 0.0

    @property
    def cpm(self) -> float:
        return round(1000.0 * self.spend / self.impressions, 4) if self.impressions else 0.0

    @property
    def cpc(self) -> float:
        return round(self.spend / self.clicks, 4) if self.clicks else 0.0

    @property
    def cpl(self) -> Optional[float]:
        return round(self.spend / self.leads, 2) if self.leads else None

    @property
    def frequency(self) -> float:
        return round(self.impressions / self.reach, 4) if self.reach else 0.0

    def meta_actions(self) -> list[dict]:
        """The ``actions`` list Meta insights carry (strings, as Meta sends them)."""
        out = []
        if self.link_clicks:
            out.append({"action_type": "link_click", "value": str(self.link_clicks)})
        if self.leads:
            for t in ("lead", "onsite_conversion.lead_grouped", "leadgen_grouped"):
                out.append({"action_type": t, "value": str(self.leads)})
        return out


@dataclass(frozen=True)
class Creative:
    id: str
    kind: str               # "image" | "video"
    title: str
    body: str
    call_to_action: str
    image_url: str          # placeholder media (see World.media_url)
    thumbnail_url: str
    video_id: Optional[str] = None


@dataclass(frozen=True)
class Ad:
    id: str
    name: str
    status: str                     # ACTIVE | PAUSED (overrides applied)
    created_on: date
    paused_on: Optional[date]       # the generation's end (None while it runs)
    adset_id: str
    campaign_id: str
    account_id: str                 # "act_..."
    client_index: int
    slot: int                       # 0..59, unique per client; one live generation per slot
    generation: int
    creative: Creative
    weight: float                   # share of the ad set's budget, relative to siblings
    quality: float                  # 0.7..1.3, moves CTR and CPL


@dataclass(frozen=True)
class AdSet:
    id: str
    name: str
    status: str
    campaign_id: str
    account_id: str
    client_index: int
    created_on: date
    daily_budget: int               # minor units (pence), overrides applied
    optimization_goal: str
    billing_event: str
    audience: str
    ads: tuple = ()


@dataclass(frozen=True)
class Campaign:
    id: str
    name: str
    status: str
    objective: str
    account_id: str
    client_index: int
    created_on: date
    adsets: tuple = ()
    daily_budget: Optional[int] = None   # budgets live on ad sets


@dataclass(frozen=True)
class Stage:
    id: str
    name: str
    position: int


@dataclass(frozen=True)
class Pipeline:
    id: str
    name: str
    stages: tuple

    def stage(self, name: str) -> Stage:
        return next(s for s in self.stages if s.name == name)


@dataclass(frozen=True)
class Member:
    id: str                 # numeric string
    first_name: str
    last_name: str
    email: str
    phone: str
    role: str               # "agent" | "manager"
    group_id: str
    client_index: int

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class Client:
    index: int
    key: str                    # slug
    name: str
    vertical: str               # "aesthetics" | "dental" | "gym"
    city: str
    health: str                 # "healthy" | "middling" | "over_cpl" | "no_leads"
    target_cpl: float           # GBP
    deal_value: float           # GBP, typical won deal
    currency: str
    timezone: str
    email: str
    phone: str
    website: str
    address: str
    postcode: str
    ghl_location_id: str
    meta_ad_account_id: str     # "act_<digits>"
    meta_page_id: str
    lead_form_id: str
    hp_group_id: Optional[str]  # set for the 5 call-centre clients
    pipeline: Pipeline
    members: tuple              # Member, empty for non call-centre clients
    campaigns: tuple = ()

    @property
    def call_centre(self) -> bool:
        return self.hp_group_id is not None

    @property
    def meta_account_number(self) -> str:
        return self.meta_ad_account_id[4:]


@dataclass(frozen=True)
class Lead:
    """One person from a Meta lead form. The same person is the GHL contact, the opportunity and,
    for call-centre clients, the HP lead (see World.contact_for / opportunity_for / calls_for)."""
    id: str                     # Meta lead id (numeric string); decodable by World.lead
    client_index: int
    ad_id: str
    adset_id: str
    campaign_id: str
    account_id: str
    form_id: str
    created_time: datetime      # aware, Europe/London
    first_name: str
    last_name: str
    email: str
    phone: str                  # E.164
    answers: dict               # form answers, question -> answer
    platform: str               # "fb" | "ig"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class Contact:
    id: str                     # 20-char GHL id
    lead_id: str
    location_id: str
    client_index: int
    first_name: str
    last_name: str
    email: str
    phone: str
    date_added: datetime
    date_updated: datetime
    source: str
    tags: tuple
    ad_id: str
    adset_id: str
    campaign_id: str
    form_id: str
    hp_lead_id: Optional[str]   # the same person in Hot Prospector (call-centre clients)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class Opportunity:
    id: str
    contact_id: str
    lead_id: str
    location_id: str
    client_index: int
    name: str
    pipeline_id: str
    stage: Stage
    status: str                 # "open" | "won" | "lost"
    monetary_value: float       # GBP; set when won, else 0
    created_at: datetime
    updated_at: datetime
    last_status_change_at: datetime
    last_stage_change_at: datetime
    history: tuple              # ((stage name, at), ...) up to now


@dataclass(frozen=True)
class Call:
    id: str
    lead_id: str                # Meta lead id
    hp_lead_id: str
    contact_id: str
    client_index: int
    group_id: str
    member_id: str
    member_name: str
    direction: str              # "outbound" | "inbound"
    started_at: datetime
    duration: int               # seconds (0: not connected)
    disposition: str
    recording_url: Optional[str]
    from_number: str
    to_number: str

    @property
    def answered(self) -> bool:
        return self.duration >= 45


@dataclass(frozen=True)
class Agency:
    name: str
    ghl_company_id: str
    ghl_user_id: str
    meta_user_id: str
    meta_user_name: str
    meta_business_id: str
    hp_account_name: str


@dataclass(frozen=True)
class Overrides:
    """Remembered writes, applied on top of the generated world (built by sandbox.state.overrides).
    ``status`` and ``budget`` keep every change, oldest first, so a pause and a later re-enable both
    stay in history; ``name`` keeps the latest. ``at`` is an aware datetime."""
    status: dict = field(default_factory=dict)       # object id -> ((value, at), ...) "ACTIVE"|"PAUSED"|...
    budget: dict = field(default_factory=dict)       # ad set id -> ((daily budget in pence, at), ...)
    name: dict = field(default_factory=dict)         # object id -> (new name, at)

    def __bool__(self) -> bool:
        return bool(self.status or self.budget or self.name)

    @staticmethod
    def value_at(events, t: datetime):
        """The value in effect at ``t`` (the last change at or before it), or None."""
        value = None
        for v, at in events or ():
            if at <= t:
                value = v
            else:
                break
        return value

    @staticmethod
    def latest(events):
        return events[-1][0] if events else None


NO_OVERRIDES = Overrides()

# ---------------------------------------------------------------------------
# Names and copy (fictional)
# ---------------------------------------------------------------------------
_CLIENT_POOL = {
    "aesthetics": ("Harbour Lane Aesthetics", "The Linden Skin Rooms", "Velvet Birch Aesthetics",
                   "Marlowe House Skin Studio", "Quillfield Aesthetics"),
    "dental": ("Kestrel Dental Studio", "Fernhill Smile Clinic", "Copperbeech Dental",
               "Northgate Orthodontic Rooms", "Brambleway Dental Care"),
    "gym": ("Ironworks Strength Club", "Tidewater Fitness Lab", "Foundry Fit Collective",
            "Ashgrove Performance Gym", "Lanterns Boxing & Fitness"),
}
_VERTICAL_PLAN = ("aesthetics", "aesthetics", "aesthetics", "dental", "dental", "dental", "gym", "gym")
_CITIES = ("Leeds", "Bristol", "Manchester", "Brighton", "Nottingham", "Sheffield", "Cardiff",
           "Edinburgh", "York", "Bath", "Norwich", "Exeter", "Leicester", "Glasgow", "Reading")
_STREETS = ("Mill Lane", "Chapel Street", "Victoria Road", "Station Approach", "Queen's Walk",
            "Albion Place", "Kingsway", "Market Row", "Orchard Close", "Wellington Terrace")
_POSTCODE = {"Leeds": "LS1", "Bristol": "BS1", "Manchester": "M1", "Brighton": "BN1", "Nottingham": "NG1",
             "Sheffield": "S1", "Cardiff": "CF10", "Edinburgh": "EH1", "York": "YO1", "Bath": "BA1",
             "Norwich": "NR1", "Exeter": "EX1", "Leicester": "LE1", "Glasgow": "G1", "Reading": "RG1"}
_VERTICAL = {
    #            target CPL      deal value       CPM        CTR%   offers
    "aesthetics": ((24, 34), (350, 1200), (8.0, 12.0), 1.3,
                   ("Lip Filler Offer", "Anti-Wrinkle Consult", "Skin Booster Launch", "Profhilo Season",
                    "Laser Hair Removal")),
    "dental": ((30, 45), (1500, 4500), (9.0, 14.0), 1.0,
               ("Invisalign Consult", "Implant Open Evening", "Whitening £199", "New Patient Exam",
                "Composite Bonding")),
    "gym": ((8, 15), (300, 650), (6.0, 9.0), 1.6,
            ("6-Week Challenge", "Free Trial Week", "PT Starter Pack", "Transformation Programme",
             "Boxing Fundamentals")),
}
_AUDIENCES = ("Broad 25-55", "Lookalike 1% Leads", "Interest Stack", "Retargeting 30d", "Radius 10mi",
              "Lookalike 3% Purchasers", "Advantage+ Audience")
_ANGLES = ("Testimonial", "Before & After", "Offer Card", "Founder Story", "Walkthrough", "FAQ",
           "Limited Spots", "UGC Review", "Price Anchor", "Results Reel")
_CTAS = ("LEARN_MORE", "BOOK_TRAVEL", "SIGN_UP", "GET_QUOTE", "CONTACT_US")
_FIRST = ("Olivia", "Amelia", "Isla", "Ava", "Mia", "Ivy", "Lily", "Freya", "Grace", "Sophie", "Emily",
          "Ella", "Chloe", "Poppy", "Evie", "Harriet", "Zara", "Aisha", "Priya", "Nadia", "Oliver",
          "George", "Harry", "Noah", "Jack", "Leo", "Arthur", "Oscar", "Charlie", "James", "Thomas",
          "Callum", "Ryan", "Daniel", "Samir", "Kofi", "Ewan", "Rhys", "Owen", "Finley")
_LAST = ("Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies", "Patel",
         "Robinson", "Wright", "Thompson", "Evans", "Walker", "White", "Roberts", "Green", "Hall",
         "Thomas", "Clarke", "Jackson", "Wood", "Hughes", "Harris", "Edwards", "Turner", "Martin",
         "Cooper", "Hill", "Ward", "Morris", "Moore", "Khan", "Ali", "Begum", "Murphy", "Kelly",
         "Campbell", "Stewart", "Reid")
_QUESTIONS = {
    "aesthetics": (("which_treatment_are_you_interested_in?", ("Lip filler", "Anti-wrinkle", "Skin boosters", "Not sure yet")),
                   ("when_would_you_like_to_book?", ("This week", "Within a month", "Just browsing"))),
    "dental": (("what_treatment_are_you_looking_for?", ("Invisalign", "Implants", "Whitening", "Check-up")),
               ("are_you_an_existing_patient?", ("No", "Yes"))),
    "gym": (("what_is_your_main_goal?", ("Lose weight", "Build strength", "Get fit", "Train for an event")),
            ("how_soon_can_you_start?", ("Straight away", "Next month", "Not sure"))),
}
_AGENCY_NAMES = ("Northlight Growth", "Brightwater Media", "Signal & Spark", "Halyard Digital",
                 "Meadowlark Marketing", "Copperline Ads")


def media_url(user_id: str, name: str) -> str:
    """A placeholder media URL. It sits on a provider host with an ``sbx_`` path segment, so if the
    product downloads it the hook answers in-process; a browser cannot load it (open question for
    phase 2: serve placeholders somewhere public)."""
    from sandbox.credentials import make_credential

    return f"https://scontent.xx.fbcdn.net/sandbox/{make_credential(user_id, 'media', nonce=name)}/{name}"


# ---------------------------------------------------------------------------
# Structure (independent of the date: cached per user)
# ---------------------------------------------------------------------------
@dataclass
class _SlotPlan:
    """One ad slot: its generations, computed lazily as far as needed."""
    client_index: int
    slot: int
    adset_id: str
    campaign_id: str
    seed: int
    starts: list = field(default_factory=list)   # generation start dates

    def generations_until(self, until: date) -> list[tuple[int, date, date]]:
        """(generation, start, end) for every generation starting on or before ``until``."""
        if not self.starts:
            self.starts.append(EPOCH + timedelta(days=int(_u(self.seed, 1) * 25)))
        while self.starts[-1] <= until:
            g = len(self.starts) - 1
            self.starts.append(self.starts[-1] + timedelta(days=21 + int(_u(self.seed, 2, g) * 50)))
        out = []
        for g in range(len(self.starts) - 1):
            if self.starts[g] > until:
                break
            out.append((g, self.starts[g], self.starts[g + 1]))
        return out


@dataclass
class _Structure:
    user_id: str
    seed: int
    agency: Agency
    clients: list           # Client without campaigns
    campaigns: dict         # client index -> list[Campaign] (adsets without ads)
    slots: dict             # adset id -> list[_SlotPlan]
    lock: threading.Lock = field(default_factory=threading.Lock)


def _build_structure(user_id: str) -> _Structure:
    seed = _h("sandbox-world", user_id.strip().lower())
    agency = Agency(
        name=f"{_pick(_AGENCY_NAMES, seed, 1)} (Sandbox)",
        ghl_company_id=_ghl_id(seed, "company"),
        ghl_user_id=_ghl_id(seed, "ghl-user"),
        meta_user_id="10" + _digits(14, seed, "meta-user"),
        meta_user_name="Sandbox Tester",
        meta_business_id="9" + _digits(14, seed, "business"),
        hp_account_name=f"{_pick(_AGENCY_NAMES, seed, 1)} Call Centre",
    )
    verticals = sorted(range(8), key=lambda i: _h(seed, "vorder", i))
    healths = sorted(range(8), key=lambda i: _h(seed, "horder", i))
    cities = sorted(_CITIES, key=lambda c: _h(seed, "city", c))
    used_names: dict = {}
    clients, campaigns, slots = [], {}, {}
    # Call centres: the 5 clients with the most expensive leads to chase (dental, aesthetics first).
    for ci in range(8):
        vertical = _VERTICAL_PLAN[verticals[ci]]
        health = HEALTH_PLAN[healths[ci]]
        pool = sorted(_CLIENT_POOL[vertical], key=lambda n: _h(seed, "name", n))
        name = pool[used_names.get(vertical, 0)]
        used_names[vertical] = used_names.get(vertical, 0) + 1
        city = cities[ci]
        (cpl_lo, cpl_hi), (deal_lo, deal_hi), _cpm, _ctr, offers = _VERTICAL[vertical]
        key = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-").replace("--", "-")
        location_id = _ghl_id(seed, "location", ci)
        pipeline = Pipeline(id=_ghl_id(seed, "pipeline", ci), name="Lead Pipeline", stages=tuple(
            Stage(id=f"{_ghl_id(seed, 'stage', ci, s)[:8].lower()}-{_digits(4, seed, ci, s)}-4{_digits(3, seed, 's', ci, s)}",
                  name=s, position=i) for i, s in enumerate(STAGES)))
        clients.append(dict(
            index=ci, key=key, name=f"{name} {city}", vertical=vertical, city=city, health=health,
            target_cpl=float(round(_between(cpl_lo, cpl_hi, seed, 10, ci))),
            deal_value=float(round(_between(deal_lo, deal_hi, seed, 11, ci), -1)),
            currency=CURRENCY, timezone=TIMEZONE,
            email=f"hello@{key}.example.com", phone=f"+44113496{_digits(4, seed, 'cphone', ci)}",
            website=f"https://{key}.example.com",
            address=f"{1 + _h(seed, 'num', ci) % 180} {_pick(_STREETS, seed, 12, ci)}, {city}",
            postcode=f"{_POSTCODE[city]} {1 + _h(seed, 'pc', ci) % 9}{_pick('ABDEFGHJLNPQRSTUWXYZ', seed, 13, ci)}"
                     f"{_pick('ABDEFGHJLNPQRSTUWXYZ', seed, 14, ci)}",
            ghl_location_id=location_id,
            meta_ad_account_id="act_" + "1" + _digits(14, seed, "act", ci),
            meta_page_id="1" + _digits(14, seed, "page", ci),
            lead_form_id="8" + _digits(14, seed, "form", ci),
            pipeline=pipeline,
        ))
    # 5 call centres, chosen per user: the non-gym clients first.
    order = sorted(range(8), key=lambda i: (clients[i]["vertical"] == "gym", _h(seed, "hp", i)))
    hp_clients = set(order[:5])
    for c in clients:
        ci = c["index"]
        if ci in hp_clients:
            group_id = str(40000 + _h(seed, "hpgroup", ci) % 50000)
            n = 3 + _h(seed, "nmembers", ci) % 3
            members = []
            for m in range(n):
                first, last = _pick(_FIRST, seed, 20, ci, m), _pick(_LAST, seed, 21, ci, m)
                members.append(Member(
                    id=str(700000 + _h(seed, "member", ci, m) % 299999), first_name=first, last_name=last,
                    email=f"{first.lower()}.{last.lower()}@{c['key']}.example.com",
                    phone=f"+44113496{_digits(4, seed, 'mphone', ci, m)}",
                    role="manager" if m == 0 else "agent", group_id=group_id, client_index=ci))
            c["hp_group_id"], c["members"] = group_id, tuple(members)
        else:
            c["hp_group_id"], c["members"] = None, ()

    for c in clients:
        ci, vertical, act = c["index"], c["vertical"], c["meta_ad_account_id"]
        offers = sorted(_VERTICAL[vertical][4], key=lambda o: _h(seed, "offer", ci, o))
        n_campaigns = 3 + _h(seed, "ncamp", ci) % 3
        client_campaigns, slot_no = [], 0
        for k in range(n_campaigns):
            cid = "1202" + _digits(14, seed, "campaign", ci, k)
            created = EPOCH + timedelta(days=_h(seed, "ccreated", ci, k) % 20)
            adsets = []
            n_adsets = 1 + _h(seed, "nadset", ci, k) % 3
            audiences = sorted(_AUDIENCES, key=lambda a: _h(seed, "aud", ci, k, a))
            for j in range(n_adsets):
                asid = "1202" + _digits(14, seed, "adset", ci, k, j)
                budget = int(round(_between(15, 60, seed, 30, ci, k, j))) * 100
                # One ad set in a few accounts was switched off before the window: status variety.
                paused = j > 0 and _u(seed, 31, ci, k, j) < 0.12
                adsets.append(AdSet(
                    id=asid, name=f"{audiences[j]} | {c['city']}", status=PAUSED if paused else ACTIVE,
                    campaign_id=cid, account_id=act, client_index=ci, created_on=created,
                    daily_budget=budget, optimization_goal="LEAD_GENERATION", billing_event="IMPRESSIONS",
                    audience=audiences[j]))
                n_slots = 2 + _h(seed, "nslots", ci, k, j) % 3
                slots[asid] = []
                for s in range(n_slots):
                    slots[asid].append(_SlotPlan(ci, slot_no, asid, cid, _h(seed, "slot", ci, slot_no)))
                    slot_no += 1
            client_campaigns.append(Campaign(
                id=cid, name=f"{offers[k % len(offers)]} | Lead Gen | {c['city']}", status=ACTIVE,
                objective="OUTCOME_LEADS", account_id=act, client_index=ci, created_on=created,
                adsets=tuple(adsets)))
        campaigns[ci] = client_campaigns
    return _Structure(user_id=user_id.strip().lower(), seed=seed, agency=agency, clients=clients,
                      campaigns=campaigns, slots=slots)


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------
_CACHE_SIZE = 64
_structures: "OrderedDict[str, _Structure]" = OrderedDict()
_worlds: "OrderedDict[tuple, World]" = OrderedDict()
_cache_lock = threading.Lock()


def _lru_get(cache: OrderedDict, key, build):
    with _cache_lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    value = build()
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _CACHE_SIZE:
            cache.popitem(last=False)
    return value


def clear_cache() -> None:
    with _cache_lock:
        _structures.clear()
        _worlds.clear()


def _london(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(TZ)


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time(0), TZ)


def world_for(user_id: str, now: Optional[datetime] = None, overrides: Optional[Overrides] = None) -> "World":
    """The account's world as of ``now`` (default: the current time; naive means UTC), with
    ``overrides`` (remembered writes, see sandbox.state.overrides) applied. Cached per
    (user, date); a call with a later ``now`` on the same day only moves "today" forward."""
    now = _london(now)
    user_id = (user_id or "").strip().lower()
    structure = _lru_get(_structures, user_id, lambda: _build_structure(user_id))
    base = _lru_get(_worlds, (user_id, now.date()), lambda: World(structure, now))
    world = base if base.now == now else base._at(now)
    return world._with_overrides(overrides) if overrides else world


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------
Scope = Union[Client, Campaign, AdSet, Ad]


class World:
    """An account's generated data as of ``now``. Build it with ``world_for``."""

    def __init__(self, structure: _Structure, now: datetime, overrides: Overrides = NO_OVERRIDES,
                 _clients: Optional[tuple] = None, _base_cache: Optional[dict] = None):
        self._s = structure
        self.user_id = structure.user_id
        self.seed = structure.seed
        self.now = now
        self.today = now.date()
        self.history_start = max(EPOCH, self.today - timedelta(days=HISTORY_DAYS - 1))
        self.agency = structure.agency
        self.overrides = overrides
        self._base = _base_cache if _base_cache is not None else {}   # full past ad-days, shared
        self.clients: tuple = _clients if _clients is not None else self._build_clients()
        self._index()

    # ----- construction -------------------------------------------------
    def _build_clients(self) -> tuple:
        out = []
        for c in self._s.clients:
            ci = c["index"]
            campaigns = []
            for camp in self._s.campaigns[ci]:
                adsets = []
                for adset in camp.adsets:
                    ads = []
                    for plan in self._s.slots[adset.id]:
                        with self._s.lock:
                            gens = plan.generations_until(self.today)
                        for g, start, end in gens:
                            if end <= self.history_start:
                                continue
                            ads.append(self._make_ad(c, camp, adset, plan, g, start, end))
                    adsets.append(replace(adset, ads=tuple(ads)))
                campaigns.append(replace(camp, adsets=tuple(adsets)))
            out.append(Client(campaigns=tuple(campaigns), **c))
        return tuple(out)

    def _make_ad(self, c: dict, camp: Campaign, adset: AdSet, plan: _SlotPlan, g: int, start: date, end: date) -> Ad:
        seed, ci = self.seed, c["index"]
        ad_id = "1202" + _digits(14, seed, "ad", ci, plan.slot, g)
        angle = _pick(_ANGLES, seed, 40, ci, plan.slot, g)
        kind = "video" if _u(seed, 41, ci, plan.slot, g) < 0.45 else "image"
        offer = camp.name.split(" | ")[0]
        creative_id = "1202" + _digits(14, seed, "creative", ci, plan.slot, g)
        image = media_url(self.user_id, f"{creative_id}.jpg")
        creative = Creative(
            id=creative_id, kind=kind, title=f"{offer} at {c['name']}",
            body=f"{angle}: {offer.lower()} in {c['city']}. Limited spaces this month, tap to see if you qualify.",
            call_to_action=_pick(_CTAS, seed, 42, ci, plan.slot, g), image_url=image, thumbnail_url=image,
            video_id=("1202" + _digits(14, seed, "video", ci, plan.slot, g)) if kind == "video" else None)
        live = end > self.today
        return Ad(
            id=ad_id, name=f"{'Video' if kind == 'video' else 'Image'} - {angle} v{g + 1}",
            status=ACTIVE if live else PAUSED, created_on=start, paused_on=None if live else end,
            adset_id=adset.id, campaign_id=camp.id, account_id=camp.account_id, client_index=ci,
            slot=plan.slot, generation=g, creative=creative,
            weight=round(_between(0.6, 1.4, seed, 43, ci, plan.slot, g), 3),
            quality=round(_between(0.7, 1.3, seed, 44, ci, plan.slot, g), 3))

    def _index(self) -> None:
        self._by_id: dict = {}
        self._client_by: dict = {}
        for c in self.clients:
            self._client_by[c.ghl_location_id] = c
            self._client_by[c.meta_ad_account_id] = c
            self._client_by[c.meta_account_number] = c
            self._client_by[c.key] = c
            if c.hp_group_id:
                self._client_by[c.hp_group_id] = c
            for camp in c.campaigns:
                self._by_id[camp.id] = camp
                for adset in camp.adsets:
                    self._by_id[adset.id] = adset
                    for ad in adset.ads:
                        self._by_id[ad.id] = ad

    def _at(self, now: datetime) -> "World":
        w = object.__new__(World)
        w.__dict__.update(self.__dict__)
        w.now, w.today = now, now.date()
        return w

    def _with_overrides(self, ov: Overrides) -> "World":
        def fix(obj):
            changes = {}
            if obj.id in ov.status:
                changes["status"] = Overrides.latest(ov.status[obj.id])
            if obj.id in ov.name:
                changes["name"] = ov.name[obj.id][0]
            if isinstance(obj, AdSet) and obj.id in ov.budget:
                changes["daily_budget"] = int(Overrides.latest(ov.budget[obj.id]))
            return changes

        clients = []
        for c in self.clients:
            camps = []
            for camp in c.campaigns:
                adsets = []
                for adset in camp.adsets:
                    ads = tuple(replace(ad, **fix(ad)) if fix(ad) else ad for ad in adset.ads)
                    adsets.append(replace(adset, ads=ads, **fix(adset)))
                camps.append(replace(camp, adsets=tuple(adsets), **fix(camp)))
            clients.append(replace(c, campaigns=tuple(camps)))
        return World(self._s, self.now, ov, _clients=tuple(clients), _base_cache=self._base)

    # ----- lookups ------------------------------------------------------
    def client(self, ref) -> Optional[Client]:
        """By index, key, GHL location id, Meta ad account id (with or without "act_") or HP group id."""
        if isinstance(ref, int):
            return self.clients[ref] if 0 <= ref < len(self.clients) else None
        return self._client_by.get(str(ref))

    def find(self, object_id: str) -> Optional[Union[Campaign, AdSet, Ad]]:
        """A campaign, ad set or ad by id."""
        return self._by_id.get(str(object_id))

    def client_of(self, obj) -> Client:
        return self.clients[obj.client_index]

    def campaign_of(self, obj: Union[AdSet, Ad]) -> Campaign:
        return self._by_id[obj.campaign_id]

    def adset_of(self, ad: Ad) -> AdSet:
        return self._by_id[ad.adset_id]

    def campaigns(self, client: Client) -> tuple:
        return client.campaigns

    def adsets(self, scope: Union[Client, Campaign]) -> list:
        if isinstance(scope, Campaign):
            return list(scope.adsets)
        return [a for camp in scope.campaigns for a in camp.adsets]

    def ads(self, scope: Optional[Scope] = None) -> list:
        """Every ad under a client / campaign / ad set (an ad: itself); all ads when None."""
        if scope is None:
            return [ad for c in self.clients for ad in self.ads(c)]
        if isinstance(scope, Ad):
            return [scope]
        if isinstance(scope, AdSet):
            return list(scope.ads)
        if isinstance(scope, Campaign):
            return [ad for a in scope.adsets for ad in a.ads]
        return [ad for camp in scope.campaigns for a in camp.adsets for ad in a.ads]

    def effective_status(self, obj: Union[Campaign, AdSet, Ad]) -> str:
        """Meta-style effective status: ACTIVE, PAUSED, ADSET_PAUSED or CAMPAIGN_PAUSED."""
        if isinstance(obj, Campaign):
            return obj.status
        camp = self.campaign_of(obj)
        if camp.status != ACTIVE:
            return "CAMPAIGN_PAUSED"
        if isinstance(obj, Ad):
            if obj.status != ACTIVE:
                return obj.status
            return "ADSET_PAUSED" if self.adset_of(obj).status != ACTIVE else ACTIVE
        return obj.status

    def members(self, client: Optional[Client] = None) -> list:
        """Hot Prospector members (callers): one client's, or every call centre's."""
        if client is not None:
            return list(client.members)
        return [m for c in self.clients for m in c.members]

    def member(self, member_id: str) -> Optional[Member]:
        return next((m for m in self.members() if m.id == str(member_id)), None)

    # ----- days ---------------------------------------------------------
    def days(self, since: date, until: date) -> list:
        """The days in [since, until] that have data: clamped to the history window and today."""
        since, until = max(since, self.history_start), min(until, self.today)
        return [since + timedelta(days=i) for i in range((until - since).days + 1)] if since <= until else []

    def day_bounds(self, day: date) -> tuple:
        """(start, end) of a London calendar day as aware datetimes (23 or 25 hours at a clock change)."""
        return _day_start(day), _day_start(day + timedelta(days=1))

    # ----- delivery -----------------------------------------------------
    def _status_at(self, obj, base_on: Optional[date], base_off: Optional[date], base_status: str, t: datetime) -> bool:
        written = Overrides.value_at(self.overrides.status.get(obj.id), t)
        if written is not None:
            return written == ACTIVE
        if base_on and t < _day_start(base_on):
            return False
        if base_off and t >= _day_start(base_off):
            return False
        return base_status == ACTIVE

    def _active_windows(self, ad: Ad, start: datetime, end: datetime) -> list:
        """Intervals within [start, end) during which the ad, its ad set and campaign all deliver."""
        adset, camp = self.adset_of(ad), self.campaign_of(ad)
        base = self._base_objects(ad, adset, camp)
        cuts = {start, end}
        for obj, on, off, _ in base:
            for d in (on, off):
                if d:
                    t = _day_start(d)
                    if start < t < end:
                        cuts.add(t)
            for _v, at in self.overrides.status.get(obj.id, ()):
                if start < at < end:
                    cuts.add(at)
        points = sorted(cuts)
        out = []
        for a, b in zip(points, points[1:]):
            if all(self._status_at(obj, on, off, st, a) for obj, on, off, st in base):
                if out and out[-1][1] == a:
                    out[-1] = (out[-1][0], b)
                else:
                    out.append((a, b))
        return out

    def _base_objects(self, ad: Ad, adset: AdSet, camp: Campaign) -> list:
        """(object, delivers from, stops on, generated status) without overrides."""
        return [
            (ad, ad.created_on, ad.paused_on, ACTIVE),
            (adset, adset.created_on, None, self._base_status(adset)),
            (camp, camp.created_on, None, self._base_status(camp)),
        ]

    def _base_status(self, obj) -> str:
        if obj.id in self.overrides.status:
            # the generated status, before any write (World objects carry the overridden one)
            return self._generated_status(obj)
        return obj.status

    def _generated_status(self, obj) -> str:
        for camp in self._s.campaigns[obj.client_index]:
            if camp.id == obj.id:
                return camp.status
            for adset in camp.adsets:
                if adset.id == obj.id:
                    return adset.status
        return ACTIVE

    def _budget_ratio(self, adset: AdSet, start: datetime, end: datetime, windows: list) -> float:
        """Active time in the day weighted by budget / generated budget, as a share of the day."""
        total = (end - start).total_seconds()
        events = self.overrides.budget.get(adset.id, ())
        base_budget = self._generated_budget(adset)
        acc = 0.0
        for a, b in windows:
            points = sorted({a, b, *(at for _v, at in events if a < at < b)})
            for x, y in zip(points, points[1:]):
                budget = Overrides.value_at(events, x)
                acc += (y - x).total_seconds() * ((int(budget) / base_budget) if budget is not None else 1.0)
        return acc / total if total else 0.0

    def _generated_budget(self, adset: AdSet) -> int:
        for camp in self._s.campaigns[adset.client_index]:
            for a in camp.adsets:
                if a.id == adset.id:
                    return a.daily_budget
        return adset.daily_budget

    def _base_day(self, ad: Ad, day: date) -> tuple:
        """Full-day delivery at the generated budget: (spend, impressions, reach, clicks, link_clicks,
        cost per lead). Cached: it never changes."""
        key = (ad.id, day.toordinal())
        hit = self._base.get(key)
        if hit is not None:
            return hit
        c = self.clients[ad.client_index]
        seed, o = self.seed ^ _h(ad.id), day.toordinal()
        siblings = self._s.slots[ad.adset_id]
        share = ad.weight / max(1.0, float(len(siblings)))
        weekday = 0.9 if day.weekday() >= 5 else 1.0
        spend = self._generated_budget(self.adset_of(ad)) / 100.0 * share * _between(0.82, 1.02, seed, o, 1) * weekday
        cpm_lo, cpm_hi = _VERTICAL[c.vertical][2]
        impressions = spend / _between(cpm_lo, cpm_hi, seed, o, 2) * 1000.0
        reach = impressions / _between(1.05, 1.4, seed, o, 3)
        clicks = impressions * _VERTICAL[c.vertical][3] / 100.0 * ad.quality * _between(0.8, 1.2, seed, o, 4)
        mult = {"healthy": 0.72, "middling": 1.05, "over_cpl": 1.85, "no_leads": 1.0}[c.health]
        cpl = c.target_cpl * mult / ad.quality * _between(0.75, 1.25, seed, o, 5)
        value = (spend, impressions, reach, clicks, clicks * 0.78, cpl)
        if day < self.today:
            self._base[key] = value
        return value

    def _potential_people(self, ad: Ad, day: date, ratio_full: float) -> list:
        """(k, seconds into the day) for every lead the ad would get that day; stable per (ad, day, k)."""
        c = self.clients[ad.client_index]
        if c.health == "no_leads" and day > self.today - timedelta(days=NO_LEADS_DAYS):
            return []
        spend, *_rest, cpl = self._base_day(ad, day)
        seed, o = self.seed ^ _h(ad.id), day.toordinal()
        n = min(MAX_LEADS_PER_AD_DAY, int(spend * ratio_full / cpl + _u(seed, o, 6)))
        start, end = self.day_bounds(day)
        length = (end - start).total_seconds()
        # Leads come in during the waking day (7:00-23:00 local), never in the last minute.
        return sorted(((k, int(length * (0.29 + 0.66 * _u(seed, o, 100 + k)))) for k in range(n)),
                      key=lambda kt: kt[1])

    def _touched(self, ad: Ad) -> bool:
        """Any remembered write on the ad, its ad set or its campaign."""
        st, bu = self.overrides.status, self.overrides.budget
        return bool(st or bu) and (ad.id in st or ad.adset_id in st or ad.campaign_id in st or ad.adset_id in bu)

    def _ad_day(self, ad: Ad, day: date) -> tuple:
        """(DayMetrics, [(k, created datetime)]) for one ad and day, overrides and the time of day
        applied. Complete days without writes on the ad are cached (they never change)."""
        touched = self._touched(ad)
        if day < ad.created_on or (ad.paused_on and day >= ad.paused_on and not touched):
            return DayMetrics(day), []
        complete = day < self.today
        if not touched:
            if self.adset_of(ad).status != ACTIVE or self.campaign_of(ad).status != ACTIVE:
                return DayMetrics(day), []
            key = ("day", ad.id, day.toordinal())
            if complete:
                hit = self._base.get(key)
                if hit is not None:
                    return hit
        start, end = self.day_bounds(day)
        stop = end if complete else min(end, self.now)
        if stop <= start:
            return DayMetrics(day), []
        if touched:
            windows = self._active_windows(ad, start, stop)
            full_windows = self._active_windows(ad, start, end) if stop != end else windows
            adset = self.adset_of(ad)
            ratio = self._budget_ratio(adset, start, end, windows)
            # The lead count uses the budget in effect at the start of the day (see README).
            at_start = Overrides.value_at(self.overrides.budget.get(adset.id), start)
            ratio_full = (int(at_start) / self._generated_budget(adset)) if at_start is not None else 1.0
            if not full_windows:
                ratio_full = 0.0
        else:
            windows = [(start, stop)]
            ratio = (stop - start).total_seconds() / (end - start).total_seconds()
            ratio_full = 1.0
        spend, imp, reach, clicks, link, _cpl = self._base_day(ad, day)
        people = []
        for k, sec in self._potential_people(ad, day, ratio_full):
            t = start + timedelta(seconds=sec)
            if t < stop and any(a <= t < b for a, b in windows):
                people.append((k, t))
        result = (DayMetrics(day, round(spend * ratio, 2), int(imp * ratio), int(reach * ratio),
                             int(clicks * ratio), int(link * ratio), len(people)), people)
        if complete and not touched:
            self._base[("day", ad.id, day.toordinal())] = result
        return result

    def _people(self, ad: Ad, day: date) -> list:
        return [self._lead(ad, day, k, t) for k, t in self._ad_day(ad, day)[1]]

    def ad_day(self, ad: Ad, day: date) -> Optional[DayMetrics]:
        """One ad's delivery on one day. None for a future day or a day before the history window;
        today is partial (up to ``now``)."""
        if day > self.today or day < self.history_start:
            return None
        return self._ad_day(ad, day)[0]

    def daily(self, scope: Scope, since: date, until: date) -> list:
        """One DayMetrics per day in ``days(since, until)``, summed over the scope's ads
        (a campaign's day is the sum of its ads' days)."""
        ads = self.ads(scope)
        out = []
        for day in self.days(since, until):
            sp = im = re_ = cl = li = le = 0
            for ad in ads:
                m = self._ad_day(ad, day)[0]
                sp += m.spend; im += m.impressions; re_ += m.reach; cl += m.clicks; li += m.link_clicks; le += m.leads
            out.append(DayMetrics(day, round(sp, 2), im, re_, cl, li, le))
        return out

    def metrics(self, scope: Scope, since: date, until: date) -> DayMetrics:
        """The total over the range (``days`` = number of days with data)."""
        rows = self.daily(scope, since, until)
        total = DayMetrics(rows[0].date if rows else since, days=len(rows))
        for r in rows:
            total = total + r
        return replace(total, days=len(rows))

    # ----- people -------------------------------------------------------
    def _phone(self, ad: Ad, day: date, k: int) -> str:
        slot = ((ad.client_index * 60 + ad.slot) * 200 + day.toordinal() % 200) * 10 + k
        return f"+447700{slot:06d}"

    def _lead(self, ad: Ad, day: date, k: int, t: datetime) -> Lead:
        c = self.clients[ad.client_index]
        seed = self.seed ^ _h(ad.id)
        o = day.toordinal()
        first, last = _pick(_FIRST, seed, o, 200 + k), _pick(_LAST, seed, o, 300 + k)
        slot = ((ad.client_index * 60 + ad.slot) * 200 + o % 200) * 10 + k
        answers = {q: _pick(opts, seed, o, 400 + k, i) for i, (q, opts) in enumerate(_QUESTIONS[c.vertical])}
        return Lead(
            id=self._lead_id(ad, day, k), client_index=ad.client_index, ad_id=ad.id, adset_id=ad.adset_id,
            campaign_id=ad.campaign_id, account_id=ad.account_id, form_id=c.lead_form_id, created_time=t,
            first_name=first, last_name=last, email=f"{first}.{last}{slot}@example.com".lower(),
            phone=self._phone(ad, day, k), answers=answers,
            platform="ig" if _u(seed, o, 500 + k) < 0.35 else "fb")

    def _lead_id(self, ad: Ad, day: date, k: int) -> str:
        body = f"{ad.client_index}{ad.slot:02d}{day.toordinal():07d}{k}"
        return "9" + body + _digits(5, self.seed, "lead", body)

    def lead(self, lead_id: str) -> Optional[Lead]:
        """A lead by its id (works for any lead, without listing)."""
        s = str(lead_id)
        if len(s) != 17 or not s.isdigit() or s[0] != "9":
            return None
        body = s[1:12]
        if s[12:] != _digits(5, self.seed, "lead", body):
            return None
        ci, slot, ordinal, k = int(body[0]), int(body[1:3]), int(body[3:10]), int(body[10])
        if ci >= len(self.clients):
            return None
        day = date.fromordinal(ordinal)
        if day > self.today or day < self.history_start:
            return None
        for ad in self.ads(self.clients[ci]):
            if ad.slot == slot and ad.created_on <= day and (ad.paused_on is None or day < ad.paused_on
                                                               or ad.id in self.overrides.status):
                return next((p for p in self._people(ad, day) if p.id == s), None)
        return None

    def ad_leads(self, ad: Ad, day: date) -> list:
        """The people behind ``ad_day(ad, day).leads``, in time order."""
        if day > self.today or day < self.history_start:
            return []
        return self._people(ad, day)

    def leads(self, scope: Scope, since: date, until: date) -> list:
        """Every lead in the scope created on a day in [since, until], in time order."""
        out = []
        ads = self.ads(scope)
        for day in self.days(since, until):
            for ad in ads:
                out.extend(self._people(ad, day))
        out.sort(key=lambda p: (p.created_time, p.id))
        return out

    # ----- GoHighLevel side ---------------------------------------------
    def contact_for(self, lead: Lead) -> Contact:
        """The lead as a GHL contact in its client's location (same name, email, phone, ad ids)."""
        c = self.clients[lead.client_index]
        seed = _h(self.seed, "contact", lead.id)
        added = lead.created_time + timedelta(seconds=30 + seed % 150)
        opp = self.opportunity_for(lead)
        updated = max(added, opp.updated_at) if opp else added
        camp = self.find(lead.campaign_id)
        tags = ["facebook lead", c.vertical, (camp.name.split(" | ")[0].lower() if camp else "lead form")]
        if opp and opp.stage.name in ("Booked", "Won"):
            tags.append("booked")
        return Contact(
            id=self.contact_id(lead.id), lead_id=lead.id, location_id=c.ghl_location_id, client_index=c.index,
            first_name=lead.first_name, last_name=lead.last_name, email=lead.email, phone=lead.phone,
            date_added=added, date_updated=updated, source="Facebook Lead Form", tags=tuple(tags),
            ad_id=lead.ad_id, adset_id=lead.adset_id, campaign_id=lead.campaign_id, form_id=lead.form_id,
            hp_lead_id=self.hp_lead_id(lead.id) if c.call_centre else None)

    def contact_id(self, lead_id: str) -> str:
        return "c" + _b62(int(lead_id), 10) + _b62(_h(self.seed, "cid", lead_id), 9)

    def contact(self, contact_id: str) -> Optional[Contact]:
        lead = self._lead_from_ref(contact_id, "c", "cid")
        return self.contact_for(lead) if lead else None

    def contacts(self, client: Client, since: date, until: date) -> list:
        """GHL contacts whose lead was created in [since, until]."""
        return [self.contact_for(p) for p in self.leads(client, since, until)]

    def _lead_from_ref(self, ref: str, prefix: str, salt: str) -> Optional[Lead]:
        ref = str(ref)
        if len(ref) != 20 or ref[0] != prefix:
            return None
        n = _unb62(ref[1:11])
        if n is None or ref[11:] != _b62(_h(self.seed, salt, str(n)), 9):
            return None
        return self.lead(str(n))

    def opportunity_id(self, lead_id: str) -> str:
        return "o" + _b62(int(lead_id), 10) + _b62(_h(self.seed, "oid", lead_id), 9)

    def opportunity(self, opportunity_id: str) -> Optional[Opportunity]:
        lead = self._lead_from_ref(opportunity_id, "o", "oid")
        return self.opportunity_for(lead) if lead else None

    def _journey(self, lead: Lead) -> list:
        """[(stage, at)] the lead goes through, generated in full (future steps included)."""
        c = self.clients[lead.client_index]
        seed = _h(self.seed, "journey", lead.id)
        created = lead.created_time + timedelta(minutes=1 + seed % 5)
        steps = [("New", created)]
        p_book = {"healthy": 0.45, "middling": 0.33, "over_cpl": 0.22, "no_leads": 0.3}[c.health]
        if c.call_centre:
            first = next((call for call in self._calls_plan(lead) if call.answered), None)
            contacted = first.started_at if first else None
        else:
            contacted = (created + timedelta(hours=_between(1, 48, seed, 1))) if _u(seed, 2) < 0.85 else None
        if contacted is None:
            if _u(seed, 3) < 0.5:
                steps.append(("Lost", created + timedelta(days=_between(6, 20, seed, 4))))
            return steps
        steps.append(("Contacted", contacted))
        if _u(seed, 5) < p_book:
            booked = contacted + timedelta(hours=_between(2, 120, seed, 6))
            steps.append(("Booked", booked))
            closed = booked + timedelta(days=_between(2, 14, seed, 7))
            steps.append(("Won" if _u(seed, 8) < 0.55 else "Lost", closed))
        elif _u(seed, 9) < 0.45:
            steps.append(("Lost", contacted + timedelta(days=_between(4, 18, seed, 10))))
        return steps

    def opportunity_for(self, lead: Lead) -> Optional[Opportunity]:
        """The lead's opportunity (about 85% of leads have one), stage as of ``now``."""
        seed = _h(self.seed, "opp", lead.id)
        if _u(seed, 1) >= 0.85:
            return None
        c = self.clients[lead.client_index]
        history = tuple((s, at) for s, at in self._journey(lead) if at <= self.now)
        if not history:
            return None
        stage_name, changed = history[-1]
        status = {"Won": "won", "Lost": "lost"}.get(stage_name, "open")
        value = round(c.deal_value * _between(0.6, 1.5, seed, 2), -1) if status == "won" else 0.0
        status_change = next((at for s, at in reversed(history) if s in ("Won", "Lost")), history[0][1])
        return Opportunity(
            id=self.opportunity_id(lead.id), contact_id=self.contact_id(lead.id), lead_id=lead.id,
            location_id=c.ghl_location_id, client_index=c.index, name=f"{lead.full_name} - {c.vertical.title()}",
            pipeline_id=c.pipeline.id, stage=c.pipeline.stage(stage_name), status=status, monetary_value=value,
            created_at=history[0][1], updated_at=changed, last_status_change_at=status_change,
            last_stage_change_at=changed, history=history)

    def opportunities(self, client: Client, since: date, until: date) -> list:
        """Opportunities of the leads created in [since, until]."""
        return [o for o in (self.opportunity_for(p) for p in self.leads(client, since, until)) if o]

    # ----- Hot Prospector side ------------------------------------------
    def hp_lead_id(self, lead_id: str) -> str:
        """The lead's Hot Prospector id: the Meta lead id with a leading 7 instead of 9."""
        return "7" + str(lead_id)[1:]

    def hp_lead(self, hp_lead_id: str) -> Optional[Lead]:
        """The lead behind a Hot Prospector lead id."""
        s = str(hp_lead_id)
        return self.lead("9" + s[1:]) if s.startswith("7") else None

    def _calls_plan(self, lead: Lead) -> list:
        """Every call for the lead, future ones included (calls_for filters to now)."""
        c = self.clients[lead.client_index]
        if not c.call_centre:
            return []
        seed = _h(self.seed, "calls", lead.id)
        members = c.members
        out, t = [], lead.created_time + timedelta(minutes=_between(3, 240, seed, 1))
        attempts = 1 + int(_u(seed, 2) * 4)
        for i in range(attempts):
            member = members[int(_u(seed, 3, i) * len(members))]
            r = _u(seed, 4, i)
            if r < 0.4:
                duration, disposition = int(_between(0, 30, seed, 5, i)), "No Answer"
            elif r < 0.55:
                duration, disposition = int(_between(15, 44, seed, 5, i)), "Left Voicemail"
            elif r < 0.6:
                duration, disposition = int(_between(5, 30, seed, 5, i)), "Wrong Number"
            else:
                duration = int(_between(60, 900, seed, 5, i))
                disposition = _pick(("Interested", "Booked Appointment", "Call Back Later", "Not Interested"), seed, 6, i)
            out.append(self._call(lead, c, member, i, "outbound", t, duration, disposition, seed))
            if duration >= 45 or disposition == "Wrong Number":
                break
            t = t + timedelta(hours=_between(18, 60, seed, 7, i))
        if _u(seed, 8) < 0.1:
            t_in = lead.created_time + timedelta(days=_between(1, 5, seed, 9))
            member = members[int(_u(seed, 10) * len(members))]
            out.append(self._call(lead, c, member, 9, "inbound", t_in, int(_between(60, 600, seed, 11)),
                                  "Interested", seed))
        return sorted(out, key=lambda x: x.started_at)

    def _call(self, lead, c, member, i, direction, at, duration, disposition, seed) -> Call:
        call_id = f"{self.hp_lead_id(lead.id)}{i}"
        recording = None
        if duration >= 45 and _u(seed, 20, i) < 0.8:
            recording = media_url(self.user_id, f"recording-{call_id}.mp3").replace(
                "scontent.xx.fbcdn.net", "service.hookscall.com")
        number = f"+44113496{_digits(4, self.seed, 'line', c.index)}"
        return Call(
            id=call_id, lead_id=lead.id, hp_lead_id=self.hp_lead_id(lead.id), contact_id=self.contact_id(lead.id),
            client_index=c.index, group_id=c.hp_group_id, member_id=member.id, member_name=member.name,
            direction=direction, started_at=at, duration=duration, disposition=disposition,
            recording_url=recording,
            from_number=number if direction == "outbound" else lead.phone,
            to_number=lead.phone if direction == "outbound" else number)

    def calls_for(self, lead: Lead) -> list:
        """The lead's Hot Prospector calls so far (empty for non call-centre clients)."""
        return [x for x in self._calls_plan(lead) if x.started_at <= self.now]

    def calls(self, client: Client, since: date, until: date) -> list:
        """Calls that started on a day in [since, until] (leads up to 14 days older are included)."""
        if not client.call_centre:
            return []
        lo, hi = _day_start(since), _day_start(until + timedelta(days=1))
        out = []
        for lead in self.leads(client, since - timedelta(days=14), until):
            out.extend(x for x in self.calls_for(lead) if lo <= x.started_at < hi)
        return sorted(out, key=lambda x: (x.started_at, x.id))
