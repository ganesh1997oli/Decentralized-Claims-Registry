"""Generate a high-entropy key for the authenticated operations dashboard."""

from __future__ import annotations

import hashlib
import secrets


def main() -> None:
    # The raw key is intentionally printed once for delivery to the operator.
    # Only its one-way digest belongs in API configuration or a secret manager.
    api_key = secrets.token_urlsafe(32)
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    print(f"Raw operations key (show once): {api_key}")
    print(f'INDEXER_OPERATIONS_API_KEY_SHA256="{digest}"')


if __name__ == "__main__":
    main()
