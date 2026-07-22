# IPFS integration

This module gives the rest of the application one small interface for storing
and retrieving claim bytes through Pinata and IPFS.

- `client.py` uploads bytes through the Pinata Files endpoint.
- It converts safe `ipfs://` pointers into gateway URLs.
- It retries newly uploaded CIDs that are not immediately available.
- `__init__.py` exposes the public classes used by FastAPI, the listener, and the
  Kafka consumer.
- `tests/` checks the behaviour without contacting Pinata or a live gateway.

## Role in the claim workflow

FastAPI creates canonical JSON and passes the exact bytes to `IPFSClient`. The
client returns a CID, which the application records as `ipfs://<CID>`. The
application downloads the file again and hashes the returned bytes before it
writes anything to Sepolia.

The blockchain stores the CID and Keccak-256 hash, not the complete claim.
Later, the listener downloads the same CID and checks that its bytes still match
the on-chain hash.

## Configuration

The adapter reads two environment variables:

| Variable | Required | Purpose |
| --- | :---: | --- |
| `PINATA_JWT` | For uploads | Server-side Pinata credential with public file-upload permission |
| `IPFS_GATEWAY` | No | Base gateway URL used to retrieve a CID |

Create the ignored local file from the tracked template:

```bash
cp integrations/ipfs/.env.example integrations/ipfs/.env.local
```

The default gateway is `https://gateway.pinata.cloud/ipfs`. Any process using
IPFS loads the module-owned file with:

```bash
set -a; source integrations/ipfs/.env.local; set +a
```

Never put `PINATA_JWT` in the React environment or commit it to Git.

## Install and test

The backend and listener requirement files include this module's dependency. To
test it directly from the repository root:

```bash
source backend/.venv/bin/activate
python -m pytest integrations/ipfs/tests -q
```

The tests use a fake HTTP session, so they do not upload files or require a JWT.

## Public IPFS warning

The current upload explicitly uses Pinata's public network. A CID is an address,
not a password. Anyone who obtains the CID can request the unencrypted content
while an IPFS node continues to provide it.

For that reason, this prototype accepts synthetic data only. A real claim would
need encryption before upload, controlled key distribution, retention and
deletion policies, and a documented privacy and regulatory assessment.

See the [root project guide](../../README.md) for the complete data flow.
