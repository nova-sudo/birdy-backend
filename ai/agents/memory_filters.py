"""
ai/agents/memory_filters.py
---------------------------
Exclusions taught to the agent as memories ("never touch Evergreen ads",
"leave Acme alone at weekends"). Pure: no I/O, no clock.

A memory's `filter` is a dict of conditions, all of which must match (AND) for
the memory to exclude a finding:

  client_group_id   the finding's client
  ad_name_contains  case-insensitive substring of a target's name, or of any
                    ad inside an ad set target (`member_names`); the
                    finding's title when it has no named targets
  weekdays          list of ints, Monday = 0, matched against `as_of`

A memory with no filter, or an empty one, excludes nothing. Unknown keys never
match, so a filter we don't understand can't silently exclude everything.
"""

from datetime import date

from ai.suggestions.contracts import Finding

KNOWN_KEYS = ("client_group_id", "ad_name_contains", "weekdays")


def _names(f: Finding) -> list[str]:
    names = []
    for t in (f.action.targets if f.action else []):
        names.append(str(t.get("name") or ""))
        # An ad set target affects every ad inside it.
        names.extend(str(n or "") for n in (t.get("member_names") or []))
    names = [n for n in names if n]
    return names or [f.title or ""]


def _matches(flt: dict, f: Finding, as_of: date | None) -> bool:
    for key, want in flt.items():
        if key == "client_group_id":
            if f.client_group_id != want:
                return False
        elif key == "ad_name_contains":
            needle = str(want or "").lower()
            if not needle or not any(needle in n.lower() for n in _names(f)):
                return False
        elif key == "weekdays":
            if as_of is None or as_of.weekday() not in {int(d) for d in (want or [])}:
                return False
        else:
            return False
    return True


def apply(findings: list[Finding], memories: list[dict], *, as_of: date | None = None) -> list[Finding]:
    """Findings not excluded by any memory filter. Pass only active memories."""
    filters = [m.get("filter") for m in memories if isinstance(m.get("filter"), dict) and m.get("filter")]
    if not filters:
        return list(findings)
    return [f for f in findings if not any(_matches(flt, f, as_of) for flt in filters)]
