# Remote Terminal Workspaces

Architecture contract and path-resolution semantics for remote terminal profiles (SSH, Docker) in Hermes WebUI.

---

## 1. Overview

When a Hermes profile is configured with a remote terminal backend (e.g. `terminal.backend: "ssh"` or `"docker"`), its working directory (`terminal.cwd`) lives on the remote target host rather than the local WebUI host filesystem.

On hosts such as macOS, local path resolution via `Path.resolve()` or `os.path.realpath()` expands synthetic firmlinks (e.g. rewriting `/home/<user>` to `/System/Volumes/Data/home/<user>`). Because the target-side path does not exist on the local macOS server, unconstrained local resolution causes runtime validation failures and session corruption.

---

## 2. Path Resolution Contract

1. **Target-side POSIX Path Preservation:**
   - Any valid POSIX path at or beneath a remote profile's configured `terminal.cwd` is preserved verbatim as a `Path` object without calling host-local `Path.resolve()`.
   - Traversal escapes (`..`), null bytes (`\0`), and blocked system roots (`/etc`, `/usr`, `/var`, `/sys`, `/proc`, `/dev`) continue to be strictly rejected.

2. **Profile Isolation & Boundary Enforcement:**
   - Remote path recognition is **strictly scoped to the target/active profile**.
   - If an active profile has a local backend, target-side remote paths belonging to inactive named profiles are **not** treated as remote for the active local profile.
   - For local profiles, workspace addition (`/api/workspaces/add`) enforces host-local directory existence and permissions.
   - For remote profiles, workspace addition validates against the profile's remote `terminal.cwd` and skips host-local directory creation (`mkdir`).

3. **Session & Streaming Lifecycle:**
   - `Session.__init__` and `Session.load` normalize session workspace paths through `_resolve_path(..., profile=profile)`, preserving target-side paths for remote profiles and preventing corruption of `session.workspace` or `session.created_workspace`.
   - Streaming execution (`_run_agent_streaming`) and multimodal asset root resolution preserve the remote session workspace when updating runtime run state.

---

## 3. Session Worktree Placement

When a session starts with worktrees enabled, its git worktree is created beneath the **main working tree** of the repository (the primary checkout's `.worktrees/`), never beneath the worktree the session happened to start in.

- **Why:** a workspace that is itself a linked worktree (the common case once `last_workspace.txt` carries a previous session's tree) would otherwise nest the new tree *inside* the current one. A nest is invisible to every pruner — `hermes worktree prune` only audits direct children of `<repo>/.worktrees/` — so it leaks disk permanently.
- **Resolution:** `find_main_worktree_root()` resolves the working tree that *owns* the common git dir. The ordinary `<main>/.git` and linked-worktree shapes resolve directly; `--separate-git-dir` checkouts and submodules resolve through the `gitdir:` gitfile or `core.worktree`, and are validated as real checkouts. A metadata directory is never treated as a checkout, and the fallback refuses a parent that does not own the common dir — starting a session in the wrong project is worse than leaving the path unresolved.
- **Existing trees are not moved.** Session trees created before this behaviour keep their location; only new sessions use the main-checkout placement.
