"""Unit: argon2 wrapper (docs/06 §Unit, docs/08 §1)."""

from __future__ import annotations


from app.core.security import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery")
    assert h != "correct horse battery"
    assert h.startswith("$argon2")
    assert verify_password(h, "correct horse battery") is True


def test_verify_wrong_password_false():
    h = hash_password("secret-one")
    assert verify_password(h, "secret-two") is False


def test_verify_broken_hash_false():
    assert verify_password("not-a-valid-hash", "whatever") is False


def test_dummy_hash_is_valid_argon2_and_anti_timing():
    # DUMMY_HASH — реальный argon2-хеш, verify против него всегда False.
    assert DUMMY_HASH.startswith("$argon2")
    assert verify_password(DUMMY_HASH, "any-guess") is False


def test_needs_rehash_default_false():
    h = hash_password("pw")
    assert needs_rehash(h) is False


def test_hashes_are_salted_unique():
    assert hash_password("same") != hash_password("same")
