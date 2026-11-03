from services.creatives.naming import format_code, slugify, meta_ad_name, parse_code


def test_format_code_pads_to_four():
    assert format_code(7) == "CT-0007"
    assert format_code(12345) == "CT-12345"


def test_slugify_rules():
    assert slugify("Mortgage myth #3 — UGC hook") == "mortgage-myth-3-ugc-hook"
    assert slugify("  Ünïcode   & spaces  ") == "unicode-spaces"
    assert len(slugify("x" * 80)) == 40
    assert not slugify("a" * 39 + " b").endswith("-")


def test_meta_ad_name_and_parse_roundtrip():
    name = meta_ad_name("CT-0048", "mortgage-myth-ugc")
    assert name == "CT-0048 | mortgage-myth-ugc"
    assert parse_code(name) == "CT-0048"
    assert parse_code("ct-0048 | x") is None
    assert parse_code("Old ad CT-0048") is None
    assert parse_code(None) is None
