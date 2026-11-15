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
# Alert emails (services/email_service.py). Without a key, email sending is skipped.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Birdy <alerts@birdy.ai>")
