"""Generate a proposal-maker API key and digest-only governance entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets

GOVERNANCE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
ETHEREUM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


def main() -> None:
    """Print a one-time raw key and its safe server-side configuration entry."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "governance_reference",
        help="stable audit reference, e.g. northstar-governance-1",
    )
    parser.add_argument(
        "insurer_address",
        help="0x-prefixed insurer address that bounds this proposal maker",
    )
    args = parser.parse_args()
    if not GOVERNANCE_REFERENCE.fullmatch(args.governance_reference):
        parser.error(
            "governance_reference must use 1-100 letters, numbers, dots, "
            "underscores, or hyphens"
        )
    if not ETHEREUM_ADDRESS.fullmatch(args.insurer_address):
        parser.error("insurer_address must be a 20-byte 0x-prefixed address")

    # The raw credential belongs in the maker's password manager and is shown
    # once. The API host stores only its one-way digest, which limits the impact
    # of an accidental configuration read. This key prepares proposals only; a
    # distinct DECISION_MAKER_ROLE wallet must still sign the chain transaction.
    api_key = secrets.token_urlsafe(32)
    entry = {
        "governanceReference": args.governance_reference,
        "insurerAddress": args.insurer_address,
        "apiKeySha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
    }
    print("Give this raw API key only to the governance proposal maker:")
    print(api_key)
    print("\nAppend this JSON entry to GOVERNANCE_CREDENTIALS_JSON:")
    print(json.dumps(entry, separators=(",", ":")))


if __name__ == "__main__":
    main()
