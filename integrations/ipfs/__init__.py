"""Small interface for the IPFS-backed claim-storage integration."""

from .client import (
    DEFAULT_IPFS_GATEWAY,
    PINATA_UPLOAD_URL,
    IPFSClient,
    IPFSError,
)

__all__ = [
    "DEFAULT_IPFS_GATEWAY",
    "PINATA_UPLOAD_URL",
    "IPFSClient",
    "IPFSError",
]
