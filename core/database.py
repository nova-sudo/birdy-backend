import os

DB_NAME = os.getenv("MONGODB_DB", "birdyaidev")

def get_db(mongo_client):
    """Return the application database from a Motor client."""
    return mongo_client[DB_NAME]
