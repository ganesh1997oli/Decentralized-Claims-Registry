"""Privacy-preserving claim document storage.

The public interface intentionally exposes one operation in each direction:
seal canonical claim bytes before external storage, and open a verified envelope
inside an authorised worker. Key-provider and cipher details stay behind that
small seam so callers cannot accidentally upload plaintext.
"""

from .claim_envelope import (
    ClaimEnvelopeCipher,
    ClaimEnvelopeConfigurationError,
    ClaimEnvelopeError,
    GcpKmsKeyWrapper,
    LocalKeyRingWrapper,
)

__all__ = [
    "ClaimEnvelopeCipher",
    "ClaimEnvelopeConfigurationError",
    "ClaimEnvelopeError",
    "GcpKmsKeyWrapper",
    "LocalKeyRingWrapper",
]
