import pytest

from app.crypto import encrypt_text, reveal_secret, secrets_equal


def test_secrets_equal_matches_and_rejects():
    assert secrets_equal("abc", "abc") is True
    assert secrets_equal("abc", "abd") is False
    assert secrets_equal("abc", "ab") is False
    assert secrets_equal(None, "abc") is False
    assert secrets_equal("abc", None) is False
    assert secrets_equal("", "") is True


def test_reveal_secret_keeps_plaintext_access_key():
    assert reveal_secret("x" * 16, "LTAI123") == "LTAI123"


def test_reveal_secret_rejects_undecryptable_fernet():
    token = encrypt_text("a" * 16, "LTAIreal")
    with pytest.raises(ValueError):
        reveal_secret("b" * 16, token)
