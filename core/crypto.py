"""
core/crypto.py
-----------------
Encryption at rest for user-supplied secrets (currently: BYOK AI provider
API keys — see services/ai_credential_service.py). Every other third-party
credential in this codebase (GHL/Meta OAuth tokens, HotProspector API keys)
is stored plaintext; this is the first place that changes, because a leaked
raw Anthropic/OpenAI key is directly billable by anyone who gets it.

Deliberately fails fast at import time if AI_CREDENTIAL_ENCRYPTION_KEY is
unset — unlike core/config.py's JWT_SECRET (which only breaks loudly the
first time a token is signed/verified), a missing or rotated encryption key
here would silently make every previously-stored credential undecryptable
until a user's chat request fails deep inside provider construction.

Generate a key once with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Store the same value across every instance/environment — rotating it
invalidates every previously-encrypted credential (users would need to
re-enter their key).
"""

import os

from cryptography.fernet import Fernet, InvalidToken

_KEY = os.getenv("AI_CREDENTIAL_ENCRYPTION_KEY")
if not _KEY:
    raise RuntimeError(
        "AI_CREDENTIAL_ENCRYPTION_KEY environment variable is not set. "
        "Generate one with: "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

_fernet = Fernet(_KEY.encode() if isinstance(_KEY, str) else _KEY)


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt credential — encryption key may have changed")
