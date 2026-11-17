import os
from dotenv import load_dotenv

load_dotenv()

# JWT
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 60 * 24 * 30  # 30 days
JWT_REFRESH_SECRET = os.getenv("JWT_REFRESH_SECRET", (JWT_SECRET or "") + "_refresh")
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
