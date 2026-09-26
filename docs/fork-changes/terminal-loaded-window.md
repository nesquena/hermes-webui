# Preserve loaded history on terminal error and cancel

Classification: upstream-candidate; owner: fork maintainers; not filed upstream.
Base: 16720a769d0c5ae3e873cce621d2b20fc3283dd7. Reconstruct before upstream contribution.

Error/cancel adoption reuse the existing loaded-window projection helper. The
adopted object supplies offset/truncation even when the helper declines slicing.
Embedded and GET cancellation responses share the same guarded adoption path;
capture occurs at application time, after any GET await. Continuation/revision,
shrink and changed-boundary cases retain canonical payloads. Transport is unchanged.

Run `tests/browser_terminal_loaded_window.py` and
`tests/browser_terminal_window_boundaries.py` with the repository browser-test
interpreter. Both engines cover 16 main cases and 22 boundary/race cases, with
synthetic transport and actual terminal listeners. The main regression fails on
base (3302 loaded messages instead of 52). Existing projection and cancellation
ownership tests remain required. No full-suite or physical battery claim.

The short call-site guards are retained rather than introducing another adoption
abstraction: projection itself is centralized; each existing handler owns its
state and lifetime. Generic recovery and same-session newer-run cancellation races
are not redesigned. No new renderer, timer, cap, migration or setting.
Rollback: revert this commit. Retire when upstream supplies equivalent terminal
window preservation; verify offset/revision semantics when updating the helper.
