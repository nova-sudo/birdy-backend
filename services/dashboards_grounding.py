"""
services/dashboards_grounding.py
--------------------------------
Checks that the figures in a model-written briefing sentence come from the data
the model was shown. Used by services/dashboards_briefing.py.

The facts are a list of *cells*: one (widget, row-or-total, column) with its
value, its previous-period value (when the frame has one) and the column's
format. A figure in the text is grounded only by a cell, in the way its form
says:

  - plain ("$1.2k", "1,234", "€2000", "1.5bn", "$2.5mm"): within 1% (relative) of
    any cell value, current or previous, or equal to it rounded to the shown
    precision ("$1.2k" matches 1234.5); a frame's row count also counts.
  - percent ("12%"): equal to a value of a percentage-format column, or to the
    signed change (current - previous) / |previous| of the SAME cell. When the
    words around it say up / down (rose, fell, increase, decrease, ...), or it
    carries a +/- sign, the change must have that sign.
  - multiple ("3x"): current / previous of the same cell.
  - points ("40pp", "5 pts", "3 percentage points"): current - previous of the
    same percentage-format cell, with the same sign rule.
  - duration ("30 days", "2 weeks", "1 month"): the length of the frames' window
    in that unit.

Not figures: dates ("Oct 20", "2026-10-20", "10/20"), a 4-digit 19xx/20xx year
only when it is the year of a frame window's `from` / `to`, sits next to a month
name or is inside an ISO date, ordinals ("20th"), digits glued into a word
("Q3") and digits inside the board's own names ("Campaign 2"). Every other
number must ground; an unknown unit glued to a figure does not exempt it.

A sentence passes only if every figure grounds and at least one of them is not a
duration (so "last 7 days" alone never makes a sentence grounded).
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

REL_TOL = 0.01

_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")

# A day of the month (1-31). A number after a month name is blanked only when it is one, and not when a noun
# shows it counts something ("In October 99 new leads", "Oct 20 leads grew").
_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_COUNTED = (r"(?!\s*%)(?!\s+(?:new\s+|total\s+)?(?:leads?|clicks?|results?|impressions?|calls?|opps?|opportunities|"
            r"contacts?|clients?|campaigns?|ads?|sales|customers?|conversions?|visitors?|sessions?|orders?)\b)")

# Dates, blanked before figures are extracted (months before the plain-day forms so "Oct 2026" is a year).
_DATE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\b\d{4}-\d{2}-\d{2}\b",                                              # 2026-10-20
    rf"\b{_MONTHS}\.?,?\s+(?:19|20)\d{{2}}\b",                             # Oct 2026
    rf"\b(?:19|20)\d{{2}}\s+{_MONTHS}\b",                                  # 2026 Oct
    rf"\b{_MONTHS}\.?\s+{_DAY}(?:st|nd|rd|th)?\b{_COUNTED}(?:,?\s+(?:19|20)\d{{2}}\b)?",   # Oct 20, October 20th, 2026
    rf"\b{_DAY}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}\b(?:,?\s+(?:19|20)\d{{2}}\b)?",  # 20 Oct, 20th of October
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",                                   # 10/20, 10/20/2026
    r"\b\d{1,2}-\d{1,2}-\d{2,4}\b",                                        # 20-10-2026
)]

_DURATION = re.compile(r"(?<![\w.])(?P<n>\d+(?:\.\d+)?)[\s-]*(?P<unit>day|week|month)s?\b", re.IGNORECASE)

_NUMBER = re.compile(
    r"(?<![\w.])(?P<cur>[$€£])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"(?:"
    r"(?P<word>\s?(?:thousand|million|billion)\b)"
    r"|(?P<suf>bn|mm|[kmb])(?![A-Za-z])"
    r"|(?P<pct>\s?%|\s?(?:percent|per\s?cent|pct)\b)"
    r"|(?P<pts>\s?(?:percentage\s+points?|pp|pts?|points?)\b)"
    r"|(?P<mul>[x×])(?![A-Za-z])"
    r"|(?P<ord>st|nd|rd|th)\b"
    r")?",
    re.IGNORECASE,
)
_MULTIPLIER = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}

_UP = r"up|rose|rise|rises|rising|risen|grew|grow|grows|growing|growth|increase|increased|increases|increasing|" \
      r"climbed|jumped|gained|surged|higher|improved|gain|above|more"
_DOWN = r"down|fell|fall|falls|falling|fallen|drop|dropped|drops|decrease|decreased|decreases|decreasing|decline|" \
        r"declined|declines|lower|slid|dipped|shrank|shrunk|slumped|plunged|reduced|lost|below|less|cut"
_DIRECTION = re.compile(rf"\b(?:(?P<up>{_UP})|(?P<down>{_DOWN}))\b", re.IGNORECASE)


@dataclass
class Figure:
    value: float            # with k / m / b applied
    half_width: float       # half a unit of the last shown digit, in `value`'s unit
    kind: str               # plain | percent | multiple | points | duration
    unit: str = ""          # duration unit
    direction: int = 0      # +1 up, -1 down, 0 none stated


@dataclass
class Cell:
    value: Optional[float]
    previous: Optional[float]
    fmt: str


def _blank_names(text: str, names: Iterable[str]) -> str:
    """Blank the board's own names that contain a digit ("Campaign 2"), whole names only."""
    for name in sorted({n for n in names if n and any(c.isdigit() for c in n)}, key=len, reverse=True):
        text = re.sub(rf"(?<![\w$€£])(?<!\d[.,]){re.escape(name)}(?!\w|[.,]\d)", " ", text, flags=re.IGNORECASE)
    return text


