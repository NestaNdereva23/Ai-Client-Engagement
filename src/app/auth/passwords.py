"""Password hashing for reviewer_user, with no dependency beyond the stdlib.

Stored as "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>" so the
iteration count can be raised later without breaking passwords hashed
under the old one -- verify_password reads the count back out of the
stored string instead of assuming today's constant.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16


class InvalidPasswordHash(ValueError):
    """A stored hash isn't in the expected pbkdf2_sha256$... shape."""


def hash_password(password: str) -> str:
    """A fresh salted hash for password. Two calls on the same password
    never produce the same string, since the salt is random each time.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Whether password matches a hash_password() string.

    Returns False (never raises) for a malformed stored value, so a
    corrupted or hand-edited row fails closed rather than crashing login.
    """
    try:
        algorithm, iterations_text, salt_hex, digest_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            raise InvalidPasswordHash(algorithm)
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, InvalidPasswordHash):
        return False

    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)
