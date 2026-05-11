# Operator handoff: chat-pinned workspaces

Status: verified locally after the workspace UX update.

## What changed

Workspaces are now pinned to the chat/session instead of acting like one global switch. Choosing a workspace from the dropdown no longer silently rewrites the active chat. Each saved workspace row exposes explicit actions:

- New chat: start a new conversation in that workspace.
- Move chat: move the current conversation to that workspace.
- Use for new chats: update the default workspace used by future chats.
- Show sessions: filter the sidebar to chats already pinned to that workspace.

The workspace chip/sidebar label follows the active chat. Switching between chats restores each chat's own workspace.

## How to use it

### New chat in workspace

Use this when you want to work in another project without disturbing the current chat.

1. Open the workspace dropdown from the composer or sidebar.
2. Find the target workspace.
3. Click New chat.

Expected result: WebUI creates a new session with that workspace and focuses the message box. The previous chat keeps its original workspace and can keep running.

### Move current chat

Use this only when the current conversation should be re-pinned to another workspace.

1. Open the workspace dropdown.
2. Find the target workspace.
3. Click Move chat.

Expected result: the active session's workspace is updated, the file browser reloads from the new workspace, and future turns in that chat run there.

If there is no active chat, Move chat falls back to creating a new chat in the chosen workspace.

## While another chat is running

A running chat no longer blocks unrelated workspace actions. You can create or move a different idle chat while another session is streaming.

Only moving the busy chat itself is blocked. In that case the backend returns 409 and the UI reports that the session is still streaming; wait for the turn to finish, cancel it, or switch to another idle chat before moving.

## Update compatibility rules and caveats

- This update is compatible with existing sessions because workspace is already stored per session.
- Existing saved workspaces remain valid; the dropdown now exposes separate actions instead of making the row click mutate the active chat.
- The previous switchToWorkspace caller path is kept as a compatibility wrapper: blank-page calls create a new chat, active-session calls route through Move chat.
- Use for new chats writes the profile/default last-workspace preference only. It does not move the current chat.
- Update simulation was non-destructive: no real pull was applied to the live working copy during verification.
- The verified working copy had preserved local modifications and no unresolved conflicts or conflict markers. At final ops check, master was behind origin/master; merge/update should still preserve local edits and resolve normally.

## Test and smoke evidence

Automated focused suite passed:

```bash
HOME=/Users/caseymoore \
HERMES_WEBUI_AGENT_DIR=/Users/caseymoore/.hermes/hermes-agent \
HERMES_WEBUI_PYTHON=/Users/caseymoore/.hermes/hermes-agent/venv/bin/python \
/Users/caseymoore/.hermes/hermes-agent/venv/bin/python -m pytest -q \
  tests/test_workspace_selector_actions.py \
  tests/test_workspace_system_message.py \
  tests/test_workspace_panel_session_list.py \
  tests/test_workspace_display_prefix.py \
  tests/test_session_import_workspace_validation.py \
  tests/test_parallel_session_switch.py \
  tests/test_1062_busy_input_modes.py
```

Result: 90 passed, 0 failed.

Manual/browser smoke evidence:

- Local service restarted safely after active streams reached zero.
- GET / served `HermesWebUI/0.51.45-dirty` with HTTP 200.
- Browser loaded `http://127.0.0.1:8787/` with title `Hermes`.
- Static assets loaded the expected functions/labels: `newChatInWorkspace`, `moveCurrentChatToWorkspace`, `filterSessionsByWorkspace`, `workspace_action_new_chat`, `workspace_action_move_chat`.
- Chat A was started in `/Users/caseymoore/Projects/hermes-webui` and left running.
- Chat B was created in `/Users/caseymoore/Projects/open-brain-local`, updated while Chat A was running, then moved to `/Users/caseymoore/Projects/TradingAgents`.
- Returning to Chat A restored `/Users/caseymoore/Projects/hermes-webui`.
- Attempting to move the still-busy Chat A returned 409 with: `Session is still streaming; wait for the current turn to finish before moving workspace.`

Update compatibility simulation passed:

- Temp detached worktree from local HEAD.
- Applied tracked diff plus untracked workspace tests.
- Stashed with `stash -u`.
- Pulled `origin/master` with `--ff-only`.
- Popped the stash.
- Ran `git diff --check` and conflict-marker scan.

Result: PASS_NO_CONFLICTS.

## Rollback/update notes

Rollback is straightforward: revert the workspace UX patch and restart the WebUI service. Existing sessions should still load because the feature uses existing per-session workspace data; no migration rollback is required.

For update/merge:

1. Preserve local modifications; do not run `git reset --hard` or `git checkout .`.
2. Run the focused pytest command above with the explicit `HOME`, `HERMES_WEBUI_AGENT_DIR`, and `HERMES_WEBUI_PYTHON` environment variables.
3. Restart the WebUI service only after active streams/runs are clear.
4. Re-smoke: load `/`, create New chat in workspace while another chat runs, move an idle chat, confirm moving the busy chat is rejected with 409.
