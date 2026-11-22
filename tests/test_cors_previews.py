"""Which sites may call the API from a browser."""
import re

from core.config import CORS_ORIGIN_REGEX, CORS_ORIGINS

OLD_BUILD = "https://birdy-beta-oupti3jw3-abdelrahman-mohameds-projects-ef52aae4.vercel.app"


def allowed(origin: str) -> bool:
    # Starlette's CORSMiddleware uses fullmatch on allow_origin_regex.
    return origin in CORS_ORIGINS or bool(re.fullmatch(CORS_ORIGIN_REGEX, origin))


def test_our_previews_and_past_builds_are_allowed():
    assert allowed("https://birdy-beta.vercel.app")
    assert allowed(OLD_BUILD)
    assert allowed("https://birdy-beta-git-feat-x-abdelrahman-mohameds-projects-ef52aae4.vercel.app")


def test_other_sites_are_not():
    assert not allowed("https://evil.vercel.app")
    assert not allowed("https://birdy-beta-x-someone-elses-team.vercel.app")
    assert not allowed(OLD_BUILD + ".evil.com")
    assert not allowed("http://" + OLD_BUILD[len("https://"):])
