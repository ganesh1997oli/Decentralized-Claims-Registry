"""Envelope-encrypt claim documents before they cross the IPFS seam.

IPFS content addressing provides integrity and availability, not confidentiality.
This module therefore encrypts every canonical claim with a fresh AES-256-GCM
data-encryption key (DEK). Only the small DEK is wrapped by the configured key
provider. Production requires Google Cloud KMS; a rotatable local key ring exists
only for development and deterministic tests.

The envelope itself is canonical JSON. Sepolia commits to those encrypted bytes,
so the listener can still verify the public Keccak hash without holding decryption
authority. In the current runtime only the scoring worker receives a cipher
capable of opening the envelope; human evidence review remains an insurer-owned
controlled process rather than a public-IPFS browser feature.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ENVELOPE_FORMAT = "claims-registry-envelope-v1"
_CONTENT_TYPE = "application/vnd.claims-registry.claim+json"
_DATA_AAD = b"decentralized-claims-registry:claim-document:v1"
_KEY_AAD_PREFIX = b"decentralized-claims-registry:data-key:v1:"
_AES_KEY_BYTES = 32
_NONCE_BYTES = 12


class ClaimEnvelopeError(RuntimeError):
    """Raised when encrypted claim bytes cannot be safely produced or opened."""


class ClaimEnvelopeConfigurationError(ClaimEnvelopeError):
    """Raised when storage confidentiality is not configured fail-closed."""


class KeyWrapper(Protocol):
    """Wrap short-lived data keys without exposing provider details to callers."""

    @property
    def active_key_id(self) -> str:
        """Return the stable identifier written into new envelopes."""

        ...

    def wrap(self, data_key: bytes) -> bytes:
        """Protect one random data key with the active managed key."""

        ...

    def unwrap(self, key_id: str, wrapped_data_key: bytes) -> bytes:
        """Recover a data key using the identifier retained by its envelope."""

        ...


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise ClaimEnvelopeError(f"Encrypted claim envelope has invalid {field}")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ClaimEnvelopeError(
            f"Encrypted claim envelope has invalid {field}"
        ) from exc


def _strict_boolean(settings: Mapping[str, str], name: str, default: str) -> bool:
    value = settings.get(name, default).strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ClaimEnvelopeConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True)
class LocalKeyRingWrapper:
    """Development-only wrapping adapter with rotation-safe key identifiers.

    Existing envelopes keep decrypting after rotation because every configured
    key remains addressable by ID. The adapter is deliberately rejected when
    ``DEPLOYMENT_ENVIRONMENT=production``; production must keep wrapping keys in
    managed KMS rather than a process environment variable.
    """

    keys: Mapping[str, bytes]
    active_key_id: str

    def __post_init__(self) -> None:
        if self.active_key_id not in self.keys:
            raise ClaimEnvelopeConfigurationError(
                "CLAIM_ENCRYPTION_ACTIVE_KEY_ID is absent from the local key ring"
            )
        for key_id, key in self.keys.items():
            if not key_id or len(key) != _AES_KEY_BYTES:
                raise ClaimEnvelopeConfigurationError(
                    "Every local claim wrapping key must have an ID and decode to 32 bytes"
                )

    def wrap(self, data_key: bytes) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self.keys[self.active_key_id]).encrypt(
            nonce,
            data_key,
            _KEY_AAD_PREFIX + self.active_key_id.encode("utf-8"),
        )
        return nonce + ciphertext

    def unwrap(self, key_id: str, wrapped_data_key: bytes) -> bytes:
        wrapping_key = self.keys.get(key_id)
        if wrapping_key is None:
            raise ClaimEnvelopeError("Encrypted claim references an unavailable key")
        if len(wrapped_data_key) <= _NONCE_BYTES:
            raise ClaimEnvelopeError("Encrypted claim contains an invalid wrapped key")
        try:
            data_key = AESGCM(wrapping_key).decrypt(
                wrapped_data_key[:_NONCE_BYTES],
                wrapped_data_key[_NONCE_BYTES:],
                _KEY_AAD_PREFIX + key_id.encode("utf-8"),
            )
        except Exception as exc:
            raise ClaimEnvelopeError("Encrypted claim data key could not be opened") from exc
        if len(data_key) != _AES_KEY_BYTES:
            raise ClaimEnvelopeError("Encrypted claim contains an invalid data key")
        return data_key

    @classmethod
    def from_mapping(cls, settings: Mapping[str, str]) -> LocalKeyRingWrapper:
        raw = settings.get("CLAIM_ENCRYPTION_LOCAL_KEYS_JSON", "").strip()
        active_key_id = settings.get("CLAIM_ENCRYPTION_ACTIVE_KEY_ID", "").strip()
        if not raw or not active_key_id:
            raise ClaimEnvelopeConfigurationError(
                "Local claim encryption requires CLAIM_ENCRYPTION_LOCAL_KEYS_JSON "
                "and CLAIM_ENCRYPTION_ACTIVE_KEY_ID"
            )
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClaimEnvelopeConfigurationError(
                "CLAIM_ENCRYPTION_LOCAL_KEYS_JSON must be valid JSON"
            ) from exc
        if not isinstance(document, dict) or not document:
            raise ClaimEnvelopeConfigurationError(
                "CLAIM_ENCRYPTION_LOCAL_KEYS_JSON must be a non-empty object"
            )
        keys: dict[str, bytes] = {}
        for key_id, encoded in document.items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise ClaimEnvelopeConfigurationError(
                    "Local claim wrapping keys must map string IDs to base64 values"
                )
            try:
                keys[key_id] = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ClaimEnvelopeConfigurationError(
                    "Local claim wrapping keys must be valid base64"
                ) from exc
        return cls(keys=keys, active_key_id=active_key_id)


class GcpKmsKeyWrapper:
    """Production adapter that delegates DEK wrapping to Google Cloud KMS."""

    def __init__(self, key_id: str, *, client: Any | None = None) -> None:
        if not key_id.startswith("projects/") or "/cryptoKeys/" not in key_id:
            raise ClaimEnvelopeConfigurationError(
                "CLAIM_ENCRYPTION_GCP_KMS_KEY must be a full CryptoKey resource name"
            )
        if client is None:
            try:
                from google.cloud import kms_v1
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise ClaimEnvelopeConfigurationError(
                    "google-cloud-kms is required for managed claim encryption"
                ) from exc
            client = kms_v1.KeyManagementServiceClient()
        self._client = client
        self._key_id = key_id

    @property
    def active_key_id(self) -> str:
        return self._key_id

    def wrap(self, data_key: bytes) -> bytes:
        try:
            response = self._client.encrypt(
                request={"name": self._key_id, "plaintext": data_key}
            )
            return bytes(response.ciphertext)
        except Exception as exc:
            raise ClaimEnvelopeError("Managed claim data key wrapping failed") from exc

    def unwrap(self, key_id: str, wrapped_data_key: bytes) -> bytes:
        try:
            response = self._client.decrypt(
                request={"name": key_id, "ciphertext": wrapped_data_key}
            )
            data_key = bytes(response.plaintext)
        except Exception as exc:
            raise ClaimEnvelopeError("Managed claim data key unwrapping failed") from exc
        if len(data_key) != _AES_KEY_BYTES:
            raise ClaimEnvelopeError("Managed KMS returned an invalid claim data key")
        return data_key


class ClaimEnvelopeCipher:
    """Seal and open one versioned encrypted claim-document format.

    Callers never choose algorithms, nonces, or key identifiers. Keeping those
    decisions inside this module prevents a route or worker from accidentally
    downgrading confidentiality while still allowing a production KMS adapter
    and an isolated test adapter at the key-provider seam.
    """

    def __init__(self, wrapper: KeyWrapper, *, allow_legacy_plaintext: bool = False):
        self.wrapper = wrapper
        self.allow_legacy_plaintext = allow_legacy_plaintext

    @classmethod
    def from_mapping(cls, settings: Mapping[str, str]) -> ClaimEnvelopeCipher:
        environment = settings.get("DEPLOYMENT_ENVIRONMENT", "development").strip().lower()
        provider = settings.get("CLAIM_ENCRYPTION_PROVIDER", "").strip().lower()
        allow_legacy = _strict_boolean(
            settings, "CLAIM_ALLOW_LEGACY_PLAINTEXT", "false"
        )
        if environment == "production" and allow_legacy:
            raise ClaimEnvelopeConfigurationError(
                "Production cannot enable CLAIM_ALLOW_LEGACY_PLAINTEXT"
            )
        if provider == "local":
            if environment == "production":
                raise ClaimEnvelopeConfigurationError(
                    "Production claim encryption must use CLAIM_ENCRYPTION_PROVIDER=gcp-kms"
                )
            wrapper: KeyWrapper = LocalKeyRingWrapper.from_mapping(settings)
        elif provider == "gcp-kms":
            key_id = settings.get("CLAIM_ENCRYPTION_GCP_KMS_KEY", "").strip()
            if not key_id:
                raise ClaimEnvelopeConfigurationError(
                    "CLAIM_ENCRYPTION_GCP_KMS_KEY is required for gcp-kms"
                )
            wrapper = GcpKmsKeyWrapper(key_id)
        else:
            raise ClaimEnvelopeConfigurationError(
                "CLAIM_ENCRYPTION_PROVIDER must be local or gcp-kms"
            )
        return cls(wrapper, allow_legacy_plaintext=allow_legacy)

    @classmethod
    def from_env(cls) -> ClaimEnvelopeCipher:
        return cls.from_mapping(os.environ)

    def seal(self, plaintext: bytes) -> bytes:
        if not plaintext:
            raise ClaimEnvelopeError("Refusing to encrypt an empty claim document")
        data_key = os.urandom(_AES_KEY_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        try:
            ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, _DATA_AAD)
            wrapped_key = self.wrapper.wrap(data_key)
        except ClaimEnvelopeError:
            raise
        except Exception as exc:
            raise ClaimEnvelopeError("Claim document encryption failed") from exc
        envelope = {
            "algorithm": "AES-256-GCM",
            "ciphertext": _b64encode(ciphertext),
            "contentType": _CONTENT_TYPE,
            "format": _ENVELOPE_FORMAT,
            "keyId": self.wrapper.active_key_id,
            "nonce": _b64encode(nonce),
            "wrappedDataKey": _b64encode(wrapped_key),
        }
        return json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    def open(self, payload: bytes) -> bytes:
        try:
            envelope = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if self.allow_legacy_plaintext:
                return payload
            raise ClaimEnvelopeError("Claim payload is not an encrypted envelope")
        if not isinstance(envelope, dict) or envelope.get("format") != _ENVELOPE_FORMAT:
            if self.allow_legacy_plaintext:
                return payload
            raise ClaimEnvelopeError("Claim payload is not an encrypted envelope")
        expected_fields = {
            "algorithm",
            "ciphertext",
            "contentType",
            "format",
            "keyId",
            "nonce",
            "wrappedDataKey",
        }
        if set(envelope) != expected_fields:
            raise ClaimEnvelopeError("Encrypted claim envelope has unexpected fields")
        if envelope["algorithm"] != "AES-256-GCM" or envelope["contentType"] != _CONTENT_TYPE:
            raise ClaimEnvelopeError("Encrypted claim envelope uses an unsupported format")
        key_id = envelope.get("keyId")
        if not isinstance(key_id, str) or not key_id:
            raise ClaimEnvelopeError("Encrypted claim envelope has invalid keyId")
        nonce = _b64decode(envelope.get("nonce"), "nonce")
        ciphertext = _b64decode(envelope.get("ciphertext"), "ciphertext")
        wrapped_key = _b64decode(envelope.get("wrappedDataKey"), "wrappedDataKey")
        if len(nonce) != _NONCE_BYTES:
            raise ClaimEnvelopeError("Encrypted claim envelope has invalid nonce")
        data_key = self.wrapper.unwrap(key_id, wrapped_key)
        try:
            plaintext = AESGCM(data_key).decrypt(nonce, ciphertext, _DATA_AAD)
        except Exception as exc:
            raise ClaimEnvelopeError("Encrypted claim document authentication failed") from exc
        return plaintext
