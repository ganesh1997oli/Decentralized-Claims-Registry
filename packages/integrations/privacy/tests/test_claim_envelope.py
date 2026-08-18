import base64
import hashlib
import json

import pytest

from packages.integrations.privacy import (
    ClaimEnvelopeCipher,
    ClaimEnvelopeConfigurationError,
    ClaimEnvelopeError,
    LocalKeyRingWrapper,
)


def local_settings(**overrides):
    settings = {
        "DEPLOYMENT_ENVIRONMENT": "development",
        "CLAIM_ENCRYPTION_PROVIDER": "local",
        "CLAIM_ENCRYPTION_ACTIVE_KEY_ID": "local-2026-08",
        "CLAIM_ENCRYPTION_LOCAL_KEYS_JSON": json.dumps(
            {"local-2026-08": base64.b64encode(b"k" * 32).decode("ascii")}
        ),
        "CLAIM_ALLOW_LEGACY_PLAINTEXT": "false",
    }
    settings.update(overrides)
    return settings


def test_envelope_round_trip_hides_plaintext_and_is_randomized():
    cipher = ClaimEnvelopeCipher.from_mapping(local_settings())
    plaintext = b'{"claimReference":"private-reference"}'

    first = cipher.seal(plaintext)
    second = cipher.seal(plaintext)

    assert plaintext not in first
    # A public plaintext digest would still reveal equality and permit guesses
    # against predictable synthetic values. AES-GCM already provides integrity.
    assert hashlib.sha256(plaintext).hexdigest().encode("ascii") not in first
    assert first != second
    assert cipher.open(first) == plaintext
    assert cipher.open(second) == plaintext


def test_tampered_ciphertext_fails_authenticated_decryption():
    cipher = ClaimEnvelopeCipher.from_mapping(local_settings())
    envelope = json.loads(cipher.seal(b"sensitive claim"))
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[0] ^= 1
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")

    with pytest.raises(ClaimEnvelopeError, match="authentication failed"):
        cipher.open(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode())


def test_key_rotation_keeps_old_envelopes_readable():
    old = b"o" * 32
    new = b"n" * 32
    old_cipher = ClaimEnvelopeCipher(
        LocalKeyRingWrapper({"old": old}, active_key_id="old")
    )
    payload = old_cipher.seal(b"claim")
    rotated = ClaimEnvelopeCipher(
        LocalKeyRingWrapper({"old": old, "new": new}, active_key_id="new")
    )

    assert rotated.open(payload) == b"claim"
    assert json.loads(rotated.seal(b"new claim"))["keyId"] == "new"


def test_plaintext_is_fail_closed_unless_explicitly_enabled_for_migration():
    strict = ClaimEnvelopeCipher.from_mapping(local_settings())
    with pytest.raises(ClaimEnvelopeError, match="not an encrypted envelope"):
        strict.open(b'{"schemaVersion":6}')

    migration = ClaimEnvelopeCipher.from_mapping(
        local_settings(CLAIM_ALLOW_LEGACY_PLAINTEXT="true")
    )
    assert migration.open(b'{"schemaVersion":6}') == b'{"schemaVersion":6}'


def test_production_rejects_local_keys_and_plaintext_compatibility():
    with pytest.raises(ClaimEnvelopeConfigurationError, match="must use"):
        ClaimEnvelopeCipher.from_mapping(
            local_settings(DEPLOYMENT_ENVIRONMENT="production")
        )

    with pytest.raises(ClaimEnvelopeConfigurationError, match="cannot enable"):
        ClaimEnvelopeCipher.from_mapping(
            {
                "DEPLOYMENT_ENVIRONMENT": "production",
                "CLAIM_ENCRYPTION_PROVIDER": "gcp-kms",
                "CLAIM_ENCRYPTION_GCP_KMS_KEY": (
                    "projects/p/locations/global/keyRings/r/cryptoKeys/k"
                ),
                "CLAIM_ALLOW_LEGACY_PLAINTEXT": "true",
            }
        )
