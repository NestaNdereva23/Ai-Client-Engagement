"""Password hashing for reviewer_user (app.auth.passwords), no database needed."""

from __future__ import annotations

from app.auth.passwords import hash_password, verify_password


def test_correct_password_verifies() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)


def test_wrong_password_is_rejected() -> None:
    stored = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", stored)


def test_same_password_hashes_differently_each_time() -> None:
    """Salted: two hashes of the same password never match each other, but
    both still verify against the original password.
    """
    first = hash_password("shared password")
    second = hash_password("shared password")
    assert first != second
    assert verify_password("shared password", first)
    assert verify_password("shared password", second)


def test_unicode_password_round_trips() -> None:
    stored = hash_password("p@sswörd-日本語-123")
    assert verify_password("p@sswörd-日本語-123", stored)
    assert not verify_password("p@sswörd-日本語-124", stored)


def test_malformed_stored_hash_fails_closed() -> None:
    assert not verify_password("anything", "not-a-real-hash")
    assert not verify_password("anything", "pbkdf2_sha256$not-a-number$abcd$abcd")
    assert not verify_password("anything", "wrong_algorithm$600000$abcd$abcd")
