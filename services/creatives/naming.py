"""Creative test naming convention: ``CT-0001 | slug``."""

import re
import unicodedata

CODE_RE = re.compile(r"^CT-\d{4,}")
SLUG_MAX = 40
SEPARATOR = " | "


def format_code(seq: int) -> str:
    return f"CT-{seq:04d}"


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:SLUG_MAX].strip("-")


def meta_ad_name(code: str, slug: str) -> str:
    return f"{code}{SEPARATOR}{slug}"


def parse_code(ad_name: str | None) -> str | None:
    if not ad_name:
        return None
    m = CODE_RE.match(ad_name)
    return m.group(0) if m else None
