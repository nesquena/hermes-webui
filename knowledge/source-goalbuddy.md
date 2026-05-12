# Source Trail — GoalBuddy

Checked: 2026-05-10 13:20 JST
Source URL: https://github.com/tolibear/goalbuddy
Status: lightweight source-backed recon; not installed locally

## Conclusion

GoalBuddy is worth studying as a workflow-design reference for long-running Codex work, not as something to install into Yuto immediately.

Its strongest idea is not the npm package itself. The useful pattern is:

> vague goal -> charter -> board -> one active role-tagged task -> receipt -> verification -> repeat

This maps closely to Yuto's control-plane direction: keep work bounded, evidence-backed, and auditable instead of letting a coding agent drift through a broad prompt.

Recommended posture for Kei/Yuto:

- Borrow the operating model and board/receipt discipline.
- Do not install or adopt as a standing workflow yet.
- If piloting, use it only in a disposable Codex test repo with no production credentials.
- Keep Yuto as PM/verification gate; Codex remains a callable worker, not a standing autonomous builder.

## Verified source facts

From GitHub API, README, `package.json`, and repo files checked 2026-05-10:

- Repo: `tolibear/goalbuddy`
- GitHub description: `Supercharge Codex Goals`
- README tagline: `Turn open-ended Codex work into one reviewable goal board.`
- Created: 2026-05-03
- Last pushed observed: 2026-05-08
- Latest observed main commit: `16c9db9e7584`, message `Merge pull request #11 from clairernovotny/codex/fix-windows-codex-spawn`
- GitHub stars/forks at check time: 292 stars, 22 forks
- License: MIT
- NPM package: `goalbuddy`
- NPM latest observed: `0.2.21`
- NPM description: `Turn open-ended Codex goals into a GoalBuddy Scout/Judge/Worker board with receipts, verification, and optional extensions.`
- Runtime: Node `>=18`
- CLI bins: `goalbuddy` and compatibility alias `goal-maker`
- Local availability checked: `goalbuddy_cli=None`; Node and npm are available locally

## What it is

GoalBuddy is a local Codex companion / npm package that installs a Codex skill and optional plugin/extension surfaces.

It is designed for Codex tasks that are too broad to trust to one prompt, such as:

- `improve this project`
- long-running refactors
- ambiguous implementation goals
- recovery from stale or failed work
- audits where planning, verification, and receipts matter

The README says it turns a vague request into:

```text
docs/goals/<slug>/
  goal.md
  state.yaml
  notes/
```

Where:

- `goal.md` is the editable charter: objective, constraints, tranche, and stop rule.
- `state.yaml` is board truth: task status, active task, receipts, and verification.
- `notes/` stores longer Scout/Judge/PM findings when a receipt is too large.

## Operating model

GoalBuddy's core primitives:

1. Charter
   - The current goal/tranche, constraints, original ask, likely misfire, and proof of completion.

2. Board
   - `state.yaml` is authoritative. It tracks tasks, status, active task, receipts, and verification.

3. Task
   - Exactly one active task at a time.
   - Task types include `scout`, `judge`, `worker`, and `pm`.

4. Receipt
   - Every completed, blocked, or escalated task leaves compact durable proof.

Default loop:

```text
vague goal -> Scout -> Judge -> Worker -> receipt -> verify -> repeat
```

The main `/goal` thread acts as PM. It owns the board, decides sequencing, keeps one active task, records receipts, and should complete only after Judge or PM audit proves the original outcome is done.

## Role model

### Scout

Maps evidence before implementation:

- repo facts
- workflows
- constraints
- risks
- verification commands
- candidate next tasks

Use Scout when the request is vague, broad, recovery-oriented, or under-specified.

### Judge

Resolves ambiguity and risk:

- scope choices
- task selection
- completion claims
- whether a Worker task is safe and bounded
- final audit

Use Judge before implementation when the system could easily succeed at the wrong thing.

### Worker

Executes one bounded implementation or recovery slice.

