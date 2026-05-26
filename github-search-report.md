# GitHub Search Report — Hermes WebUI Fork Commits
**Date:** 2026-05-26  
**Searched:** `nesquena/hermes-webui` (upstream)  
**Fork:** `someaka/hermes-webui`

---

## Commit `4f6cc97e` — `fix: update opencode-go base_url in provider overlap tests`

Changes `api.opencode.ai/go/v1` → `opencode.ai/zen/go/v1` in `tests/test_issue1894_provider_overlap.py` (5 lines).

### Issues mentioning (5 found, all CLOSED):

| # | Title | State | Link |
|---|-------|-------|------|
| **#1894** | 401 error via opencode go deepseek model | CLOSED (2026-05-13) | [link](https://github.com/nesquena/hermes-webui/issues/1894) |
| **#772** | Error: AIAgent credential_pool kwarg | CLOSED | [link](https://github.com/nesquena/hermes-webui/issues/772) |
| **#794** | Response shows HTML from Opencode (followup to #772) | CLOSED | [link](https://github.com/nesquena/hermes-webui/issues/794) |
| **#850** | Unable to send request to opencode model (followup to #772/#794) | CLOSED | [link](https://github.com/nesquena/hermes-webui/issues/850) |
| **#2518** | New Conversation button unresponsive during cold model catalog | CLOSED | [link](https://github.com/nesquena/hermes-webui/issues/2518) |

### PRs mentioning (3 found):

| # | Title | State | Link |
|---|-------|-------|------|
| **#2204** | **Fix opencode-go custom provider overlap routing** | MERGED | [link](https://github.com/nesquena/hermes-webui/pull/2204) |
| **#2209** | stage-350: medium-risk batch (includes #2204) | MERGED | [link](https://github.com/nesquena/hermes-webui/pull/2209) |
| **#2179** | fix(config): preserve nvidia/ prefix on NVIDIA NIM | MERGED | [link](https://github.com/nesquena/hermes-webui/pull/2179) |

### Key finding:

**PR #2204** is the origin of `tests/test_issue1894_provider_overlap.py` — it was the actual fix for Issue #1894. Our fork's commit `4f6cc97e` only updated stale test assertions (the base_url string) to match the canonical URL already used in the upstream source code (`api/config.py`). The source code was already correct — only the test needed the sync.

---

## Commit `d7a915dd` — `Merge remote-tracking branch 'upstream/master'`

No mentions found. This is a standard merge commit syncing with upstream.

---

## Commit `835fb746` — `chore: clean up test_issue1894_provider_overlap — remove vestigial noqa, add docstrings`

No mentions found (created locally, pushed to fork only).

---

## Conclusion

- **Zero** issues/PRs directly reference the `someaka/hermes-webui` fork
- The upstream source code was already correct — our fork commit is a test-only sync
- PR #2204 is the origin of the test file; our change keeps it aligned
- All related upstream issues are CLOSED and the fix is MERGED
