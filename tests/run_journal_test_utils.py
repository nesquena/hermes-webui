"""Explicit run-journal authority helpers for test fixture construction."""

from api.run_journal import (
    RunJournalWriter as _RunJournalWriter,
    activate_run_journal_session,
    append_run_event as _append_run_event,
)


def append_run_event(session_id, run_id, event_name, payload=None, **kwargs):
    session_dir = kwargs.get("session_dir")
    incarnation = activate_run_journal_session(
        session_id,
        session_dir=session_dir,
    )
    return _append_run_event(
        session_id,
        run_id,
        event_name,
        payload,
        **kwargs,
        _incarnation=incarnation,
    )


def RunJournalWriter(session_id, run_id, *, session_dir=None):
    incarnation = activate_run_journal_session(
        session_id,
        session_dir=session_dir,
    )
    return _RunJournalWriter(
        session_id,
        run_id,
        session_dir=session_dir,
        incarnation=incarnation,
    )
