# Managed worktree audit (V1)

Hermes WebUI V1 provides an observation-only command for Git worktrees created
for WebUI sessions. It reads session sidecars, checks process and WebUI
activity, and classifies current Git state. It never removes a worktree,
deletes a branch, edits a sidecar, refreshes a remote reference, or otherwise
changes repository state.

V1 is approved for a seven-day observation period only. There is no mutation
mode.

## Run an audit

One repository filter is required:

```bash
python scripts/worktree_gc.py --repo /path/to/repository
```

Useful options include:

```bash
python scripts/worktree_gc.py \
  --repo /path/to/repository \
  --profile default \
  --min-age-days 14 \
  --target-ref origin/master \
  --json
```

`--dry-run` may be supplied explicitly, but observation-only behavior is always
used.

Defaults:

- `--profile`: `default`
- `--min-age-days`: `7` (values below `1` are rejected)
- `--target-ref`: `origin/master`
- `--state-dir`: `HERMES_WEBUI_STATE_DIR`, otherwise
  `${HERMES_HOME:-~/.hermes}/webui`
- `--health-url`: `http://127.0.0.1:${HERMES_WEBUI_PORT:-8787}/health`
- `--report-path`:
  `${XDG_STATE_HOME:-~/.local/state}/hermes-webui/worktree-gc/report.json`;
  another user-state path is preferred if needed, and the operating system's
  temporary directory is the last fallback. Defaults stay outside the source
  checkout.

For a scheduled audit, pass explicit `--repo`, `--state-dir`, `--health-url`,
and `--report-path` values when the scheduler environment may differ from an
interactive shell. Prevent overlapping invocations at the scheduler level.

## Fail-closed workflow

The audit discovers `<state-dir>/sessions/*.json` without importing the WebUI
runtime or changing the active profile. It ignores `_index.json`, backup names,
and in-progress temporary names. A session without a `profile` field is treated
as `default` for compatibility; an explicit null or different profile is not.
Repository and workspace comparisons use canonical path containment, never
string prefixes.

For each managed worktree, the audit also indexes every session in the selected
profile that has a canonical `workspace`. A session is linked when its
workspace equals the worktree path or is a descendant of it. This catches
ordinary, duplicate, and forked sessions that share a worktree without copying
the `worktree_*` metadata. The report aggregates only their session IDs; titles,
messages, attachment names, and content are never emitted.

A worktree reaches Git classification only when all of these facts are
confirmed:

1. Every linked session is explicitly archived.
2. The managed worktree path, branch, and repository root are valid.
3. The canonical repository root equals `--repo`.
4. The managed worktree is old enough. `worktree_created_at` is authoritative;
   when absent, `updated_at` is the conservative fallback.
5. Every linked session has a valid `updated_at` that reaches the age threshold.
6. No linked session records an active stream.
7. No linked session records pending text, attachments, or a pending-start
   timestamp.
8. A complete `/proc` scan finds no process whose canonical cwd equals or is
   below the worktree path.
9. The health endpoint responds successfully with `active_runs == 0`.

The creation-time Hermes worktree lock PID is bookkeeping, not independent
proof of activity. Activity is established by the stream, pending, health, and
cwd checks above.

The Git classifier uses only local, read-only evidence. It validates the
configured target ref, lists registered worktrees, checks tracked and untracked
status, and separately enumerates ignored paths with NUL-delimited output. It
never reads ignored file contents. A present ignored path produces
`KEEP_IGNORED_FILES` and an `ignored_count`.

Missing or invalid dates, unreadable or malformed sidecars, contradictory
duplicate records, invalid workspace paths, an incomplete process scan, a
failed health probe, a failed Git command, a timeout, oversized output, or
non-parseable NUL output are uncertainties. Uncertainty keeps the worktree and
makes the audit blocking. A local target ref is not refreshed by this command;
operators who need a newer comparison point must update it outside the audit
under their normal repository controls, then rerun the audit.

## Report

Every report has `mode="dry-run"` and `collection_requested=false`. It has no
per-candidate mutation result and makes no claim that a worktree or branch was
changed.

The JSON report is written through a mode-`0600` temporary file in the
destination directory. The file is flushed and synced, atomically replaced,
then the parent directory is synced on supported POSIX systems. `--json` prints
the same report to stdout; otherwise stdout contains a one-line summary and the
report path.

Reports contain only operational metadata: session IDs, profile, worktree
identity, normalized timestamps and age, verdicts, reasons, health/process
counters, and Git classification counts. They do not include conversation
titles, messages, attachment names or contents, credentials, Git stderr, or
ignored file names and contents.

## Exit codes

- `0`: the audit completed with no blocking item or global uncertainty.
- `2`: activity, work in progress, ignored/untracked/dirty data, unique work,
  or uncertainty kept at least one item.

Argument errors use the command-line parser's standard exit `2`. Unhandled
execution failures remain standard non-zero process failures. V1 has no partial
mutation status or dedicated partial-mutation exit code.

## After the observation period

Enabling automated removal after the seven-day observation period requires a
new design and a separate review. That future design must provide an atomic or
compare-and-swap authority boundary across session identity, workspace
references, runtime activity, Git branch identity, and the final filesystem
action. V1 audit evidence is not authorization to add mutation to this command.
