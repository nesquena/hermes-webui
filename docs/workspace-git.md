# Workspace Git controls

Workspace Git controls let the browser inspect Git state for the active session workspace. By default,
WebUI can read status, list branches, show diffs, fetch remote refs, and generate commit-message
suggestions. Actions that modify the repository, index, or worktree are disabled unless the WebUI
process is started with `HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE=1`.

> **Trust model - read this first.** Once mutating Git actions are enabled, a browser action can run
> Git commands inside a mounted workspace. Some Git commands can also run repository hook code from
> `.git/hooks/` or a configured `core.hooksPath`. That hook code runs as the WebUI process user, with
> the WebUI process permissions and environment. Treat hooks that download or execute code as code
> execution by the WebUI process user.

## What works by default

Without `HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE=1`, WebUI can:

- show repository status
- list branches
- show file diffs
- fetch from the configured remote
- generate commit-message suggestions

Fetch may update remote-tracking refs, but it does not change the worktree, merge branches, create
commits, or push changes.

Commit-message generation may send staged or selected diff context to the configured model provider.
The UI labels this before generation.

Diffs for untracked files are size checked before WebUI reads file contents. Large or binary files
return metadata instead of inline diff text.

## What requires explicit enablement

These actions are blocked unless `HERMES_WEBUI_WORKSPACE_GIT_DESTRUCTIVE=1` is set:

- stage and unstage
- discard changes
- commit and selected-file commit
- pull and push
- checkout
- branch switching that parks and restores local changes

Leave the flag unset for deployments where WebUI should only inspect mounted workspaces. Set it only
when browser users are trusted to modify those repositories from WebUI.

When the flag is enabled, branch switching parks and restores local changes automatically. If the
branch being left has local changes, WebUI parks those changes in a WebUI-owned Git stash, switches
branches, and then restores any WebUI-owned stash for the branch being entered. If Git cannot restore
the stash cleanly, WebUI leaves the stash in place and reports the restore failure instead of dropping
it.

## Workspace and path scope

The browser does not send an arbitrary repository path. Git requests carry a session id and, when
needed, workspace-relative file paths. The server resolves the session workspace, checks each path
against that workspace, and then builds Git pathspecs from the checked paths.

Git commands run through `subprocess.run` with `shell=False`. Local status and diff commands use a 5
second timeout. Remote operations such as fetch, pull, and push use a 60 second timeout.

Before any Git subprocess starts, WebUI removes inherited `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`, and injected
`GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*` values from the environment. It also removes inherited
`GIT_ASKPASS`, `SSH_ASKPASS`, `GIT_SSH`, and `GIT_SSH_COMMAND` values, then sets
`GIT_TERMINAL_PROMPT=0` so remote authentication failures fail fast instead of blocking on an
interactive prompt. Those variables can redirect Git to a different repository, inject config, or run
helper commands, so WebUI does not trust them from the parent process.

`GIT_INDEX_FILE` is the intentional exception. Selected-file commits use a temporary index so WebUI
can commit only the requested files, then remove the temporary index afterward.

## Coordination with active runs

Mutating Git actions are rejected while the same session has a live stream. The API returns
`active_stream` instead of racing a running agent that may be writing files in the same workspace.

Mutating Git actions also take a per-repository lock. If another Git mutation is already running for
that repository, the API returns `operation_in_progress`.

## Hook and remote behavior

WebUI does not bypass Git hooks. Hook code may come from `.git/hooks/` or from a configured
`core.hooksPath`.

Commit actions run normal commit hooks such as `pre-commit`, `commit-msg`, and `post-commit`. Push can
run `pre-push`. Pull uses `--ff-only`, so it does not create a merge commit, but it is still a Git
operation running under the WebUI process.

Push keeps Git's normal non-fast-forward protection and reports non-fast-forward rejection separately
from general Git failures.

If a hook fails, the API returns a structured Git error instead of hiding the failure. Other classified
failures include authentication errors, missing upstream branches, conflicts, dirty worktrees, invalid
refs, missing Git binaries, and timeouts.

Repository-local credential helpers and askpass commands are disabled for workspace Git operations.
Private HTTPS remotes that depend on a stored credential helper may fail to fetch, pull, or push from
WebUI; use an SSH remote or another externally authenticated transport for those workflows.