def _direction(text: str, start: int, end: int) -> int:
    """+1 / -1 from the nearest up / down word around the figure, or from a +/- sign; 0 if none."""
    def sign(m):
        return 1 if m.group("up") else -1

    offset = max(0, start - 40)
    before = list(_DIRECTION.finditer(text[offset:start]))
    after = _DIRECTION.search(text[end:end + 25])
    nearest = []
    if before:
        nearest.append((start - (offset + before[-1].end()), 0, sign(before[-1])))   # ties go to the word before
    if after:
        nearest.append((after.start(), 1, sign(after)))
    if nearest:
        return min(nearest)[2]
    if start > 0 and text[start - 1] == "+" and (start == 1 or text[start - 2] in " ("):
        return 1
    if start > 0 and text[start - 1] in "-−–" and (start == 1 or text[start - 2] in " ("):
        return -1
    return 0


def extract_figures(text: str, names: Iterable[str] = (), years: Iterable[str] = ()) -> list[Figure]:
    """Every figure in `text` that has to be grounded (see the module docstring for what is not one)."""
    text = _blank_names(text, names)
    for pattern in _DATE_PATTERNS:
        text = pattern.sub(" ", text)
    for year in set(years):
        text = re.sub(rf"(?<![\w.,$€£]){re.escape(year)}(?![\w%]|[.,]\d)", " ", text)

    figures = []

    def blank_duration(m):
        figures.append(Figure(float(m.group("n")), 0.0, "duration", unit=m.group("unit").lower()))
        return " "

    text = _DURATION.sub(blank_duration, text)

    for m in _NUMBER.finditer(text):
        if m.group("ord"):
            continue
        digits = m.group("num").replace(",", "")
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        mult = _MULTIPLIER.get((m.group("suf") or m.group("word") or "").strip().lower(), 1.0)
        kind = "plain"
        if m.group("pct"):
            kind = "percent"
        elif m.group("pts"):
            kind = "points"
        elif m.group("mul"):
            kind = "multiple"
        direction = _direction(text, m.start(), m.end()) if kind in ("percent", "points") else 0
        figures.append(Figure(float(digits) * mult, 0.5 * 10 ** -decimals * mult, kind, direction=direction))
    return figures


def _close(n: float, ref: float, half_width: float) -> bool:
    return abs(n - ref) <= max(REL_TOL * abs(ref), half_width) + 1e-9


def _days(start: str, end: str) -> Optional[int]:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    except (TypeError, ValueError):
        return None


class Grounding:
    """The facts a figure may rest on: cells, row counts and the frames' window lengths."""

    def __init__(self, cells: list[Cell], row_counts: Iterable[int] = (), windows: Iterable[tuple] = ()):
        self.cells = cells
        self.counts = [float(c) for c in row_counts]
        self.window_days = [d for d in (_days(f, t) for f, t in windows) if d]
        self.years = {x[:4] for f, t in windows for x in (f, t) if isinstance(x, str) and len(x) >= 4 and x[:4].isdigit()}

    def _plain(self, n: float, hw: float) -> bool:
        if any(_close(n, c, hw) for c in self.counts):
            return True
        return any(v is not None and _close(n, abs(v), hw)
                   for cell in self.cells for v in (cell.value, cell.previous))

    @staticmethod
    def _signed_ok(delta: float, direction: int) -> bool:
        return direction == 0 or (delta > 0 and direction > 0) or (delta < 0 and direction < 0)

    def _percent(self, f: Figure) -> bool:
        for cell in self.cells:
            if cell.fmt == "percentage" and any(v is not None and _close(f.value, abs(v), f.half_width)
                                                for v in (cell.value, cell.previous)):
                return True
            if cell.value is None or cell.previous in (None, 0):
                continue
            change = (cell.value - cell.previous) / abs(cell.previous) * 100
            if self._signed_ok(change, f.direction) and _close(f.value, abs(change), f.half_width):
                return True
        return False

    def _points(self, f: Figure) -> bool:
        for cell in self.cells:
            if cell.fmt != "percentage" or cell.value is None or cell.previous is None:
                continue
            delta = cell.value - cell.previous
            if self._signed_ok(delta, f.direction) and _close(f.value, abs(delta), f.half_width):
                return True
        return False

    def _multiple(self, f: Figure) -> bool:
        return any(cell.value is not None and cell.previous not in (None, 0)
                   and _close(f.value, abs(cell.value / cell.previous), f.half_width)
                   for cell in self.cells)

    def _duration(self, f: Figure) -> bool:
        per = {"day": 1.0, "week": 7.0, "month": 30.4375}[f.unit]
        return any(abs(d / per - f.value) <= 0.5 for d in self.window_days)

    def holds(self, f: Figure) -> bool:
        if f.kind == "percent":
            return self._percent(f)
        if f.kind == "points":
            return self._points(f)
        if f.kind == "multiple":
            return self._multiple(f)
        if f.kind == "duration":
            return self._duration(f)
        return self._plain(f.value, f.half_width)

    def is_grounded(self, text: str, names: Iterable[str] = ()) -> bool:
        """Every figure in `text` grounds, and at least one is not a bare duration."""
        figures = extract_figures(text, names, self.years)
        return (any(f.kind != "duration" for f in figures)
                and all(self.holds(f) for f in figures))
