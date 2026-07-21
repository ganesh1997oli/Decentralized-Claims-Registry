# React claim-submission form (proposal Week 4)

This Vite/React/Tailwind application calls the Week 3 FastAPI backend. It does
not access a wallet, Pinata token, or private key in the browser.

## Install and verify

```bash
cd frontend
npm install
npm test
npm run lint
npm run build
```

## Run

```bash
cp .env.example .env.local
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. FastAPI must be running on
`http://127.0.0.1:8000` unless `VITE_API_BASE_URL` is changed.

Only submit synthetic data. The current Pinata integration uses public,
unencrypted IPFS.
