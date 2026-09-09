Deploy & Publish Instructions for UniSentinel

The repository changes are staged on branch: feature/unisentinel-ci-deploy (local). You do not have push permissions from this environment. To publish these changes to GitHub and deploy to Vercel, follow the steps below.

Option A — Apply patches locally and push (recommended if you don't want to share tokens):

1. From your local machine clone the repo (if not already):
   git clone https://github.com/nesquena/hermes-webui.git
   cd hermes-webui

2. Copy the patches directory into your repo root (if you received it), or run the following in this repository if you have access to it:
   # patches/ contains numbered patch files created by the assistant
   git am patches/*.patch

   This will apply the commits and create the branch history locally.

3. Create a remote branch and push:
   git checkout -b feature/unisentinel-ci-deploy
   git push origin feature/unisentinel-ci-deploy

4. Open a Pull Request on GitHub from feature/unisentinel-ci-deploy -> master (or your default branch).

5. Connect Vercel:
   - Go to https://vercel.com and sign in with your GitHub account.
   - Import Project -> Select the hermes-webui repository -> Choose the feature/unisentinel-ci-deploy branch or master after merge.
   - Build command: (none) — the project uses Node.js and the Dockerfile; for a Node deployment use the root unisentinel folder as the project root.
   - Set Environment Variables in Vercel (Important):
       ETHEREUM_RPC_URL (optional for live RPC)
       MULTICALL_ADDRESS (if you want multicall batching)
       UNISENTINEL_DB_DRIVER=sqlite (for local/ephemeral DB in Vercel, consider external DB for prod)
   - Deploy.

Option B — Grant push permissions (not recommended to share tokens publicly):

If you trust this environment and want the assistant to push and create the PR automatically, provide a GitHub personal access token with repo permissions, or add the environment as a GitHub deploy key with write access. Then instruct the assistant and it will push and create the PR.

Notes on Vercel setup
- Vercel prefers projects with a single package.json at the root. For the unisentinel subproject, set the Project Root to '/unisentinel' during import.
- Build Command: npm ci && npm run build (if a build exists). For this app, Vercel should run `npm install` and use `npm start` to run the server. Vercel's serverless model isn't ideal for stateful SQLite; prefer using Vercel to host the frontend only and run backend on a small VPS, Render, or Heroku.

Local quick test
- To run locally (Windows PowerShell):
  cd unisentinel
  npm ci
  setx UNISENTINEL_DB_DRIVER sqlite
  $env:UNISENTINEL_DB_DRIVER = 'sqlite'
  npm start
  # open http://localhost:3000

If you'd like, I can also prepare a PR body and checklist file. The patches/ directory is available with the commits so you can apply them and push from your machine.
