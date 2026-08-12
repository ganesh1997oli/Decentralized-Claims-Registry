"""Generate a human-assessor API key and digest-only configuration entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets

ASSESSOR_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def main() -> None:
    """Print one raw key for delivery and one safe JSON credential record."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "assessor_reference",
        help="stable audit reference, e.g. research-assessor-1",
    )
    args = parser.parse_args()
    if not ASSESSOR_REFERENCE.fullmatch(args.assessor_reference):
        parser.error(
            "assessor_reference must use 1-100 letters, numbers, dots, "
            "underscores, or hyphens"
        )

    # This raw key is shown once for transfer to the assessor's password manager.
    # Only the JSON entry containing its one-way digest belongs on the API host.
    api_key = secrets.token_urlsafe(32)
    entry = {
        "assessorReference": args.assessor_reference,
        "apiKeySha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
    }
    print("Give this raw API key only to the human assessor:")
    print(api_key)
    print("\nAppend this JSON entry to ASSESSOR_OUTCOME_CREDENTIALS_JSON:")
    print(json.dumps(entry, separators=(",", ":")))


if __name__ == "__main__":
    main()