A Worker task should define:

- `allowed_files`
- `verify`
- `stop_if`
- expected output
- receipt requirements

This is the key safety pattern: a Worker does not roam freely.

### PM

Owns the board and final responsibility.

For Yuto, this maps to:

> Yuto = PM / control tower / verification gate

Codex can be a temporary Worker, but not a permanent standing worker.

## Install and usage surface

README usage:

```bash
npx goalbuddy
```

or:

```bash
npm i -g goalbuddy
goalbuddy
```

Then restart Codex and invoke:

```text
$goal-prep
```

`$goal-prep` prepares the GoalBuddy board and prints the `/goal` command. It does not start `/goal` automatically.

Goal execution starts with:

```text
/goal Follow docs/goals/<slug>/goal.md.
```

Readiness checks from README:

```bash
codex login status
codex features enable goals
npx goalbuddy doctor --goal-ready
```

Board health check:

```bash
node ~/.codex/skills/goalbuddy/scripts/check-goal-state.mjs docs/goals/<slug>/state.yaml
```

## Repo structure observed

Important files/directories:

- `README.md`: main product and usage explanation
- `AGENTS.md`: repo development rules
- `goalbuddy/SKILL.md`: canonical Codex skill payload
- `goalbuddy/agents/`: Scout, Judge, Worker definitions
- `goalbuddy/templates/`: `goal.md`, `state.yaml`, `note.md`
- `goalbuddy/scripts/check-goal-state.mjs`: board checker
- `internal/cli/goal-maker.mjs`: npm installer CLI and compatibility command
- `plugins/goalbuddy/`: Codex plugin scaffold
- `extend/catalog.json`: optional extension catalog
- `examples/`: completed sample runs
- `package.json`: package metadata and scripts

`AGENTS.md` says runtime should remain dependency-free unless there is a strong reason, and `npm run check` should run before implementation is claimed complete.

## Extension model

GoalBuddy core is npm-stable; optional extensions live under `extend/` and can be discovered from `extend/catalog.json`.

Current catalog examples from README/source:

- `github-pr-workflow`
- `github-projects`
- `ai-diff-risk-review`
- `ci-failure-triage`
- `docs-drift-audit`
- `codebase-onboarding-map`
- `release-readiness`
- `test-gap-planner`
- `dependency-upgrade-planner`

Important design point:

> Extensions are not board truth. `state.yaml` remains authoritative.

This is useful for Yuto: extra integrations should support the board, not become new sources of truth.

## Fit with Yuto

GoalBuddy overlaps strongly with Yuto's preferred operating model:

- Yuto as PM/control tower rather than main coding agent
- one active task at a time
- bounded Worker slices
- explicit verification before completion claims
- receipts instead of vague progress updates
- Scout before Worker when scope is unclear
- Judge before completion when success is ambiguous
- state file as durable handoff artifact

Potentially useful Yuto adaptations:

1. Add `GoalBuddy-style receipts` to Yuto/Codex tasks.
2. Borrow `Scout / Judge / Worker / PM` language for complex coding lanes.
3. Use `allowed_files`, `verify`, and `stop_if` for delegated Codex work.
4. Add `likely misfire` to project briefs and system-design templates.
5. Keep `state.yaml` or similar board state for long-running implementation goals.
6. Build a Hermes-native equivalent later if repeated use proves valuable.

## Fit with Kei's AI-era legal advocacy company

GoalBuddy is not directly a legal-tech product, but its workflow pattern is useful for legal/cyber/forensic operations:

- Scout = evidence mapping / source collection
- Judge = legal/forensic risk review and scope decision
- Worker = one bounded analysis or drafting slice
- Receipt = audit trail for what was done and verified
- Board = case-prep task state

Possible future use:

- create a case-prep board for deepfake/scam incident intake
- require receipts for every evidence-handling task
- separate fact-gathering, legal-risk judgment, and drafting
- preserve stop conditions for sensitive or high-risk actions

