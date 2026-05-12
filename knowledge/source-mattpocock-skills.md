# Source: mattpocock/skills

Checked: 2026-05-02
Source: https://github.com/mattpocock/skills
Local inspection clone: `/tmp/mattpocock-skills` at commit `b843cb5`
Related: [[sources]] [[workflows]] [[memory-system]] [[yuto-recursive-context-operator]] [[hermes-agent-skill-authoring]]

## Conclusion

`mattpocock/skills` is highly relevant as a reference library for agent skill design, especially for engineering workflows. Yuto should borrow patterns, not bulk-install or copy into Hermes blindly.

Best ideas to adapt:
- small composable skills instead of one giant process;
- skill setup that writes per-repo agent context (`docs/agents/*`, `CONTEXT.md`, ADR conventions);
- strong debugging/TDD loops focused on feedback signals;
- domain language / shared glossary to reduce token waste and misalignment;
- separating promoted, personal, and deprecated skills.

## Source Facts

Repo metadata from GitHub API on 2026-05-02:
- Full name: `mattpocock/skills`
- Description: `Skills for Real Engineers. Straight from my .claude directory.`
- Stars: `51949`
- Forks: `4337`
- Open issues: `7`
- Default branch: `main`
- License: `MIT`
- Latest inspected commit: `b843cb5` on 2026-04-30, message: `Add structured sections for 'what-to-do' and 'supporting-info' in SKILL.md`
- Primary language reported by GitHub: `Shell`

Repository structure observed locally:
- `README.md`: explains purpose, quickstart, and reference list.
- `.claude-plugin/plugin.json`: lists 12 promoted skills for Claude plugin loading.
- `skills/`: 22 `SKILL.md` files total.
- Buckets: `engineering`, `productivity`, `misc`, `personal`, `deprecated`.
- `scripts/link-skills.sh`: symlinks all repo skills into `~/.claude/skills`.
- `scripts/list-skills.sh`: lists all `SKILL.md` files.

README claims:
- Quickstart uses `npx skills@latest add mattpocock/skills`.
- Skills are positioned as small, adaptable, composable alternatives to heavier process frameworks.
- Main failure modes targeted: agent misalignment, verbosity/lack of shared language, code not working due weak feedback loops, architecture degrading into a ball of mud.

## Skill Patterns Worth Borrowing

### Grilling before building

Relevant skills:
- `grill-me`
- `grill-with-docs`

Pattern:
- Interview the user one question at a time.
- Provide a recommended answer with each question.
- Explore codebase when the answer is discoverable instead of asking.
- For code projects, update shared language docs and ADRs as decisions crystallize.

Yuto implication:
- This aligns with Kei's brake-check preference, but Yuto should not over-question. Use when ambiguity materially changes implementation or product direction.

### Shared language / domain context

Relevant source files:
- `CONTEXT.md`
- `skills/engineering/grill-with-docs/SKILL.md`
- `skills/engineering/setup-matt-pocock-skills/SKILL.md`

Pattern:
- Maintain a concise glossary/domain model for each repo.
- Use ADRs only for hard-to-reverse, surprising, tradeoff-heavy decisions.
- Avoid coupling `CONTEXT.md` to implementation details; keep domain-expert language.

Yuto implication:
- For future code/product projects, a `CONTEXT.md` + `docs/adr/` pattern can reduce Yuto/agent verbosity and misalignment. For Yuto's own KG, `knowledge/rules.md`, `memory-system.md`, and `workflows.md` already play this role.

### Debugging as feedback-loop construction

Relevant skill:
- `skills/engineering/diagnose/SKILL.md`

Pattern:
- Phase 1 is building a deterministic pass/fail loop.
- Then reproduce, generate falsifiable hypotheses, instrument, fix, regression-test, and clean up.
- If no feedback loop exists, stop and say what evidence is missing instead of guessing.

Yuto implication:
- Strongly matches Yuto's `verify-before-claim` failure repairs. Could inform future patch to `systematic-debugging` if our existing skill is weaker.

### Setup skill as repo-specific config

Relevant skill:
- `skills/engineering/setup-matt-pocock-skills/SKILL.md`

Pattern:
- One setup skill configures issue tracker, triage labels, and domain docs.
- Writes `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, and `docs/agents/domain.md`.
- Edits existing `CLAUDE.md` or `AGENTS.md`, but does not create the wrong one if the other exists.

Yuto implication:
- Useful for future project onboarding: instead of stuffing project-specific workflow into active memory, create per-repo docs that agents read.

### Skill lifecycle buckets

Relevant file:
- `CLAUDE.md`

Pattern:
- Promoted skills must be referenced in README and plugin manifest.
- `personal/` and `deprecated/` skills are excluded from plugin manifest.

Yuto implication:
- Yuto could mirror this idea in Hermes skills: active/core skills, experimental skills, personal/local-only skills, deprecated skills. Do not load everything as core just because it exists.

## Fit With Yuto / Hermes

Adopt conceptually:
- `diagnose` feedback-loop-first discipline.
- `grill-with-docs` as a product/code alignment mode.
- `setup-*` skill pattern for project onboarding.
- `CONTEXT.md` and ADR discipline for future codebases.
- `personal` / `deprecated` skill lifecycle separation.

Do not directly install into Yuto core yet:
- The repo targets Claude Code / `.claude/skills` and `skills.sh`, not Hermes skill management directly.
- Skill frontmatter is minimal and lacks Hermes peer metadata such as `version`, `author`, `license`, and `metadata.hermes.tags`.
- Some skills reference Claude-specific assumptions and files.
- Bulk symlinking into `~/.claude/skills` is not useful for Hermes and could create drift/noise.

Potential Hermes adaptations:
- Create a Yuto-specific `project-context-onboarding` skill inspired by `setup-matt-pocock-skills` after it is needed in a real project.
- Patch existing `systematic-debugging` only if comparison shows it lacks feedback-loop-first language.
- Add a workflow note for future project onboarding: issue tracker, triage labels, domain docs, ADRs.

## Risks / Cautions

- Popularity is not proof of fit. Stars and forks are adoption signals, not quality or safety proof.
- Instructions are written for another agent runtime; direct copying can create tool/path mismatch.
- Some workflows encourage interactive interviewing; Yuto must balance this with Kei's preference for safe autonomy and not ask low-value questions.
- `scripts/link-skills.sh` can modify `~/.claude/skills`; do not run on Kei's machine unless explicitly requested.
- The repo includes personal/deprecated skills that should not become core workflow without review.

## Recommended Next Action

No install now.

Use it as a design reference. Next time Kei starts a real code/product project, consider creating a small project onboarding loop:
1. Inspect repo docs and issue tracker.
2. Create or update `CONTEXT.md` / `docs/adr/` / `docs/agents/*` only if useful.
3. Run a grilling/brake session for ambiguous product decisions.
4. Use feedback-loop-first debugging/TDD for implementation work.

Canary questions:
- Did Yuto choose a small relevant skill instead of a giant process?
- Did Yuto create project-local context instead of bloating active memory?
- Did debugging start with a deterministic feedback loop?
- Did Yuto avoid installing Claude-specific tooling into Hermes without a reason?
