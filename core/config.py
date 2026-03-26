import os
from dotenv import load_dotenv

load_dotenv()

# JWT
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 600
JWT_REFRESH_SECRET = os.getenv("JWT_REFRESH_SECRET", (JWT_SECRET or "") + "_refresh")
JWT_REFRESH_EXPIRY_DAYS = 30

# Cookies
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)

# CORS
CORS_ORIGINS = [
    "https://birdy-beta.vercel.app",
    "http://localhost:3000",
]
