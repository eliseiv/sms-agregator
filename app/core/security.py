"""Хеширование паролей — argon2id singleton (docs/08-security §1)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Единый экземпляр с дефолтными (memory-hard) параметрами argon2-cffi.
_PH = PasswordHasher()

# Фиксированный dummy-hash для анти-timing (несуществующий логин / NULL-пароль).
DUMMY_HASH = _PH.hash("anti-timing-placeholder-not-a-real-password")


def get_password_hasher() -> PasswordHasher:
    return _PH


def hash_password(password: str) -> str:
    return _PH.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True при совпадении; False при несовпадении/битом хеше."""
    try:
        _PH.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:  # низкоуровневые ошибки argon2
        return False


def needs_rehash(password_hash: str) -> bool:
    return _PH.check_needs_rehash(password_hash)
