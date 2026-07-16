"""core/crypto.py backs BYOK AI credential storage — every other secret in
this codebase (GHL/Meta/HotProspector) is stored plaintext, so round-trip
correctness here matters more than usual."""

import pytest

from core.crypto import encrypt, decrypt


def test_round_trip():
    plaintext = "sk-ant-super-secret-key-12345"
    ciphertext = encrypt(plaintext)
    assert ciphertext != plaintext
    assert decrypt(ciphertext) == plaintext


def test_ciphertext_is_not_deterministic():
    # Fernet includes a random IV/timestamp — encrypting the same plaintext
    # twice must not produce identical ciphertext (would leak that two users
    # have the same key).
    a = encrypt("same-value")
    b = encrypt("same-value")
    assert a != b
    assert decrypt(a) == decrypt(b) == "same-value"


def test_decrypt_garbage_raises_value_error():
    with pytest.raises(ValueError, match="Failed to decrypt"):
        decrypt("not-a-real-fernet-token")
