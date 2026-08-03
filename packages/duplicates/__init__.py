"""Cross-insurer duplicate-claim detection."""

from .detector import (
    FINGERPRINT_VERSION,
    CrossInsurerDuplicateDetector,
    DuplicateCheck,
    DuplicateDetectionConfigurationError,
    DuplicateMatch,
)

__all__ = [
    "FINGERPRINT_VERSION",
    "CrossInsurerDuplicateDetector",
    "DuplicateCheck",
    "DuplicateDetectionConfigurationError",
    "DuplicateMatch",
]
