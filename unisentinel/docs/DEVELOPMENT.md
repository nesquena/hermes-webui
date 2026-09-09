UniSentinel development notes

Quick tasks to continue scaffold development:

- Add monitors for governance, slippage, and contract events

Note: This project targets Node.js >= 18 (see package.json "engines"). Use Node 18+ when running locally or in CI.
- Replace file-backed DB with SQLite or Postgres in production
- Add authentication and role-based access for alert acknowledgement
- Add frontend dashboard (React or lightweight templating)
- Add automated tests for Web3Service and AlertService
- Hook up CI to run lint and tests, and let CI install dependencies and run npm install
- Use MULTICALL_ADDRESS (optional) to configure a multicall contract to batch on-chain reads and reduce RPC calls
- Metrics: the server exposes a simple Prometheus-format endpoint at /metrics providing counters for slippage scanning (multicall calls, multicall failures, single RPC calls, and pairs scanned)

Security note:
- A `.dockerignore` file has been added under `unisentinel/.dockerignore` to exclude `.env`, `data/`, `logs/`, and other sensitive or mutable files from Docker build contexts. Do not put credentials in files inside the `unisentinel/` folder; instead configure secrets as environment variables in your CI or hosting provider.

Local dev notes

- Copy .env.example to .env and set ETHEREUM_RPC_URL
- If using multicall, set MULTICALL_ADDRESS in your .env
- Start: npm run dev
- Data: alerts are persisted to unisentinel/data/alerts.json by default

Local dev notes

- Copy .env.example to .env and set ETHEREUM_RPC_URL
- Start: npm run dev
- Data: alerts are persisted to unisentinel/data/alerts.json by default
