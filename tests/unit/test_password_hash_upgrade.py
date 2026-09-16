"""PBKDF2 hashing: new-format + legacy verify, and rehash-on-upgrade detection."""

from app.core.security import (
    hash_password, verify_password, needs_rehash, _pbkdf2, _LEGACY_ITERATIONS,
)


def test_new_hash_roundtrip():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)
    assert not needs_rehash(h)


def test_legacy_hash_verifies_and_flags_rehash():
    # Reconstruct an original-format hash: "salt$hash" at the old 100k iterations.
    salt = "a" * 32
    legacy = f"{salt}${_pbkdf2('pw', salt, _LEGACY_ITERATIONS)}"
    assert verify_password("pw", legacy)
    assert not verify_password("nope", legacy)
    assert needs_rehash(legacy)


def test_lower_iteration_new_format_flags_rehash():
    salt = "b" * 32
    weak = f"pbkdf2_sha256$1000${salt}${_pbkdf2('pw', salt, 1000)}"
    assert verify_password("pw", weak)
    assert needs_rehash(weak)
