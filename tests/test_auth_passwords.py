from api.auth import _hash_pw, _verify_password

def test_scrypt_password_verifier_is_salted_and_validates():
    first = _hash_pw("a-secure-password")
    second = _hash_pw("a-secure-password")

    assert first.startswith("scrypt$")
    assert first != second
    assert _verify_password("a-secure-password", first) == (True, False)
    assert _verify_password("wrong-password", first) == (False, False)


def test_legacy_sha256_verifier_remains_compatible_for_login_migration():
    import hashlib

    legacy = hashlib.sha256(b"a-secure-password").hexdigest()
    assert _verify_password("a-secure-password", legacy) == (True, True)
