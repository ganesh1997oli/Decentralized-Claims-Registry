"""Generate one insurer API key and the digest-only server configuration entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets

from web3 import Web3

INSURER_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]\Z")
CREDENTIAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def main() -> None:
    """Print a one-time raw key and its safe digest-only credential record."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "insurer_id", help="authoritative insurer ID, e.g. northstar-mutual"
    )
    parser.add_argument(
        "credential_id", help="rotation-friendly ID, e.g. northstar-cloud-v1"
    )
    parser.add_argument(
        "signer_address",
        help="authorized EIP-712 signer holding SUBMITTER_ROLE",
    )
    parser.add_argument("--daily-quota", type=int, default=25)
    args = parser.parse_args()
    if args.daily_quota < 1:
        parser.error("--daily-quota must be at least 1")
    if not INSURER_ID_PATTERN.fullmatch(args.insurer_id):
        parser.error(
            "insurer_id must use 3-64 lowercase letters, numbers, and internal hyphens"
        )
    if not CREDENTIAL_ID_PATTERN.fullmatch(args.credential_id):
        parser.error(
            "credential_id must use 1-100 letters, numbers, dots, "
            "underscores, or hyphens"
        )
    try:
        signer_address = Web3.to_checksum_address(args.signer_address)
    except ValueError:
        parser.error("signer_address must be a valid Ethereum address")
    if int(signer_address, 16) == 0:
        parser.error("signer_address cannot be the zero address")

    api_key = secrets.token_urlsafe(32)
    entry = {
        "credentialId": args.credential_id,
        "insurerId": args.insurer_id,
        "apiKeySha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
        "signerAddress": signer_address,
        "dailyQuota": args.daily_quota,
    }
    print("Give this raw API key only to the insurer operator:")
    print(api_key)
    print("\nStore only this JSON entry on the server:")
    print(json.dumps(entry, separators=(",", ":")))


if __name__ == "__main__":
    main()
