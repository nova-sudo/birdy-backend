import httpx
import json
import urllib.parse
from datetime import datetime, timedelta
import logging
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# Meta OAuth configuration
META_OAUTH_CONFIG = {
    "auth_url": "https://www.facebook.com/v25.0/dialog/oauth",
    "token_url": "https://graph.facebook.com/v25.0/oauth/access_token",
    "scopes": [
        "ads_management",
        "ads_read",
        "read_insights",
        "pages_show_list",
        "business_management",
        "leads_retrieval",
        "pages_read_engagement",
        "pages_manage_ads"
    ]
}

FACEBOOK_CLIENT_ID = os.getenv("FACEBOOK_CLIENT_ID")
FACEBOOK_CLIENT_SECRET = os.getenv("FACEBOOK_CLIENT_SECRET")
FACEBOOK_REDIRECT_URI = os.getenv("FACEBOOK_REDIRECT_URI")

class FacebookIntegration:
    def __init__(self, client_id, client_secret, redirect_uri):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def generate_auth_url(self):
        """Generate auth URL for Meta OAuth flow without state"""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": ",".join(META_OAUTH_CONFIG["scopes"])
        }
        return f"{META_OAUTH_CONFIG['auth_url']}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, code):
        """Exchange authorization code for access token, then upgrade to long-lived token."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # Step 1: Exchange auth code for short-lived token
                response = await client.post(
                    META_OAUTH_CONFIG["token_url"],
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": self.redirect_uri,
                        "code": code
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded"
                    }
                )
                if response.status_code != 200:
                    logger.error(f"Token exchange failed: {response.status_code} {response.text}")
                    return False, {"error": response.json().get("error", "Token exchange failed"), "status_code": response.status_code}
                tokens = response.json()
                short_lived_token = tokens.get("access_token")

                # Step 2: Exchange short-lived token for long-lived token (~60 days)
                try:
                    ll_response = await client.get(
                        META_OAUTH_CONFIG["token_url"],
                        params={
                            "grant_type": "fb_exchange_token",
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "fb_exchange_token": short_lived_token,
                        },
                    )
                    if ll_response.status_code == 200:
                        ll_data = ll_response.json()
                        tokens["access_token"] = ll_data.get("access_token", short_lived_token)
                        expires_in = ll_data.get("expires_in", 60 * 24 * 60 * 60)
                        tokens["expires_in"] = expires_in
                        tokens["token_type"] = "long_lived"
                        logger.info("Successfully exchanged for long-lived Meta token (expires in %s s)", expires_in)
                    else:
                        logger.warning("Long-lived token exchange failed (%s), keeping short-lived token", ll_response.status_code)
                except Exception as ll_err:
                    logger.warning("Long-lived token exchange error, keeping short-lived token: %s", ll_err)

                expires_in = tokens.get("expires_in", 60 * 24 * 60 * 60)  # Default: 60 days
                tokens["expires_at"] = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
                logger.info(f"Token exchange complete, expires_in={expires_in}s")
                return True, tokens
            except httpx.RequestError as e:
                logger.error(f"Network error during token exchange: {str(e)}")
                return False, {"error": f"Network error: {str(e)}"}

async def save_facebook_token(user_id: str, tokens: dict, mongo_client: AsyncIOMotorClient):
    """Save Meta access token to MongoDB"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        users_collection = db["users"]
        token_doc = {
            "access_token": tokens.get("access_token"),
            "expires_at": datetime.fromisoformat(tokens.get("expires_at")) if isinstance(tokens.get("expires_at"), str) else tokens.get("expires_at"),
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        await users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "integrations.facebook": token_doc,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )
        logger.info(f"Saved Meta tokens for user: {user_id}")
    except Exception as e:
        logger.error(f"Failed to save Meta tokens for user {user_id}: {str(e)}", exc_info=True)
        raise

async def get_facebook_token(user_id: str, mongo_client: AsyncIOMotorClient):
    """Retrieve Meta access token from MongoDB"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        users_collection = db["users"]
        user_doc = await users_collection.find_one({"user_id": user_id})
        if user_doc and user_doc.get("integrations", {}).get("facebook"):
            return user_doc["integrations"]["facebook"]
        logger.warning(f"No Meta token found for user: {user_id}")
        return None
    except Exception as e:
        logger.error(f"Failed to retrieve Meta token for user {user_id}: {str(e)}", exc_info=True)
        raise

# Instantiate Meta integration
facebook_integration = FacebookIntegration(FACEBOOK_CLIENT_ID, FACEBOOK_CLIENT_SECRET, FACEBOOK_REDIRECT_URI)