"""
ai/agents/stock/writer.py
-------------------------
The AI half of a card (spec section 6, Writing). Code supplies every number and the whole
allowed list; the model only chooses among allowed ads and writes short text. Nothing it
returns reaches a card without `validate_draft`.

Money rule: a money figure in the text is a number next to a currency (symbol, ISO code or
word). It must come from the facts or the candidates, in a currency that fact was given in.
Decision on rounding: a figure written with fewer decimals than the fact is accepted when it is
that fact rounded half-up or truncated to the written precision (4.115 may be written 4.12, 4.11,
4.1 or 4), with
"k" and "m" suffixes scaled the same way. Any figure that is not a rounding of a supplied one,
including sums and differences, is rejected. A bare number is also money when it sits within 30
characters of cpl, cost, spend, spent, budget, limit or target (either order) and is not a
percentage or a count of leads, ads, days and the like; it must match a fact in any currency. "1,234.50", "1.234,50" and "4,12" are all read.
"""

import asyncio
import logging
import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from ai.prompts import stock_agent as prompt

logger = logging.getLogger(__name__)

LLM_TIMEOUT_S = 45
MAX_TOKENS = 1500
TEXT_FIELDS = ("additional_changes", "thought_process", "next_steps")
TEXT_MAX = 1500
FLAG_KINDS = ("city", "offer", "clinic")

_SYMBOLS = {"£": {"GBP"}, "€": {"EUR"}, "¥": {"JPY", "CNY"}, "₹": {"INR"}, "₩": {"KRW"}, "₺": {"TRY"},
            "₽": {"RUB"}, "₪": {"ILS"}, "₫": {"VND"}, "฿": {"THB"}, "₦": {"NGN"}, "₱": {"PHP"},
            "$": {"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"}}
_WORDS = {"pound": {"GBP"}, "pounds": {"GBP"}, "quid": {"GBP"}, "euro": {"EUR"}, "euros": {"EUR"},
          "dollar": _SYMBOLS["$"], "dollars": _SYMBOLS["$"]}
_CODES = ("GBP USD EUR CAD AUD NZD AED SAR QAR KWD BHD OMR EGP INR JPY CNY CHF SEK NOK DKK PLN CZK "
          "HUF TRY ZAR MXN BRL SGD HKD ILS THB MYR IDR PHP KRW NGN RUB").split()
