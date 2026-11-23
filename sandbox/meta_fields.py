"""
sandbox/meta_fields.py
----------------------
Graph's ``fields`` grammar, parsed:

    name,status,insights.date_preset(maximum){spend,actions},adsets.limit(200){name,ads{id}}

becomes a list of FieldSpec(name, mods, children, raw_children). Modifier arguments may hold
JSON (``insights.time_range({"since":"2026-11-01","until":"2026-11-07"}){spend}``), so parentheses,
braces, brackets and quotes nest inside them. Empty items (``a,,b``) are skipped, as Graph does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class FieldsError(ValueError):
    pass


@dataclass
class FieldSpec:
    name: str
    mods: dict = field(default_factory=dict)        # modifier -> raw argument text
    children: Optional[list] = None                 # None: no {...}
    raw_children: str = ""                          # the text between the braces


_STOP = set(".,{}()")


def parse_fields(text: Optional[str]) -> list:
    """The specs in a ``fields`` value ([] for None / empty)."""
    if not text:
        return []
    specs, i = _parse_list(text, 0, None)
    if i != len(text):
        raise FieldsError(f"Syntax error in fields near position {i}: {text[i:i + 20]!r}")
    return specs


def _parse_list(s: str, i: int, close: Optional[str]):
    out = []
    while i < len(s):
        ch = s[i]
        if close and ch == close:
            return out, i
        if ch in ", \n\t":
            i += 1
            continue
        spec, i = _parse_item(s, i)
        if spec:
            out.append(spec)
        if i < len(s) and s[i] == ",":
            i += 1
    if close:
        raise FieldsError(f"Unclosed '{{' in fields")
    return out, i


def _parse_item(s: str, i: int):
    start = i
    while i < len(s) and s[i] not in _STOP:
        i += 1
    name = s[start:i].strip()
    spec = FieldSpec(name=name)
    while i < len(s) and s[i] == ".":
        i += 1
        mstart = i
        while i < len(s) and s[i] not in _STOP:
            i += 1
        mod = s[mstart:i].strip()
        if i >= len(s) or s[i] != "(":
            raise FieldsError(f"Modifier {mod!r} on {name!r} needs an argument")
        arg, i = _paren_arg(s, i)
        spec.mods[mod] = arg
    if i < len(s) and s[i] == "{":
        cstart = i + 1
        children, i = _parse_list(s, i + 1, "}")
        spec.children, spec.raw_children = children, s[cstart:i]
        i += 1   # past '}'
    return (spec if name else None), i


def _paren_arg(s: str, i: int):
    """The text inside the parentheses starting at s[i] == '(' and the index past its ')'."""
    depth, j, quote = 0, i, None
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack = []
    while j < len(s):
        ch = s[j]
        if quote:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in pairs:
            stack.append(pairs[ch])
            depth += 1
        elif stack and ch == stack[-1]:
            stack.pop()
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    raise FieldsError("Unclosed '(' in fields")


def names(specs: list) -> set:
    return {s.name for s in specs}
