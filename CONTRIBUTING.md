# Contributing to Hermes WebUI

Thanks for contributing.

Hermes WebUI is intentionally simple to work on: Python on the server, vanilla JS in the browser, no build step, no bundler, no frontend framework. The best pull requests preserve that simplicity while solving a real problem cleanly.

## Two Paths to a Strong Pull Request

### Path 1: Small, Focused Changes

This is the fastest path to review and merge.

- Fix one clear bug or add one tightly scoped improvement
- Touch the fewest files you can
- Avoid drive-by refactors mixed into functional changes
- Run the relevant tests locally before opening the PR
- Keep the PR description concise and specific

These are the changes that are easiest to review and safest to merge quickly.

### Path 2: Bigger Changes

If you want to change architecture, reshape a workflow, add a substantial UI feature, or alter core behavior, align on direction first.

- Open an issue, start a discussion, or open a draft PR early
- Explain the problem you are solving, not just the implementation you want
- Call out tradeoffs, migration risk, and any alternatives you considered
- Keep the final PR easy to review by separating unrelated work

Large changes are welcome, but surprise rewrites are hard to review well.

## What We Expect in Every PR

### 1. One Logical Change Per PR

Keep each PR focused. A small related group of fixes is fine. A bug fix plus a CSS cleanup plus a refactor plus a docs rewrite is not.

### 2. Local Verification

Run the test suite locally:

```bash
pytest tests/ -v --timeout=60
```

CI also runs this suite on Python `3.11`, `3.12`, and `3.13`.

If your change affects browser behavior, also run the relevant manual checks from [TESTING.md](TESTING.md).

### 3. Clear PR Description

There is currently no PR template in this repo, so include the important sections yourself:

- Thinking Path
- What Changed
- Why It Matters
- Verification
- Risks / Follow-ups
- Model Used

If the change is user-visible, include screenshots or a short video.

For UI or UX changes, before/after images are required. PRs that change the interface or interaction flow without before/after images will likely be ignored, or closed in a regular maintainer sweep without review.

### 4. AI Usage Disclosure

If AI helped produce the change, say so in the PR description.

Include:

- Provider
- Exact model name or ID
- Any notable mode or tool use that mattered

If no AI was used, write: `None — human-authored`.

### 5. Keep the Docs Honest

If your change alters behavior, architecture, testing, setup, or user-facing workflows, update the relevant docs in the same PR.

Common files:

- [README.md](README.md) for setup, usage, and contributor-facing commands
- [ROADMAP.md](ROADMAP.md) for shipped features and sprint history
- [ARCHITECTURE.md](ARCHITECTURE.md) for implementation details and design constraints
- [TESTING.md](TESTING.md) for manual and automated verification guidance
- [CHANGELOG.md](CHANGELOG.md) when maintainers want release-note-ready entries

## Project-Specific Guidelines

### Preserve the Design Constraints

Hermes WebUI is deliberately:

- No build step
- No bundler
- No frontend framework
- Easy to modify from a terminal

Do not introduce new infrastructure or dependencies unless the gain is clear and the tradeoff is justified.

### Match the Existing Shape of the Codebase

- Server logic belongs in `api/` with `server.py` staying thin
- Frontend behavior belongs in the existing `static/*.js` modules
- Prefer extending current patterns over introducing parallel abstractions
- Keep changes legible to future contributors working directly from the repo in a terminal

### Be Careful With User-Facing Changes

This project is heavily UI-driven. If you change interaction flows, session behavior, workspace browsing, onboarding, or mobile layouts:

- test the happy path
- test reload behavior where relevant
- test narrow/mobile layouts where relevant
- include before/after images in the PR

### Security and Safety Matter

This app can expose workspace contents, run agent actions, and optionally sit behind a reverse proxy or Docker deployment. Treat auth, path handling, uploads, streaming, and environment handling as high-risk areas.

If your PR touches security-sensitive behavior, say so explicitly in the PR description and explain how you verified it.

## Writing a Good PR Message

Start with a short Thinking Path that explains the chain from project goal to the specific fix.

Example:

> - Hermes WebUI aims for near 1:1 parity with the Hermes CLI in a browser
> - Long-running chat turns rely on SSE streaming and session recovery
> - Reloading during an in-flight turn can leave the UI in an inconsistent state
> - The bug was that recovered sessions restored messages but not the live stream state
> - This PR fixes the recovery path so in-flight turns reconnect cleanly after reload
> - The benefit is that users can refresh or reconnect without losing visibility into active work

Another example:

> - Hermes WebUI is intentionally a simple Python + vanilla JS application
> - The right panel is used for workspace browsing and previews
> - On mobile, panel state changes need to be obvious and touch-friendly
> - The existing close affordance was inconsistent with the bottom-nav flow
> - This PR fixes the mobile panel close behavior and aligns it with the current navigation model
> - The result is fewer dead-end UI states on phones

After that, cover:

- what you changed
- why you changed it
- how you verified it
- what risks remain

## Review Tips

Want the smoothest review?

- Keep diffs tight
- Name things clearly
- Avoid unnecessary rewrites
- Add short comments only where the code would otherwise be hard to follow
- Respond directly to review feedback and update the PR description if the scope changes

## Development References

- [README.md](README.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [TESTING.md](TESTING.md)
- [ROADMAP.md](ROADMAP.md)
- [SPRINTS.md](SPRINTS.md)

Questions are best raised early, before a large change is finished.
