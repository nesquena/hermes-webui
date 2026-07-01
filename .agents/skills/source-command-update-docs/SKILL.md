---
name: "source-command-update-docs"
description: "Update all markdown files (README, ROADMAP, CHANGELOG, SPRINTS) to reflect current state"
---

# source-command-update-docs

Use this skill when the user asks to run the migrated source command `update-docs`.

## Command Template

# Update All Markdown Documentation

Bring all docs up to date with the current codebase state.

## Process

### 1. Gather current state
Run these in parallel:
- `wc -l static/{ui,workspace,sessions,messages,panels,boot,commands}.js | tail -1` (JS lines)
- `wc -l api/*.py | tail -1` (Python lines)
- `wc -l static/style.css static/index.html server.py` (other files)
- `pytest tests/ --co -q 2>&1 | tail -3` (test count)
- `pytest tests/ --timeout=60 -q 2>&1 | tail -3` (pass/fail)
- `git tag --list 'v*' --sort=-v:refname | head -3` (latest version)
- `ls tests/test_*.py | wc -l` (test file count)

### 2. Update README.md
- Features section: ensure all shipped features are listed
- Architecture tree: update line counts for all files
- Test count: update "Current count: **X tests**"
- Docker section: should mention GHCR pre-built images
- Any new sections needed (e.g. Tailscale was added for mobile access)

### 3. Update ROADMAP.md
- Header: version, test count, date
- Sprint History table: add row for latest sprint if missing
- Architecture Status table: update line counts, add new rows if needed
- Feature Parity Checklist: check off completed items, add new ones
- User Requested Features: mark shipped items, add new requests
- Advanced/Future: add any new deferred features

### 4. Update CHANGELOG.md
- Add entry for current version if missing
- Include Features, Bug Fixes, Architecture subsections as needed
- Footer: update version and test count

### 5. Update SPRINTS.md
- Header: current version and test count
- Footer: current version, test count, next sprint
- If a new sprint was completed, add the full sprint section

### 6. Create PR
- Branch: `docs/update-all-markdown-vX.Y`
- Commit message: "docs: update all markdown to vX.Y state"
- PR body: list what was updated in each file
- For docs-only PRs: can merge immediately after a quick review
