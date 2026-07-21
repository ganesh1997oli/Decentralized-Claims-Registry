# React claim-submission form (proposal Week 4)

This Vite/React/Tailwind application calls the FastAPI backend. It does not
access a wallet, Pinata token, model artifact, or private key in the browser.
The receipt shows the Week 5 synthetic fraud score, contributing indicators,
and the separate Sepolia assessment transaction. The paginated claims dashboard
calls `GET /claims?page=...&page_size=...` on startup and after each successful
submission to display on-chain claim status and fraud score, newest first. The
interface offers 5, 10, 25, or 50 claims per page.

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
