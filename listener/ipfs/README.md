# IPFS claim storage

Everything specific to IPFS and Pinata lives in this folder:

- `client.py` uploads claim bytes and downloads `ipfs://` pointers.
- `__init__.py` is the small interface used by the backend and listener.
- `requirements.txt` owns the HTTP client dependency.
- `tests/` verifies uploads, downloads and pointer validation without a network.

The module stores only the bytes supplied by its caller. The application is
responsible for creating canonical JSON and for deciding whether encryption is
required before upload. The current dissertation demonstration uses public,
unencrypted IPFS and must therefore contain synthetic data only.

Configuration is still loaded from `listener/.env.local`:

```dotenv
PINATA_JWT="your-server-side-token"
IPFS_GATEWAY="https://gateway.pinata.cloud/ipfs"
```

Never expose `PINATA_JWT` to the React frontend or commit it to Git.
