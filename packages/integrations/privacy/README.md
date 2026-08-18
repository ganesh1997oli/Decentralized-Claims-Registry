# Claim document privacy

This module owns the cryptographic boundary between canonical claim JSON and
public content-addressed storage. Application routes and storage adapters do not
select algorithms, nonces or key identifiers.

## Envelope design

For every claim, `ClaimEnvelopeCipher`:

1. creates a random 256-bit data-encryption key;
2. encrypts the canonical claim with AES-256-GCM and fixed, versioned additional
   authenticated data;
3. wraps the data key with the configured provider;
4. emits deterministic-key-order JSON containing format, algorithm, key ID,
   nonce, ciphertext and wrapped data key; and
5. lets the caller anchor Keccak-256 of those exact envelope bytes on-chain.

The public envelope deliberately contains no plaintext digest because that
would leak equality and enable guesses against predictable values. AES-GCM
provides authenticated integrity. Decryption fails closed for malformed,
unknown-key or tampered envelopes.

## Providers

| Provider | Intended environment | Key custody |
| --- | --- | --- |
| `local` | Development and deterministic tests only | Explicit base64 32-byte key ring in process settings |
| `gcp-kms` | Production | Google Cloud KMS symmetric CryptoKey wraps each random data key |

Production mode rejects the local adapter and legacy plaintext. Rotation keeps
old envelopes readable because each stores the key identifier used at creation.
Do not remove an old local key or KMS version until the associated retention and
key-destruction policy permits those claims to become unreadable.

## Process access

The API needs encryption and wrapping access. The scoring worker needs unwrap
and decryption access. The listener needs neither: it verifies the envelope hash
against Sepolia without opening the claim. The browser receives no encryption
key. Human evidence review is intentionally handled outside public IPFS by the
insurer's controlled evidence process.

Run the focused tests from the repository root:

```bash
apps/backend/.venv/bin/python -m pytest \
  packages/integrations/privacy/tests -q
```
