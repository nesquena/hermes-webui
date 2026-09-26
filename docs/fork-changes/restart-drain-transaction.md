# Scheduled restart admission transaction

The scheduled WebUI update restart owns a process-image drain marker. Marker
publication runs synchronously under `ACTIVE_RUNS_LOCK`, the same lock used by
worker registration. Publication failure propagates without launching the
restart thread. A second scheduler cannot acquire another drain while the first
owns admission.

The replacement worker waits for the existing restart-safety predicate. A
still-blocked result (including timeout) aborts before bytecode purge or process
replacement. Thread construction/start failure, a wait exception, and a returned
replacement attempt release the marker so the surviving process can admit work.

The marker includes a random interpreter-image generation. POSIX `execv` retains
the PID but loads a new generation: the new image ignores an explicitly
identified predecessor marker. Missing-generation, malformed, and unreadable
markers remain fail-closed. The old marker is replaced by the next successful
drain publication; it is not an authority for the replacement image.

## Verification and remaining scope

`tests/test_restart_drain_transaction.py` exercises synchronous publication,
publication failure, blocked timeout, duplicate scheduler ownership, and thread
construction rollback. `tests/test_restart_drain_exec.py` runs a real POSIX exec
in an isolated child, proves PID continuity, and registers a replacement run.
No production process or real user state is used.

Gateway service restart and shutdown now acquire the same admission drain
before waiting or launching their process/signal side effects. Blocked waits
abort and release admission. Synchronous chat retains a registry reservation
through persistence; streaming request preparation reserves occupancy before
pending-state mutation. Worker registration still checks the drain separately.
Agent updates must observe a completed Gateway restart, not merely an in-progress
command, before scheduling replacement. If the quick timeout returns in-progress,
the update transaction transfers its admission drain to the background subprocess
waiter; it releases admission only after the CLI exits or is terminated.
Admission-surface regressions live in
`tests/test_restart_admission_surfaces.py` and `tests/test_gate_221beca7_blockers.py`.

The Gateway wait currently runs synchronously before its command launch, so a
busy restart request may wait up to the existing drain ceiling. End-to-end
concurrent HTTP admission/worker-handoff stress remains outside these tests.
