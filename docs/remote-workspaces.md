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

A session started with `worktree: true` gets its tree at `<main root>/.worktrees/hermes-<8hex>`, never inside another session's worktree. Placement is answered by Git, never inferred from the filesystem:

1. `git -C <workspace> rev-parse --show-toplevel` names the **checkout** the workspace is in.
2. `--git-dir` / `--git-common-dir` and the first `git worktree list --porcelain` record name the repository's **main worktree**, which is accepted only when Git, asked inside that directory, corroborates it (`--show-toplevel` equal to itself, `--git-common-dir` equal to the workspace's common dir). Git's records are the only evidence: `.git` shape, filenames, depth, index contents and cleanliness are never consulted, and no directory walk happens over user paths.

The Agent's own `_setup_worktree` runs against the selected checkout — so base, `base` refs and `.worktreeinclude` handling are the installed Agent's policy — and the tree it creates is then moved to the main root, which is the only step the WebUI adds.

**No claim is a result, not a guess.** When Git records no main worktree (a bare repository, or a `--separate-git-dir` layout whose metadata path is not a working tree), the tree stays where the Agent created it and the session payload reports `worktree_placement: "selected-unrecorded"`. A refused move — destination already exists, or the tree is locked — is reported as `worktree_placement: "move-failed"` with the tree left in place, and `worktree_placement_reason` then carries the move's **own** error rather than the classification that preceded it.

**The outcome is durable, not a log line.** `worktree_placement` and `worktree_placement_reason` are session fields: they come back in `/api/session/new` through the same `Session.compact()` projection as the path/branch/repo root and survive a reload of the session sidecar.

**The moved tree stays ownable and discoverable.**

- *Ownership lock:* the Agent locks every tree with `hermes pid=<pid>` (or `hermes session=<id> db=<state.db>`), and its stale-owner classifier recognises only that shape — anything else is kept live forever. The reason is read from the tree before the move and re-applied verbatim afterwards, on the failed-move path too; a tree that arrived unlocked is not locked by the WebUI.
- *Maintenance registry:* the Agent registers the checkout it ran in, which is the *selected* one. A tree moved under the main root is only swept if that root is registered, so the landing root is recorded through the Agent's own `worktree_registry` contract — fail-soft, and after the move has actually landed.

**Declared limitation:** with `git init --separate-git-dir=<d>/.git <checkout>`, Git records `<d>` as the main worktree and `<d>` answers every query exactly like a plain repository. A session started in a *linked* worktree of such a checkout is therefore placed under `<d>/.worktrees`. Telling that layout apart needs a filesystem predicate on the gitfile, which is the classification this contract exists to avoid.

**Known over-claim, recorded rather than guessed:** a workspace opened through a stale `.git` gitfile alias pointing at another checkout's common dir answers `--show-toplevel` with the alias while `git worktree list` names the real checkout. The destination is the same either way, but the payload reports `worktree_placement: "main-worktree"` and `repo_root` as the alias. Separating the two needs a registry-versus-toplevel cross-check whose outcome would not move a single tree, so the mismatch is stated here instead of being turned into another predicate.

