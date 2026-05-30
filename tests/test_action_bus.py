"""Unit tests for the Hermes Action Bus.

Covers:
    1. ActionRegistry dispatch + ActionNotFound
    2. Exception inside an action becomes ActionResult(ok=False, error=...)
    3. Idempotency cache (same key returns cached result; different keys
       run the action again; no key always runs)
    4. The trivial ``echo.test`` builtin behavior
    5. The HTTP adapter ``handle_actions_post``: 400 on missing/invalid
       fields, 404 on unknown action, 200 on a valid dispatch

These are pure in-process tests; they do not start the test server.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

# Ensure the repo root is importable without relying on CWD. Mirrors the
# bootstrap in tests/test_background_tasks.py.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.actions import (  # noqa: E402  (after sys.path bootstrap)
    ActionContext,
    ActionNotFound,
    ActionRegistry,
    ActionResult,
    register_builtins,
)
from api.actions.builtin.echo_test import EchoTestAction  # noqa: E402
from api.actions_http import handle_actions_post  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────


def _capture_emit():
    calls: list[tuple[str, dict]] = []

    def _emit(event_type: str, payload: dict) -> None:
        calls.append((event_type, payload))

    return _emit, calls


def _ctx(**overrides) -> ActionContext:
    emit, _calls = _capture_emit()
    defaults = dict(
        session_id=overrides.pop("session_id", "sess-1"),
        user_id=overrides.pop("user_id", None),
        source=overrides.pop("source", "test"),
        emit_event=overrides.pop("emit_event", emit),
        request_meta=overrides.pop("request_meta", {}),
    )
    return ActionContext(**defaults, **overrides)


# ── Registry behavior ──────────────────────────────────────────────────


class TestRegistryDispatch(unittest.TestCase):
    def test_unknown_action_raises_action_not_found(self):
        reg = ActionRegistry()
        with self.assertRaises(ActionNotFound):
            reg.dispatch("missing.action", {}, _ctx())

    def test_register_duplicate_name_raises(self):
        reg = ActionRegistry()
        reg.register(EchoTestAction())
        with self.assertRaises(ValueError):
            reg.register(EchoTestAction())

    def test_known_actions_is_sorted(self):
        reg = ActionRegistry()

        class A:
            name = "zeta.thing"
            def run(self, payload, context):  # pragma: no cover
                return ActionResult()

        class B:
            name = "alpha.thing"
            def run(self, payload, context):  # pragma: no cover
                return ActionResult()

        reg.register(A())
        reg.register(B())
        self.assertEqual(reg.known_actions(), ["alpha.thing", "zeta.thing"])

    def test_exception_in_action_becomes_error_result(self):
        reg = ActionRegistry()

        class Boom:
            name = "x.boom"
            def run(self, payload, context):
                raise RuntimeError("kaboom")

        reg.register(Boom())
        result = reg.dispatch("x.boom", {}, _ctx())
        self.assertFalse(result.ok)
        self.assertTrue(result.silent)
        self.assertIsNotNone(result.error)
        self.assertIn("RuntimeError", result.error)
        self.assertIn("kaboom", result.error)


class TestIdempotency(unittest.TestCase):
    def _counting_action(self):
        class Counter:
            name = "x.count"
            calls = 0

            def run(self, payload, context):
                type(self).calls += 1
                return ActionResult(
                    ok=True,
                    silent=False,
                    assistant_message=f"run #{type(self).calls}",
                )

        return Counter

    def test_same_key_returns_cached_result(self):
        reg = ActionRegistry()
        Counter = self._counting_action()
        reg.register(Counter())

        r1 = reg.dispatch("x.count", {}, _ctx(), idempotency_key="k1")
        r2 = reg.dispatch("x.count", {}, _ctx(), idempotency_key="k1")

        self.assertEqual(r1.assistant_message, "run #1")
        self.assertEqual(r2.assistant_message, "run #1")
        self.assertEqual(Counter.calls, 1)

    def test_different_keys_run_action_again(self):
        reg = ActionRegistry()
        Counter = self._counting_action()
        reg.register(Counter())

        r1 = reg.dispatch("x.count", {}, _ctx(), idempotency_key="k1")
        r2 = reg.dispatch("x.count", {}, _ctx(), idempotency_key="k2")

        self.assertEqual(r1.assistant_message, "run #1")
        self.assertEqual(r2.assistant_message, "run #2")
        self.assertEqual(Counter.calls, 2)

    def test_no_key_always_runs(self):
        reg = ActionRegistry()
        Counter = self._counting_action()
        reg.register(Counter())

        reg.dispatch("x.count", {}, _ctx())
        reg.dispatch("x.count", {}, _ctx())
        self.assertEqual(Counter.calls, 2)


# ── echo.test builtin ──────────────────────────────────────────────────


class TestEchoTest(unittest.TestCase):
    def test_echo_returns_visible_assistant_message(self):
        reg = ActionRegistry()
        reg.register(EchoTestAction())

        result = reg.dispatch(
            "echo.test",
            {"content": "hello bus"},
            _ctx(source="unit"),
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.silent)
        self.assertEqual(result.assistant_message, "hello bus")
        self.assertFalse(result.refresh_chat)
        self.assertEqual(result.meta.get("source"), "unit")

    def test_echo_empty_content_is_silent_ok(self):
        reg = ActionRegistry()
        reg.register(EchoTestAction())

        result = reg.dispatch("echo.test", {"content": "   "}, _ctx())
        self.assertTrue(result.ok)
        self.assertTrue(result.silent)
        self.assertIsNone(result.assistant_message)

    def test_echo_non_string_content_is_error(self):
        reg = ActionRegistry()
        reg.register(EchoTestAction())

        result = reg.dispatch("echo.test", {"content": 42}, _ctx())
        self.assertFalse(result.ok)
        self.assertTrue(result.silent)
        self.assertEqual(result.error, "content must be a string")

    def test_register_builtins_registers_echo_only(self):
        reg = ActionRegistry()
        register_builtins(reg)
        self.assertEqual(reg.known_actions(), ["echo.test"])

    def test_action_can_chain_via_context_dispatch(self):
        """A registered action can dispatch a sibling via ``context.dispatch``.

        v1 ships no chaining actions, but the bus contract supports the
        pattern (see docs/rfcs/action-bus.md ``Chaining`` section). This
        test locks the contract in so follow-up actions
        (``session.nudge -> session.refresh``, etc) can rely on it and
        their PR diffs do not have to also grow the test surface.

        Exercises the ``_ctx(dispatch=...)`` extensibility too: the
        chaining action only works when its ``context.dispatch`` is
        wired to the registry's own dispatch method.
        """
        reg = ActionRegistry()
        reg.register(EchoTestAction())

        class _ChainEchoAction:
            name = "chain.echo"

            def run(self, payload, context):
                inner_payload = {"content": payload.get("forward", "")}
                inner = context.dispatch("echo.test", inner_payload, context)
                if not inner.ok:
                    return ActionResult(
                        ok=False, silent=True, error=f"inner: {inner.error}"
                    )
                return ActionResult(
                    ok=True,
                    silent=False,
                    assistant_message=f"chain -> {inner.assistant_message}",
                    refresh_chat=False,
                )

        reg.register(_ChainEchoAction())

        # _ctx already accepts arbitrary overrides; passing dispatch=
        # wires the chain through the same registry the outer dispatch
        # uses. Without this wiring, the default _no_dispatch raises
        # RuntimeError -- which is the right failure mode for callers
        # that did not opt in to chaining.
        result = reg.dispatch(
            "chain.echo",
            {"forward": "ping"},
            _ctx(dispatch=reg.dispatch),
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.silent)
        self.assertEqual(result.assistant_message, "chain -> ping")

    def test_chaining_without_dispatch_raises(self):
        """If the test surface forgets to wire dispatch, chaining fails loud.

        The bus default for ``ActionContext.dispatch`` is ``_no_dispatch``
        which raises ``RuntimeError``. The registry's outer ``dispatch``
        catches the resulting ``Exception`` and wraps it in an error
        ``ActionResult`` -- which is the right behavior, but means the
        test surface must explicitly opt in to chaining via
        ``_ctx(dispatch=...)``. This test asserts that omission is
        observable rather than silently producing wrong results.
        """
        reg = ActionRegistry()
        reg.register(EchoTestAction())

        class _ChainEchoAction:
            name = "chain.echo"

            def run(self, payload, context):
                context.dispatch("echo.test", {"content": "x"}, context)
                return ActionResult(ok=True, silent=False, assistant_message="ok")

        reg.register(_ChainEchoAction())

        result = reg.dispatch("chain.echo", {}, _ctx())

        self.assertFalse(result.ok)
        self.assertTrue(result.silent)
        self.assertIn("RuntimeError", result.error or "")

    def test_register_builtins_default_registry_is_idempotent(self):
        """Repeated calls against default_registry must not raise.

        The route hook in api/routes.py calls register_builtins on every
        request. The first call registers; later calls must be no-ops
        rather than raising ValueError on duplicate names. Adding new
        builtins in follow-up PRs must not change this contract --
        whatever set _register_all_builtins() installs is what the
        registry must end up with after exactly one observable
        registration pass.
        """
        from api.actions import default_registry, register_builtins
        import api.actions as actions_pkg

        # The default_registry is process-global; this test has to mutate
        # both the registration flag and the registry's action map to
        # observe a fresh registration pass. Snapshot the pre-test state
        # and register an addCleanup() that restores it so the test stays
        # order-independent: any later test in the same process sees the
        # exact registry state it would have seen if this test had never
        # run.
        saved_registered = actions_pkg._BUILTINS_REGISTERED
        saved_actions = dict(default_registry._actions)

        def _restore_default_registry_state() -> None:
            with actions_pkg._BUILTINS_LOCK:
                actions_pkg._BUILTINS_REGISTERED = saved_registered
                default_registry._actions.clear()
                default_registry._actions.update(saved_actions)

        self.addCleanup(_restore_default_registry_state)

        # Force a clean registration pass for this test. Mirrors what a
        # fresh process startup would observe.
        with actions_pkg._BUILTINS_LOCK:
            actions_pkg._BUILTINS_REGISTERED = False
            default_registry._actions.clear()

        register_builtins(default_registry)
        after_first = sorted(default_registry.known_actions())

        # Must not raise on the second call.
        register_builtins(default_registry)
        after_second = sorted(default_registry.known_actions())

        self.assertEqual(after_first, after_second)
        self.assertIn("echo.test", after_second)


# ── HTTP adapter handle_actions_post ───────────────────────────────────


class _MockHandler:
    """Stand-in for BaseHTTPRequestHandler -- only the fields the adapter reads."""

    def __init__(self, remote: str = "127.0.0.1", user_id=None):
        self.client_address = (remote, 0)
        self.user_id = user_id


class TestHandleActionsPostValidation(unittest.TestCase):
    def setUp(self):
        self.reg = ActionRegistry()
        self.reg.register(EchoTestAction())
        self.handler = _MockHandler()

    def test_missing_action_returns_400(self):
        body, status = handle_actions_post(self.handler, {}, registry=self.reg)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "action required")

    def test_non_string_action_returns_400(self):
        body, status = handle_actions_post(
            self.handler, {"action": 42}, registry=self.reg
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_blank_string_action_returns_400(self):
        body, status = handle_actions_post(
            self.handler, {"action": "   "}, registry=self.reg
        )
        self.assertEqual(status, 400)

    def test_invalid_payload_type_returns_400(self):
        body, status = handle_actions_post(
            self.handler,
            {"action": "echo.test", "payload": "not-an-object"},
            registry=self.reg,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "payload must be an object")

    def test_invalid_session_id_type_returns_400(self):
        body, status = handle_actions_post(
            self.handler,
            {"action": "echo.test", "session_id": 123},
            registry=self.reg,
        )
        self.assertEqual(status, 400)
        self.assertIn("session_id", body["error"])

    def test_invalid_idempotency_key_type_returns_400(self):
        body, status = handle_actions_post(
            self.handler,
            {"action": "echo.test", "idempotency_key": ["nope"]},
            registry=self.reg,
        )
        self.assertEqual(status, 400)
        self.assertIn("idempotency_key", body["error"])

    def test_non_dict_body_returns_400(self):
        body, status = handle_actions_post(
            self.handler, "not a dict", registry=self.reg  # type: ignore[arg-type]
        )
        self.assertEqual(status, 400)


class TestHandleActionsPostDispatch(unittest.TestCase):
    def setUp(self):
        self.reg = ActionRegistry()
        self.reg.register(EchoTestAction())
        self.handler = _MockHandler()

    def test_unknown_action_returns_404(self):
        body, status = handle_actions_post(
            self.handler,
            {"action": "does.not.exist.anywhere", "payload": {}},
            registry=self.reg,
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])
        self.assertIn("unknown action", body["error"])

    def test_echo_returns_200_with_normalized_result(self):
        body, status = handle_actions_post(
            self.handler,
            {"action": "echo.test", "payload": {"content": "ping"}},
            registry=self.reg,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["silent"])
        self.assertEqual(body["assistant_message"], "ping")
        self.assertFalse(body["refresh_chat"])
        self.assertIsNone(body["error"])

    def test_missing_payload_defaults_to_empty_object(self):
        body, status = handle_actions_post(
            self.handler,
            {"action": "echo.test"},  # no payload key at all
            registry=self.reg,
        )
        # Empty payload -> echo.test returns silent ok.
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["silent"])

    def test_idempotency_via_http_adapter(self):
        # Same idempotency_key should return the cached result, even
        # though we wrap the registry in two separate dispatches.
        class Counter:
            name = "x.count"
            calls = 0

            def run(self, payload, context):
                type(self).calls += 1
                return ActionResult(
                    ok=True,
                    silent=False,
                    assistant_message=f"run #{type(self).calls}",
                )

        self.reg.register(Counter())

        b1, s1 = handle_actions_post(
            self.handler,
            {"action": "x.count", "payload": {}, "idempotency_key": "abc"},
            registry=self.reg,
        )
        b2, s2 = handle_actions_post(
            self.handler,
            {"action": "x.count", "payload": {}, "idempotency_key": "abc"},
            registry=self.reg,
        )
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        self.assertEqual(b1["assistant_message"], "run #1")
        self.assertEqual(b2["assistant_message"], "run #1")
        self.assertEqual(Counter.calls, 1)

    def test_emit_event_is_passed_through_context(self):
        # An action that fires an event should reach the emit_event
        # closure handed to handle_actions_post.
        calls: list[tuple[str, dict]] = []

        def _emit(event_type: str, payload: dict) -> None:
            calls.append((event_type, payload))

        class Emitter:
            name = "x.emit"
            def run(self, payload, context):
                context.emit_event("x.fired", {"echo": payload.get("note")})
                return ActionResult(ok=True, silent=True)

        self.reg.register(Emitter())

        body, status = handle_actions_post(
            self.handler,
            {"action": "x.emit", "payload": {"note": "hi"}},
            registry=self.reg,
            emit_event=_emit,
        )
        self.assertEqual(status, 200)
        self.assertEqual(calls, [("x.fired", {"echo": "hi"})])


if __name__ == "__main__":
    unittest.main()
