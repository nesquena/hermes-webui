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

When a session starts with worktrees enabled, its git worktree is created beneath the **main working tree** of the repository (the primary checkout's `.worktrees/`) whenever that main checkout can be resolved — never beneath the worktree the session happened to start in.

- **Why:** a workspace that is itself a linked worktree (the common case once `last_workspace.txt` carries a previous session's tree) would otherwise nest the new tree *inside* the current one. A nest is invisible to every pruner — `hermes worktree prune` only audits direct children of `<repo>/.worktrees/` — so it leaks disk permanently.
- **Resolution:** `find_main_worktree_root()` resolves the working tree that *owns* the common git dir. The ordinary `<main>/.git` and linked-worktree shapes resolve directly; `--separate-git-dir` checkouts and submodules resolve through the `gitdir:` gitfile or `core.worktree`, and are validated as real checkouts. The fallback refuses a parent that does not own the common dir — starting a session in the wrong project is worse than leaving the path unresolved. A metadata directory (`<storage>/.git`) is crowned as a checkout **only** when the claim search provably finished: no unreadable level, no depth or budget truncation, and no directory skipped *outside* that candidate. A skipped directory inside the candidate (the repository's own `node_modules`, `.venv`, `.worktrees`) is a sound stop; one outside it (`<base>/node_modules/pkg/checkout`, `<base>/vendor/otherrepo/…`) hides a place the owning checkout can live, so the layout is left unresolved instead of being promoted. The search region is fixed: the metadata's parent and its parent with bounded descent, then the immediate children of each ancestor up to the filesystem root. Two limits follow, deliberately: a claim nested below a *child* of an ancestor above the metadata's grandparent (`<base>/y/primary` beside `<base>/x/storage/.git`), and a claim nested deeper than one level inside another repository's tree, are outside the region and do not veto the plain layout. Widening either would turn the user's own `~/.venv` or `~/node_modules` into skips outside the candidate and leave every project under that level unresolved.
- **Placement is the only job the main root does.** The base commit and the `.worktreeinclude` manifest come from the checkout the session started in (`find_git_repo_root(workspace)`), not from the primary: a session opened in a linked checkout is built from that branch's HEAD and inherits that checkout's local settings, while the tree still lands under `<main>/.worktrees/` so the pruner can see it. Includes that only the primary lists are not copied into the session tree.
- **Ownership never depends on uncommitted state — except for the one ambiguous pair.** A working tree owns its repository whether its files are clean, modified, staged, entirely rewritten or partially deleted: for an unambiguous candidate no content, `git status` or per-path existence comparison decides ownership. A checkout with every tracked file edited still resolves, a same-content rewrite or a bare `touch` changes nothing, and deleting a tracked file revokes nothing. Ownership comes from a gitfile claim on the metadata, or — for a plain repository — from the recorded `<tree>/.git` layout, and that layout is trusted only when the gitfile search finished without being truncated **and** without skipping anything outside the candidate; otherwise the ownership stays unknown rather than promoting a `--separate-git-dir` storage directory, which has exactly that shape, into a checkout. The single exception is a gitfile claimant and the plain candidate **mutually containing each other** — a checkout nested in its own storage dir (`storage/pkg/checkout`), or an alias wrapping the real checkout (`x/.git -> x/primary/.git`). Those two layouts are identical on disk and mean opposite things, so `git status` (untracked files ignored) decides which of the two still holds the committed tree; both holding it, neither holding it, or an unreadable status leaves ownership unknown instead of guessing.
- **Unresolved layouts keep the old behaviour.** When the main checkout cannot be recovered, `create_worktree_for_workspace()` falls back to `find_git_repo_root()` and the tree is placed under the current workspace's toplevel. In that case a session started inside a linked worktree still nests, and the nest is not reclaimable by the pruner — resolution is best-effort, not a guarantee.
- **Existing trees are not moved.** Session trees created before this behaviour keep their location; only new sessions use the main-checkout placement.