_SYM = "[" + "".join(_SYMBOLS) + "]"
_SP = "[ \u00a0\u202f]"
_NUM = (rf"\d{{1,3}}(?:[.,]\d{{3}})+(?:[.,]\d+)?|\d{{1,3}}(?:{_SP}\d{{3}})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
_SCALE = r"(?:[kK]|[mM]|bn|million|billion|thousand|grand)(?![A-Za-z])"
_SCALES = {"k": 1000, "thousand": 1000, "grand": 1000, "m": 10**6, "million": 10**6,
           "bn": 10**9, "billion": 10**9}
_MONEY_KEYS = ("spend", "cpl", "prev_spend", "prev_cpl", "green_under", "red_over")
# A bare number (no currency marker) is money only by shape. Either it directly follows a money
# word with at most a short linker ("limit is 7", "budget to 250"), or it has a decimal part and
# sits within _NEAR characters of a money word ("CPL fell 31% to 4.12"). Counted nouns, units,
# percentages, dates, month names and long ids are never money.
_MONEY_WORDS = re.compile(r"\b(?:cpl|cost|costs|spend|spent|budget|budgets|limit|limits|target)\b", re.I)
_LINK = r"(?:of|at|to|is|was|from|by|around|about|under|over|hit|reached)"
_LINKED = re.compile(rf"(?:\s*(?:\b{_LINK}\b|[=:]))*\s*", re.I)
_NEAR = 30
_COUNTED = re.compile(r"\s?(?:%|percent\b|(?:ads?|leads?|days?|weeks?|hours?|sets?|variants?)\b)", re.I)
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_NEXT_MONTH = re.compile(rf"\s?(?:st|nd|rd|th)?\s?{_MONTH}\b", re.I)
_PREV_MONTH = re.compile(rf"\b{_MONTH}\s?$", re.I)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _money_re(codes) -> re.Pattern:
    code = "|".join(sorted(set(codes), key=len, reverse=True))
    word = "|".join(_WORDS)
    return re.compile(
        rf"(?:(?<![A-Za-z])(?P<pc>{code})\s?|(?P<ps>{_SYM})\s?)(?P<pn>{_NUM})(?:\s?(?P<pk>{_SCALE}))?"
        rf"|(?P<sn>{_NUM})(?:\s?(?P<sk>{_SCALE}))?\s?"
        rf"(?:(?P<sc>{code})(?![A-Za-z])|(?P<ss>{_SYM})|(?P<sw>{word})(?![A-Za-z]))")


def _parse_amount(raw: str) -> tuple[Decimal, int]:
    """Value and number of decimals written, reading '.' or ',' as decimal or thousands, and a
    space or nbsp before exactly three digits as thousands."""
    raw = re.sub(_SP, "", raw)
    has_c, has_d = "," in raw, "." in raw
    if has_c and has_d:
        dec = "," if raw.rfind(",") > raw.rfind(".") else "."
    elif has_c:
        tail = raw.rsplit(",", 1)[1]
        dec = "," if raw.count(",") == 1 and len(tail) != 3 else None
    elif has_d:
        dec = "." if raw.count(".") == 1 else None
    else:
        dec = None
    if dec:
        whole, frac = raw.rsplit(dec, 1)
        return Decimal(re.sub(r"[.,]", "", whole) + "." + frac), len(frac)
    return Decimal(re.sub(r"[.,]", "", raw)), 0


def _money_figures(text: str, codes) -> list[tuple[str, Decimal, int, int, set]]:
    """[(as written, value, decimals, scale, possible currency codes)]. Currency codes are
    empty for a bare number, which then matches any currency's facts."""
    out, spans = [], []
    for m in _money_re(codes).finditer(text):
        raw = m.group("pn") or m.group("sn")
        try:
            value, dec = _parse_amount(raw)
        except InvalidOperation:
            continue
        scale = _SCALES.get((m.group("pk") or m.group("sk") or "").lower(), 1)
        cur = (m.group("pc") or m.group("sc") or "").upper()
        if cur:
            cs = {cur}
        else:
            cs = _SYMBOLS.get(m.group("ps") or m.group("ss") or "") or _WORDS.get((m.group("sw") or "").lower(), set())
        out.append((m.group(0).strip(), value, dec, scale, cs))
        spans.append(m.span())
    words = [m.span() for m in _MONEY_WORDS.finditer(text)]
    spans += [m.span() for m in _ISO_DATE.finditer(text)]
    bare = re.compile(rf"(?<![\w:.,-]){_NUM}(?:\s?{_SCALE})?(?![\w:-])")
    for m in bare.finditer(text):
        if any(a < m.end() and m.start() < b for a, b in spans):
            continue
        digits = re.sub(r"\D", "", m.group(0))
        if (len(digits) >= 11 or _COUNTED.match(text, m.end()) or _NEXT_MONTH.match(text, m.end())
                or _PREV_MONTH.search(text[:m.start()])):
            continue
        direct = any(e <= m.start() and _LINKED.fullmatch(text[e:m.start()]) for _, e in words)
        try:
            decimal = _parse_amount(re.match(_NUM, m.group(0)).group(0))[1] > 0
        except (InvalidOperation, AttributeError):
            continue
        near = any(0 <= m.start() - e <= _NEAR or 0 <= s - m.end() <= _NEAR for s, e in words)
        if not (direct or (decimal and near)):
            continue
        sm = re.search(_SCALE + "$", m.group(0))
        num = m.group(0)[:sm.start()].strip() if sm else m.group(0)
        try:
            value, dec = _parse_amount(re.match(_NUM, num).group(0))
        except (InvalidOperation, AttributeError):
            continue
        out.append((m.group(0).strip(), value, dec, _SCALES.get(sm.group(0).lower(), 1) if sm else 1, set()))
    return out


def _supplied_money(facts: dict, candidates: dict) -> list[tuple[str | None, Decimal]]:
    """Every (currency, amount) the model was shown. Facts and revive/remove use the client's
    currency; an Add candidate uses its source account's."""
    pool: list = []

    def take(obj, cur):
        if isinstance(obj, dict):
            for k in _MONEY_KEYS:
                v = obj.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    pool.append((cur, Decimal(str(v))))
        return obj

    own = facts.get("currency")
    take(facts, own)
    take(facts.get("zones"), own)
    for ad in facts.get("ads") or []:
        take(ad, own)
    for c in (candidates.get("revive") or []) + (candidates.get("remove") or []):
        take(c, own)
    for c in candidates.get("add") or []:
        take(c, c.get("currency") or own)
    return pool


def _rounds_to(fact: Decimal, value: Decimal, dec: int, scale: int) -> bool:
    q = Decimal(1).scaleb(-dec)
    try:
        return any((fact / scale).quantize(q, rounding=r) == value for r in (ROUND_HALF_UP, ROUND_DOWN))
    except InvalidOperation:
        return False


def _money_errors(text: str, codes, pool) -> list[str]:
    errs = []
    for written, value, dec, scale, currencies in _money_figures(text, codes):
        if value == 0:
            continue
        ok = any((cur is None or not currencies or cur in currencies) and _rounds_to(fact, value, dec, scale)
                 for cur, fact in pool)
        if not ok:
            errs.append(f"{written} is not a figure from the facts")
    return errs


def _id_list(raw, key: str, allowed: set[str], errs: list[str]) -> list[str]:
    val = raw.get(key, [])
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        errs.append(f"{key} must be a list of ids")
        return []
    out = list(dict.fromkeys(val))
    for x in out:
        if x not in allowed:
            errs.append(f"{x} is not an allowed {key} id")
    return out


_SLACK_MARKUP = re.compile(r"<(?:![^<>]*|[@#][^<>]*|(?:https?://|mailto:)[^<>]*)>")


def _plain(text: str) -> str:
    """Remove Slack-shaped tokens (<!channel>, <@U1>, <#C1>, <https://x|y>, <mailto:x>). A bare
    < or > is ordinary text and stays."""
    while True:
        out = _SLACK_MARKUP.sub("", text)
        if out == text:
            return out.strip()
        text = out


def _clean_flags(raw, chosen: set[str]) -> list[dict]:
    out = []
    for f in raw if isinstance(raw, list) else []:
        if isinstance(f, dict) and f.get("id") in chosen:
            kind = str(f.get("kind") or "").lower()
            out.append({"id": f["id"], "kind": kind if kind in FLAG_KINDS else "other",
                        "note": _plain(str(f.get("note") or ""))[:200]})
    return out


def validate_draft(raw: dict, facts: dict, candidates: dict, max_live: int) -> tuple[dict | None, list[str]]:
    """(clean draft, []) or (None, reasons). Ids must be allowed, live ads must stay within
    max_live, and every money figure in the text must come from the facts or candidates."""
    if not isinstance(raw, dict):
        return None, ["reply is not a JSON object"]
    errs: list[str] = []
    add = _id_list(raw, "add", {c["candidate_id"] for c in candidates.get("add") or []}, errs)
    remove = _id_list(raw, "remove", {c["ad_id"] for c in candidates.get("remove") or []}, errs)
    revive = _id_list(raw, "revive", {c["ad_id"] for c in candidates.get("revive") or []}, errs)
    if set(remove) & set(revive):
        errs.append("an ad cannot be both removed and revived")
    live_after = (facts.get("ads_live") or 0) - len(remove) + len(add) + len(revive)
    if live_after > max_live:
        errs.append(f"{live_after} ads would be live, more than the limit of {max_live}")
    text: dict[str, str] = {}
    for f in TEXT_FIELDS:
        v = raw.get(f, "")
        if not isinstance(v, str):
            errs.append(f"{f} must be text")
            v = ""
        text[f] = _plain(v)[:TEXT_MAX]
    pool = _supplied_money(facts, candidates)
    codes = set(_CODES) | {c for c, _ in pool if isinstance(c, str)}
    for f, v in text.items():
        errs += [f"{f}: {e}" for e in _money_errors(v, codes, pool)]
    if errs:
        return None, errs
    flags = _clean_flags(raw.get("flags"), set(add) | set(remove) | set(revive))
    for fl in flags:
        errs += [f"flags note: {e}" for e in _money_errors(fl["note"], codes, pool)]
    if errs:
        return None, errs
    return {"add": add, "remove": remove, "revive": revive, **text, "flags": flags}, []


def _fmt_money(v, currency) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return "no data"
    cur = currency if isinstance(currency, str) and currency.strip() else None
    sym = next((s for s, cs in _SYMBOLS.items() if cs == {cur}), None) if cur else None
    if sym:
        return f"{sym}{v:.2f}"
    return f"{v:.2f} {cur}" if cur else f"{v:.2f}"


def _failed(facts: dict) -> dict:
    """The plain code-written card. Never raises, whatever the facts hold."""
    try:
        cur = facts.get("currency")
        sentence = (f"CPL moved from {_fmt_money(facts.get('prev_cpl'), cur)} to {_fmt_money(facts.get('cpl'), cur)}. "
                    "Birdy couldn't write this one, so please review the changes yourself.")
    except Exception:
        sentence = "Birdy couldn't write this one, so please review the changes yourself."
    return {"add": [], "remove": [], "revive": [], "additional_changes": "", "thought_process": sentence,
            "next_steps": "", "flags": [], "failed": True}


async def write_card(db, agent: dict, facts: dict, candidates: dict, lessons: list, *,
                     last_card: dict | None = None, open_questions: list | None = None) -> dict:
    """One AI call, one retry that quotes what was wrong. Never raises: any failure (no
    credits, provider error, timeout, unusable or invalid reply twice) gives the plain
    code-written sentence with no changes and failed=True, so nothing runs on Auto."""
    from ai import provider_factory
    from ai.agents.learning import _meter, _parse_json
    from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
    import credits

    user_id = agent.get("user_id")
    try:
        if await credits.is_blocked(db, user_id):
            return _failed(facts)
        max_live = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})["creative"]["max_ads_live"]
        provider = await provider_factory.get_provider_for_user(user_id, db)
        problems: list[str] = []
        for _ in range(2):
            resp = await asyncio.wait_for(provider.chat_completion(
                messages=[{"role": "system", "content": prompt.SYSTEM},
                          {"role": "user", "content": prompt.build_user_message(
                              facts, candidates, lessons, max_live, problems,
                              last_card=last_card, open_questions=open_questions)}],
                temperature=0, max_tokens=MAX_TOKENS), timeout=LLM_TIMEOUT_S)
            await _meter(db, user_id, provider, resp, feature="stock_agent_card")
            parsed = _parse_json(getattr(resp, "content", None))
            if parsed is None:
                problems = ["the reply was not a JSON object"]
                continue
            draft, problems = validate_draft(parsed, facts, candidates, max_live)
            if draft is not None:
                return {**draft, "failed": False}
        logger.warning("stock writer: draft rejected twice for %s: %s", facts.get("client_group_id"), problems)
    except Exception as e:
        logger.warning("stock writer: could not write the card for %s: %s", facts.get("client_group_id"), e)
    return _failed(facts)
