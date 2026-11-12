"""Regression tests for #6672 / PR #6683: switching the workspace mid-session
must NOT mutate the system prompt (msg[0]).

`Session.workspace` is updated on every turn when the user switches workspaces
via the WebUI header dropdown. Before the fix, that live value was interpolated
into the system prompt, so a mid-session switch rewrote msg[0] and invalidated
the LLM prefix cache (APC/Radix Tree) for the whole transcript. The fix freezes
an immutable `created_workspace` snapshot at session creation and uses it in the
system prompt; live switches ride on the [Workspace::v1: ...] tag on msg[-1].
"""
import os

from api.models import Session


def test_created_workspace_frozen_at_init():
    s = Session(workspace="/tmp/proj-alpha")
    assert s.created_workspace == os.path.realpath(os.path.expanduser("/tmp/proj-alpha"))
    # Simulate a mid-session workspace switch (what the WebUI header dropdown does).
    s.workspace = os.path.realpath("/tmp/proj-beta")
    # The creation snapshot must NOT follow the live switch.
    assert s.created_workspace == os.path.realpath("/tmp/proj-alpha")
    assert s.created_workspace != s.workspace


def test_created_workspace_defaults_to_workspace_when_unset():
    s = Session(workspace="/tmp/proj-gamma")
    # No explicit created_workspace passed -> falls back to the workspace.
    assert s.created_workspace == os.path.realpath("/tmp/proj-gamma")


def test_created_workspace_round_trips_through_compact_and_load():
    s = Session(workspace="/tmp/proj-orig")
    s.workspace = os.path.realpath("/tmp/proj-switched")
    d = s.compact()
    # compact() exposes the frozen creation workspace, distinct from the live one.
    assert d["created_workspace"] == os.path.realpath("/tmp/proj-orig")
    assert d["workspace"] == os.path.realpath("/tmp/proj-switched")
    # Round-trip: reconstruct from the persisted dict and confirm the frozen
    # value is preserved (not re-derived from the switched workspace).
    s2 = Session(**{k: v for k, v in d.items() if k in _init_kwargs()})
    assert s2.created_workspace == os.path.realpath("/tmp/proj-orig")


def test_legacy_session_without_created_workspace_falls_back():
    # A legacy on-disk session has no persisted created_workspace.
    s = Session(workspace="/tmp/proj-legacy", created_workspace=None)
    assert s.created_workspace == os.path.realpath("/tmp/proj-legacy")


def _init_kwargs():
    """Kwargs accepted by Session.__init__, for a safe dict round-trip."""
    import inspect
    return set(inspect.signature(Session.__init__).parameters) - {"self"}
