import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _jwt_secrets(environ) -> tuple[str, str]:
    """(JWT_SECRET, JWT_REFRESH_SECRET), refusing to start without a real signing secret.

    PyJWT signs and verifies happily with an empty key, so a blank JWT_SECRET
    (`JWT_SECRET=` in an env file) would let anyone mint a session for any
    account, and the old refresh fallback made the refresh secret the public
    string "_refresh". Both now stop the app at startup instead.

    JWT_REFRESH_SECRET should be its own value. When it is unset (or blank) it
    is still derived from JWT_SECRET exactly as before, so refresh cookies
    already issued keep working; a warning says to set it. Setting a new value
    later only means people sign in again once their 30-day session ends.
    """
    # Blank is judged after strip, but the value is used as given: trimming a real secret
    # would change it and sign everyone out.
    secret = environ.get("JWT_SECRET") or ""
    if not secret.strip():
        raise RuntimeError(
            "JWT_SECRET environment variable is not set (or is blank). It signs every login "
            "session; generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    refresh = environ.get("JWT_REFRESH_SECRET") or ""
    if not refresh.strip():
        logger.warning("JWT_REFRESH_SECRET is not set; deriving it from JWT_SECRET. Set its own value.")
        refresh = secret + "_refresh"
    elif refresh == secret:
        raise RuntimeError("JWT_REFRESH_SECRET must differ from JWT_SECRET.")
    return secret, refresh


# JWT
JWT_SECRET, JWT_REFRESH_SECRET = _jwt_secrets(os.environ)
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 60 * 24 * 30  # 30 days
JWT_REFRESH_EXPIRY_DAYS = 365  # 1 year

# Cookies
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)

# CORS
CORS_ORIGINS = [
    "https://birdy-beta.vercel.app",
    "http://localhost:3000",
    "http://localhost:3005",
]
# More exact origins without a code change: comma-separated.
CORS_ORIGINS += [o.strip().rstrip("/") for o in os.getenv("CORS_EXTRA_ORIGINS", "").split(",") if o.strip()]
# Every Vercel deployment of the frontend project: previews and past builds
# (birdy-beta-<hash or branch>-<team>.vercel.app). The team suffix keeps it to
# our own project; a bare *.vercel.app would let anyone's site call the API.
CORS_ORIGIN_REGEX = os.getenv(
    "CORS_ORIGIN_REGEX",
    r"https://birdy-beta-[a-z0-9-]+-abdelrahman-mohameds-projects-ef52aae4\.vercel\.app",
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://birdy-beta.vercel.app").rstrip("/")
# Public URL of this API (OAuth issuer; the MCP server is served at {API_PUBLIC_URL}/mcp).
API_PUBLIC_URL = os.getenv("API_PUBLIC_URL", "https://birdy-backend.vercel.app").rstrip("/")
# PostHog server-side events (services/posthog_events.py). Without a key, events are skipped.
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY", "")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")
# Alert emails over SMTP (services/email_service.py), same settings as nodemailer. Without SMTP_HOST/USER/PASS,
# email sending is skipped. Gmail: host smtp.gmail.com, port 465, secure true, pass = a Google app password.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465") or 465)
SMTP_SECURE = os.getenv("SMTP_SECURE", "true").strip().lower() in ("1", "true", "yes")  # true = SSL (465), false = STARTTLS (587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "") or (f"Birdy <{SMTP_USER}>" if SMTP_USER else "Birdy <alerts@birdy.ai>")
