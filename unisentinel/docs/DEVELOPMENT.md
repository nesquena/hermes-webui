UniSentinel development notes

Quick tasks to continue scaffold development:

- Add monitors for governance, slippage, and contract events
- Replace file-backed DB with SQLite or Postgres in production
- Add authentication and role-based access for alert acknowledgement
- Add frontend dashboard (React or lightweight templating)
- Add automated tests for Web3Service and AlertService
- Hook up CI to run lint and tests, and let CI install dependencies and run npm install

Local dev notes

- Copy .env.example to .env and set ETHEREUM_RPC_URL
- Start: npm run dev
- Data: alerts are persisted to unisentinel/data/alerts.json by default
