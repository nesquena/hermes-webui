# Conversation overview and session file browser

Evidence uses the isolated full-engine fixture, synthetic users and a local
deterministic provider. No production state or external model was used.

- The overview test fails on the previous implementation at the absent
  `btnConversationBoard` entry. `before-overview.png` comes from that revision.
- 19 integrated browser cases passed: conversation workbench, bot workbench,
  role workspace, mobile summary panels, session actions and session-activation
  races. The four new workbench cases passed again after adjusting mobile filter
  control sizes. The file case also passed when capturing the file panel alone.
  These are repeat validations, not additive test counts.
- Desktop, light, 900px and 390px overview captures are included. File captures
  show the actual panel, excluding the synthetic tool transcript beside it.
- Runtime JavaScript lint and diff whitespace checks passed.

The new cases check persisted pin/search selection, draft preservation, opening
the correct transcript, live runtime state, retained keyboard focus, profile
switch loading, failed refresh/retry, identity isolation, local file filtering,
real checked previews, reset on session changes and mobile control bounds.

Existing Python coverage was also run: 45 passed and two failures reproduced
on the unchanged base (the cross-session extracted-JS harness times out, and
the artifact locale test expects obsolete inline catalogs). An exploratory
profile/relative-time selection had 54 passes and seven failures also reproduced
on the base: five profile expectations and two obsolete inline-locale assertions.
These selections overlap. They do not establish a green full suite.

Relevant commands:

```sh
npm run lint:runtime
python scripts/e2e/run_full.py --engine /path/to/isolated-compatible-engine \
  --out /path/to/evidence \
  --grep 'CONVERSATION WORKBENCH|BOT WORKBENCH|WORKSPACE SHELL|SUPPLEMENT SESSION ACTIVATION|US-SP-SESS-ACTIONS|US-SP-UX-MOBILE'
```

Use the repository's `./scripts/test.sh` wrapper for Python tests. No runtime,
permission policy, canonical session identity or persisted message state is
changed by the new views; they consume existing authorized projections.
