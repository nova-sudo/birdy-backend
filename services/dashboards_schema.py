"""
services/dashboards_schema.py
-----------------------------
Validates a dashkit dashboard document against the package's JSON Schema.

`dashkit_schema.json` is a verbatim copy of dashkit's `dist/schema.json`,
refreshed by `scripts/sync_dashkit_schema.sh` in the dashkit repo — never edit
it by hand. The validator is built once at import time; a missing or
malformed schema file should fail the app at startup, not on a user's save.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

_SCHEMA_PATH = Path(__file__).with_name("dashkit_schema.json")

_VALIDATOR = Draft202012Validator(json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))


def _pointer(path) -> str:
    """JSON-pointer-ish location of an error: `/sections/0/widgets`, or `/` for the root."""
    return "/" + "/".join(str(p) for p in path)


def validate_doc(doc: dict) -> list[str]:
    """Every schema violation in `doc` as `"<json path>: <message>"`; empty when valid."""
    errors = sorted(_VALIDATOR.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{_pointer(e.absolute_path)}: {e.message}" for e in errors]


def schema_has_meta_updated_at() -> bool:
    """Whether the schema declares `meta.updatedAt`, i.e. whether the server should stamp it."""
    meta = _VALIDATOR.schema.get("properties", {}).get("meta", {})
    return "updatedAt" in meta.get("properties", {})
