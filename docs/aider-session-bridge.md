# Aider session bridge

Aider keeps one Markdown transcript per project: `.aider.chat.history.md`, written at the
project's git root. There is no central Aider store to scan, so the WebUI bridges exactly ONE
bounded root and lists those transcripts in the sidebar as **read-only** sessions.

This page covers what the bridge discovers, how to point it somewhere else, and what the
read-only limits mean.

## Discovery

The bridge resolves a single scan root and walks it, bounded at every step:

1. `HERMES_WEBUI_AIDER_SCAN_DIR` if set (see [the override](#the-override)).
2. Otherwise the **active profile's** `last_workspace` git root — the workspace directory is
   walked upward to the nearest ancestor containing `.git`.

The active profile's own binding is used (not an ambient resolve), so a conversation listed in
the sidebar is the same conversation the detail view opens, and a named profile never picks up
another profile's workspace.

Under a test run (`HERMES_WEBUI_TEST_STATE_DIR` is set) the fallback resolves to nothing, so a
developer's real repository is never read during tests.

### What the walk refuses

The walk is defensive by construction, matching the shipped Claude Code bridge:

| Guard | Limit |
| --- | --- |
| Symlinked scan root | refused outright |
| Symlinked entries | never traversed, never yielded |
| Files over `AIDER_MAX_FILE_BYTES` (10 MiB) | skipped |
| Directory depth | 8 levels |
| Visited directories | 2 000 |
| Entries read per directory | 2 000 |
| Files yielded | 200 |
| Messages per transcript | 1 000 (newest) |

Containment is re-checked **at the point of use**, not only during the scan: the transcript is
opened through the scan root's own directory handles with `O_NOFOLLOW` on every component, so a
file or directory swapped for a symlink between the scan and the read cannot redirect the read
outside the root. The read is also capped on the opened file, so a file that grew after the size
check is not read without limit.

### The transcript window

Aider transcripts are **append-only**: the file only ever grows, and new turns are appended at
the end. The 1 000-message window therefore keeps the **newest** messages — a long-running
conversation keeps showing what just happened instead of freezing on its first thousand turns.
The sidebar title stays anchored to the **first** user turn of the file, so it does not drift as
the window slides.

## The override

`HERMES_WEBUI_AIDER_SCAN_DIR` pins the scan root explicitly:

```bash
HERMES_WEBUI_AIDER_SCAN_DIR=/path/to/project python3 bootstrap.py
```

- `~` is expanded.
- The path is **not** followed through symlinks: a symlinked root is refused, exactly like the
  default resolve.
- The value is a *root*, not a file. The bridge looks for
  `.aider.chat.history.md` underneath it.

Reach for the override when the workspace is not a git repository (there is no `.git` to walk up
to), or when several roots are in play and you want discovery pinned rather than derived.

## Read-only limits

Aider conversations are owned by Aider, not by the WebUI. The bridge therefore never writes an
Aider file, and the WebUI never materializes one as a writable session:

- List rows carry `read_only: True` and no Hermes profile — the transcript lives in a git root,
  not inside a Hermes profile tree.
- The **`aider_` id prefix is the read-only identity.** It cannot be laundered by a metadata
  lookup that misses, so an Aider session can never be claimed, forked, or rewritten as a
  writable WebUI session.
- Mutating an Aider session from the WebUI is unsupported: there is no rename, truncate, clear,
  delete, or archive affordance for these rows.
- The sidebar setting **Settings → Show Aider sessions** hides only Aider history and leaves
  every other external session source (Claude Code, Codex, and the rest) visible.

## Verification

The bridge is covered by targeted tests:

| File | Covers |
| --- | --- |
| `tests/test_aider_chat_history_import.py` | parsing, role mapping, scan bounds, parse cache |
| `tests/test_issue674_all_profiles_boundary.py` | the all-profiles aggregate boundary |
| `tests/test_issue674_external_workspace_resolve.py` | one shared workspace resolve per build |
| `tests/test_issue674_named_profile_visibility.py` | Aider rows survive the sidebar profile filter |
| `tests/test_issue674_point_of_use_containment.py` | point-of-use containment and race handling |
| `tests/test_issue674_transcript_cap_window.py` | the window keeps the newest messages |