Important: this would need a lawful, privacy-safe, evidence-preserving adaptation. Do not reuse a coding-agent workflow blindly for victim data or legal evidence.

## Comparison with current Yuto/CocoIndex work

CocoIndex helps with fresh context / derived index:

```text
Markdown KG -> derived metadata/search index
```

GoalBuddy helps with execution discipline:

```text
vague goal -> board -> bounded task -> receipt -> verification
```

They solve different layers:

- CocoIndex = recall/freshness/index layer
- GoalBuddy = work-control/PM/execution layer
- Yuto = verification/router/control layer

Together, the conceptual stack would be:

```text
Markdown KG = source of truth
CocoIndex = fresh derived context
GoalBuddy-like board = durable task execution state
Yuto = PM / verifier / safety gate
Codex = temporary Worker when needed
```

## Risks and cautions

1. Tied to Codex `/goal`
   - README says native Codex `/goal` is under-development; local runtime must have goals enabled.

2. Not installed locally
   - `goalbuddy_cli=None` at check time. This recon did not install it.

3. Could add process overhead
   - For one-change tasks, GoalBuddy itself says not to create a board.

4. Could conflict with Hermes/Yuto workflows
   - It is Codex-specific. Do not blindly import its CLI model into Hermes.

5. Side effects through installer/plugin
   - Installation touches Codex skill/plugin home. Use temp `CODEX_HOME` or disposable repo first.

6. State drift risk
   - If `state.yaml` is not maintained, the board becomes fake confidence. Receipts must be real and verified.

7. External integrations require care
   - Extensions such as GitHub Projects have approval/credential implications.

## Recommended pilot

Do not install into the real Codex home yet.

Suggested safe pilot:

1. Create a throwaway test repo.
2. Use a temporary Codex home.
3. Run install/doctor only in the temp home:

```bash
tmp=$(mktemp -d)
npx goalbuddy install --codex-home "$tmp"
npx goalbuddy doctor --codex-home "$tmp"
rm -rf "$tmp"
```

4. Inspect generated skill/templates/agents.
5. If safe, create a tiny fake goal and inspect `goal.md` / `state.yaml` only.
6. Do not enable live GitHub extensions or publish anything.

Yuto-specific pilot alternative:

Instead of installing, manually borrow the pattern into a local note or template:

- `goal.md` -> project charter
- `state.yaml` -> task board
- `Scout/Judge/Worker` -> Yuto/Codex delegation roles
- receipts -> task completion evidence

## Decision

Borrow ideas now; sandbox before installing.

Most valuable ideas:

- `likely misfire`
- `one active task`
- role-tagged tasks
- bounded Worker with `allowed_files`, `verify`, `stop_if`
- durable receipts
- final Judge/PM audit before claiming completion

Do not adopt as a required workflow for all work. Use only for broad, long-running, ambiguous, high-risk, or stale coding goals.

## Sources checked

- GitHub API repo metadata: https://api.github.com/repos/tolibear/goalbuddy
- GitHub latest commit API: https://api.github.com/repos/tolibear/goalbuddy/commits/main
- README: https://raw.githubusercontent.com/tolibear/goalbuddy/main/README.md
- `package.json`: https://raw.githubusercontent.com/tolibear/goalbuddy/main/package.json
- `AGENTS.md`: https://raw.githubusercontent.com/tolibear/goalbuddy/main/AGENTS.md
- `goalbuddy/SKILL.md`: https://raw.githubusercontent.com/tolibear/goalbuddy/main/goalbuddy/SKILL.md
- `extend/catalog.json`: https://raw.githubusercontent.com/tolibear/goalbuddy/main/extend/catalog.json
- npm latest metadata: https://registry.npmjs.org/goalbuddy/latest

Related: [[source-cocoindex]], [[source-mattpocock-skills]], [[source-ai-project-manager-agent]], [[ai-era-legal-advocacy-company-blueprint]], [[systems-thinking-general-template]]
