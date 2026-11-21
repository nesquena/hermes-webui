#!/usr/bin/env python3
"""Mutation matrix for the #7862 round-21 fixes.

Each mutation breaks ONE production mechanism that a new test claims to
pin. The new test(s) must go RED under it. A test that stays green under
its own mutation is not pinning anything.

Run from the worktree root:
    .venv/bin/python tests/r21_mutation_matrix.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "api" / "goal_continuation_store.py"
ROUTES = REPO_ROOT / "api" / "routes.py"

PY = str(REPO_ROOT / ".venv" / "bin" / "python")

GOAL_TESTS = "tests/test_issue7862_round21_goal_recovery.py"
PEER_TESTS = "tests/test_issue7862_round21_peer_continuation.py"
RECEIPT_TESTS = "tests/test_issue7862_round10_receipt_lifetime.py"

# (name, file, [(anchor, replacement), ...], test file, expected-red names)
MUTATIONS = [
    (
        "R1: bring the wall-clock retirement of a pending intent back",
        STORE,
        [
            (
                "        # 2. A pending record whose intent was EXPLICITLY retired is dead",
                "        _MUT_now = time.time()\n"
                "        for _mut_sid in list(PENDING_GOAL_CONTINUATION_RECORDS.keys()):\n"
                "            _mut_rec = PENDING_GOAL_CONTINUATION_RECORDS[_mut_sid]\n"
                "            if (_MUT_now - float(_mut_rec.get(\"created_at\") or _MUT_now)) > (\n"
                "                24 * 60 * 60\n"
                "            ):\n"
                "                retire_pending_goal_continuation(_mut_sid, reason=\"expired\")\n"
                "        # 2. A pending record whose intent was EXPLICITLY retired is dead",
            ),
        ],
        GOAL_TESTS,
        [
            "test_a_valid_queued_continuation_survives_a_restart_however_old",
            "test_the_browser_token_of_an_aged_continuation_is_still_accepted",
        ],
    ),
    (
        "R2: let the restore treat an unreadable registry as an empty one",
        STORE,
        [
            (
                '            if _LAST_LOAD_ERROR:\n'
                '                logger.warning(\n'
                '                    "Goal continuation registry: the restore was skipped "',
                '            if False:\n'
                '                logger.warning(\n'
                '                    "Goal continuation registry: the restore was skipped "',
            ),
        ],
        GOAL_TESTS,
        [
            "test_an_unreadable_registry_does_not_erase_live_records",
            "test_a_corrupt_registry_does_not_erase_live_records",
        ],
    ),
    (
        "R3a: let the record reader raise on a malformed numeric field",
        STORE,
        [
            (
                '        try:\n'
                '            record_generation = int(rec.get("generation") or file_generation)\n'
                '            record_created_at = float(rec.get("created_at") or time.time())\n'
                '        except (TypeError, ValueError) as exc:',
                '        try:\n'
                '            record_generation = int(rec.get("generation") or file_generation)\n'
                '            record_created_at = float(rec.get("created_at") or time.time())\n'
                '        except ZeroDivisionError as exc:',
            ),
        ],
        GOAL_TESTS,
        [
            "test_a_bad_creation_time_alone_is_reported_instead_of_raising",
            "test_a_malformed_record_does_not_silently_degrade_a_continuation",
        ],
    ),
    (
        "R3b: let the top-level generation conversion raise again",
        STORE,
        [
            (
                '        try:\n'
                '            _LAST_FILE_GENERATION = int(data.get("generation") or 0)\n'
                '        except (TypeError, ValueError) as exc:',
                '        try:\n'
                '            _LAST_FILE_GENERATION = int(data.get("generation") or 0)\n'
                '        except ZeroDivisionError as exc:',
            ),
        ],
        GOAL_TESTS,
        [
            "test_a_bad_numeric_field_is_reported_instead_of_raising",
        ],
    ),
    (
        "R4: let the timer callback keep its own reference, so it can never "
        "arm the successor",
        STORE,
        [
            (
                "    global _DEFERRED_RECOVERY_TIMER\n"
                "    # Clear the reference FIRST: the recovery below re-arms the timer for\n"
                "    # the next deadline, and the guard must not see this (still running)\n"
                "    # timer as \"one is already scheduled\".\n"
                "    _DEFERRED_RECOVERY_TIMER = None",
                "    global _DEFERRED_RECOVERY_TIMER\n"
                "    # MUTATION: keep the reference, so the guard rejects the successor.\n"
                "    pass",
            ),
        ],
        GOAL_TESTS,
        [
            "test_a_later_deadline_is_checked_in_the_same_process_lifetime",
            "test_the_deferred_recovery_arms_the_next_deadline_from_its_own_callback",
        ],
    ),
    (
        "R5a: refuse the continuation token from the local marker set again",
        ROUTES,
        [
            (
                "            if s.session_id not in PENDING_GOAL_CONTINUATION and not (\n"
                "                pending_goal_continuation_matches(\n"
                "                    s.session_id, goal_continuation_id\n"
                "                )\n"
                "            ):",
                "            if s.session_id not in PENDING_GOAL_CONTINUATION:",
            ),
        ],
        PEER_TESTS,
        ["test_a_peer_persisted_continuation_is_admitted_by_the_real_route"],
    ),
    (
        "R5b: let the store's match check answer from memory only",
        STORE,
        [
            (
                "        with _registry_transaction():\n"
                "            # Reload the durable state under the cross-process lock: the\n"
                "            # local memory is not authoritative (round 16).\n"
                "            _merge_disk_state_unlocked()\n"
                "            from api.config import PENDING_GOAL_CONTINUATION_RECORDS",
                "        with _registry_transaction():\n"
                "            # MUTATION: trust the local memory only.\n"
                "            from api.config import PENDING_GOAL_CONTINUATION_RECORDS",
            ),
        ],
        PEER_TESTS,
        ["test_a_peer_persisted_continuation_is_admitted_by_the_real_route"],
    ),
    (
        "R1b: let the sweep retire an intent without comparing generations",
        STORE,
        [
            (
                "        for sid in list(PENDING_GOAL_CONTINUATION_RECORDS.keys()):\n"
                "            record = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)\n"
                "            if not isinstance(record, dict):\n"
                "                continue\n"
                "            if not _record_is_retired(sid, record):\n"
                "                continue",
                "        for sid in list(PENDING_GOAL_CONTINUATION_RECORDS.keys()):\n"
                "            record = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)\n"
                "            if not isinstance(record, dict):\n"
                "                continue\n"
                "            if int(_TOMBSTONES.get(sid) or 0) <= 0:\n"
                "                continue",
            ),
        ],
        GOAL_TESTS,
        ["test_the_sweep_does_not_retire_a_continuation_newer_than_the_stamp"],
    ),
    (
        "R1c: let the sweep ignore the live handoff (string key never matches)",
        STORE,
        [
            (
                "            if any(key[0] == sid for key in _CONTINUATION_HANDOFF_TOKENS):\n"
                "                # An admission is still in flight for this session: its\n"
                "                # handoff is the evidence, so the intent is not dead work.\n"
                "                continue",
                "            if sid not in _CONTINUATION_HANDOFF_TOKENS:\n"
                "                # MUTATION: the string key never matches the tuple keys.\n"
                "                pass\n"
                "            else:\n"
                "                continue",
            ),
        ],
        GOAL_TESTS,
        ["test_the_sweep_keeps_an_intent_with_a_live_handoff"],
    ),
    (
        "T1: let the abandoned-receipt cleanup drop nothing",
        STORE,
        [
            (
                "    for key in expired:\n"
                "        _ROLLBACK_RECEIPTS.pop(key, None)",
                "    for key in []:\n"
                "        _ROLLBACK_RECEIPTS.pop(key, None)",
            ),
        ],
        RECEIPT_TESTS,
        ["test_an_abandoned_attempts_receipt_is_swept"],
    ),
    (
        "T2: let the abandoned-receipt cleanup drop every receipt",
        STORE,
        [
            (
                "    for key in expired:\n"
                "        _ROLLBACK_RECEIPTS.pop(key, None)",
                "    for key in list(_ROLLBACK_RECEIPTS):\n"
                "        _ROLLBACK_RECEIPTS.pop(key, None)",
            ),
        ],
        RECEIPT_TESTS,
        ["test_the_abandoned_receipt_sweep_keeps_a_live_attempts_receipt"],
    ),
]


def _run(test_file: str) -> tuple[int, str]:
    proc = subprocess.run(
        [
            PY,
            "-m",
            "pytest",
            test_file,
            "-q",
            "-p",
            "no:randomly",
            "--timeout=180",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={
            # The inherited PATH matters: some test files skip the WHOLE
            # module when ``node`` is not on PATH, and a fully-skipped run
            # must not be read as "the mutation did not turn the test red".
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "HOME": "/tmp/pr7862-mutation-home",
            "TMPDIR": "/tmp/pr7862-tmp",
            "HERMES_WEBUI_PYTHON": PY,
            "HERMES_WEBUI_TEST_STATE_DIR": "/tmp/pr7862-state",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "LANG": "en_US.UTF-8",
            # The conftest needs an agent home, otherwise EVERY case is
            # skipped and the mutation would look like a pass.
            "HERMES_HOME": "/tmp/pr7862-mutation-home",
            "HERMES_BASE_HOME": "/tmp/pr7862-mutation-home",
        },
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    failures: list[str] = []
    for name, target, edits, test_file, expected_red in MUTATIONS:
        original = target.read_text(encoding="utf-8")
        mutated = original
        missing = []
        for anchor, replacement in edits:
            if anchor not in mutated:
                missing.append(anchor[:60])
                continue
            mutated = mutated.replace(anchor, replacement, 1)
        if missing:
            print(f"[ANCHOR-MISS] {name}\n          missing: {missing}")
            failures.append(f"{name}: anchor not found in {target.name}")
            continue
        target.write_text(mutated, encoding="utf-8")
        try:
            code, out = _run(test_file)
        finally:
            target.write_text(original, encoding="utf-8")

        went_red = []
        for fragment in expected_red:
            if fragment in out and "FAILED" in out:
                went_red.append(fragment)
        # A run where everything was SKIPPED is an ENVIRONMENT failure, not a
        # pass: without this guard a broken child env reads as "the mutation
        # did not turn the test red".
        skipped_all = "skipped" in out and " passed" not in out and "failed" not in out
        if skipped_all:
            status = "ENV-ERROR"
            failures.append(f"{name}: the whole run was skipped (child env)")
        elif len(went_red) == len(expected_red):
            status = "RED-OK"
        else:
            status = "PROBLEM"
            failures.append(
                f"{name}: expected RED for {expected_red}, got {went_red}"
            )
        print(
            f"[{status}] {name}\n"
            f"          expected red: {expected_red}\n"
            f"          observed red: {went_red}\n"
            f"          exit={code}\n"
        )

    print("\n=== mutation matrix summary ===")
    if failures:
        for line in failures:
            print("FAIL:", line)
        return 1
    print("all mutations turned their target test RED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
