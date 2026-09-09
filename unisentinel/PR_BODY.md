Title: feat(unisentinel): observability, CI audit, Dockerfile, tests, and accessibility improvements

Summary
-------
This PR brings the UniSentinel subproject up to a more production-ready state. Key changes:
- Add Prometheus-style /metrics endpoint and in-memory metrics counters
- Wire MULTICALL_ADDRESS from env into SlippageService
- Add tests: API integration tests, metrics test, multicall fallback tests
- Replace file DB fallback with sqlite adapter for tests / production
- CI improvements: npm ci fallback, npm audit step, Docker smoke-build
- Dockerfile improvements and Dependabot config
- Small frontend accessibility fixes (skip link, ARIA attributes)
- Added SECURITY-AUDIT.md summarizing npm audit findings

Testing
-------
- Run `npm test` inside unisentinel — all unit & integration tests pass locally.

Deployment
----------
- See DEPLOY_INSTRUCTIONS.md for steps to apply patches, push branch, and deploy to Vercel.

Checklist
---------
- [ ] CI passes on GitHub Actions
- [ ] PR reviewers: review dependency upgrades, especially sqlite3 and mocha majors
- [ ] Perform integration tests against a forked mainnet or local multicall
- [ ] Decide on production DB (SQLite is not ideal for multi-instance deployments)

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
