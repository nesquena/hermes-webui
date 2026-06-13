# RFC: Hermes Action Bus

- **Status:** Proposed (v1)
- **Author:** @webflow-pt
- **Created:** 2026-05-28

## Problem

Hermes WebUI exposes a small number of entry points that need to make
the same kind of side-effecting backend call: a cron job wakes a
session, a webhook delivers a third-party event, a future MCP tool
asks Hermes to do something on behalf of a remote caller, the in-UI
slash-command surface fires a one-shot operation. Today each of those
entry points reaches into `api/routes.py` (or `api/background.py`,
or the gateway adapter, or `api/cron.py`) and assembles its own ad-hoc
path: parse a payload, look up a session, decide whether to run the
agent, decide whether to persist anything, decide whether to notify
open WebUI tabs.

The lack of a shared primitive means:

- Each entry point reimplements the same validation and error
  contract slightly differently.
- Idempotency (repeated cron/webhook deliveries should not double up)
  has to be solved per-caller.
- The wire shape of "what happened on the server" is not normalized,
  so client code has to special-case every channel.
- Adding a new operation means touching the dispatch code in every
  entry point, which discourages experiments.

## Goals

- One typed, synchronous dispatcher that any entry point (HTTP,
  cron, webhook, gateway, MCP, slash-command) can call to invoke a
  registered backend action.
- A normalized result shape that decouples "did it succeed?" from
  "should the UI render anything?".
- In-process idempotency so the same `(action, idempotency_key)`
  pair returns the same `ActionResult` within a TTL window.
- An emit-event seam so actions can broadcast typed SSE events on
  the existing session-events channel without taking a direct
  dependency on the SSE module.
- A registration model that survives the addition of new builtins
  in follow-up PRs without anyone editing the route hook.

## Non-goals

- Replacing `api/background.py` slash-commands or `api/cron.py` job
  delivery. Those can be re-expressed on top of the bus later; v1
  ships alongside them.
- Asynchronous dispatch. The WebUI is a `ThreadingHTTPServer` and
  every existing handler is synchronous; an `asyncio` runtime is
  out of scope for this RFC.
- Durable idempotency. v1 caches in-process for a configurable
  TTL; a durable backend (sqlite, Redis) can slot in behind the
  same `dispatch` signature later.
- A general-purpose plugin loader. Builtins are explicitly
  registered in `api/actions/__init__.py::_register_all_builtins`.
- A per-action access-control model. v1 piggybacks on the existing
  `_check_csrf` path on `/api/actions`; a future PR can add a
  per-action `allowed_sources` allowlist when the action surface
  grows beyond browser-driven calls.

## The contract

### `Action`

A registered backend action is anything that implements
`api.actions.types.Action`:

```python
class Action(Protocol):
    name: str
    def run(self, payload: dict, context: ActionContext) -> ActionResult: ...
```

Actions are synchronous, side-effecting, and free to raise. The
registry catches any non-`BaseException` and converts it into an
error `ActionResult`; `BaseException` (`KeyboardInterrupt`,
`SystemExit`) propagates so process shutdown is never blocked.

### `ActionContext`

Every dispatch carries an `ActionContext` describing the caller:

```python
@dataclass
class ActionContext:
    session_id: str | None
    user_id: str | None
    source: str                      # "webui_api", "cron", "webhook", "gateway", ...
    emit_event: Callable[[str, dict], None]
    dispatch: Callable[[str, dict, "ActionContext", str | None], "ActionResult"]
    request_meta: dict
```

- `session_id` is the WebUI session the action targets, when one
  exists. Actions that operate on a session validate this field
  themselves; the bus does not enforce it.
- `source` is a free-form tag the caller sets so actions and logs
  can distinguish a browser POST from a cron firing.
- `emit_event(event_type, payload)` lets the action broadcast a
  typed SSE event on the shared session-events channel. The
  default (when no production publisher is available) is a no-op.
- `dispatch(name, payload, context, idempotency_key=None)` lets
  an action chain to another registered action. See **Chaining**
  below.
- `request_meta` is a free-form bag for caller-specific context
  (remote address, gateway request id, etc.) without bloating the
  context type.

### `ActionResult`

Every dispatch returns:

```python
@dataclass
class ActionResult:
    ok: bool
    silent: bool
    assistant_message: str | None = None
    refresh_chat: bool = False
    error: str | None = None
    meta: dict | None = None
```

The three orthogonal flags matter:

- `ok` — did the action complete successfully? Used by the HTTP
  adapter to map to 2xx vs. 5xx.
- `silent` — should the UI surface anything? An action can succeed
  silently (a cron that decided not to wake the session) or fail
  silently (a no-op duplicate delivery).
- `refresh_chat` — should open WebUI tabs reload the active
  session's messages? Set by actions that wrote to the session.

`assistant_message` carries the rendered text when the action
emitted one. Decoupling it from `ok`/`silent` means UI code never
has to infer intent from content.

## Idempotency

The registry holds an in-memory `_IdempotencyCache`:

```text
(action_name, idempotency_key) -> (created_at, ActionResult)
```

`dispatch(action, payload, context, idempotency_key=...)` checks
the cache before running. A hit returns the previously stored
result and never re-invokes the action. On a miss the action runs,
its result is cached, and the cache prunes entries older than
`ttl_seconds` (default 300) on the next `get`.

Trade-offs:

- The cache is in-process. A process restart loses pending entries.
  Cron/webhook callers should treat this as "best-effort within
  the TTL window" rather than "exactly-once forever".
- Pruning is lazy and O(n) on the cache size. v1 expects very few
  in-flight keys at any time; a future tightening can swap to a
  sorted-by-insertion-time pop if hot-path metrics warrant it.
- The lock is per-cache, not per-action. Two threads dispatching
  the same `(action, key)` may both run the action if they race
  past the `get` before either `put`. v1 accepts this — the
  guarantee is "the cached result wins for the rest of the TTL",
  not "the action runs at most once during the race window".

## Error handling

```text
dispatch(name=...)
  -> ActionNotFound          (raised by registry, caught by HTTP -> 404)
  -> ActionResult(ok=False, error=...) for any other Exception
  -> ActionResult(ok=True, ...)        for success
```

The HTTP adapter (`api/actions_http.py`) maps:

- `ActionNotFound` -> 404 with `{"error": "...", "known_actions": [...]}`
- Validation failures (missing/invalid `action`, non-dict body,
  bad `session_id`/`idempotency_key` types) -> 400 with a
  descriptive error.
- Successful dispatch -> 200 with the serialized `ActionResult`.

Validation lives in the HTTP adapter, not the registry, so callers
that bypass HTTP (in-process cron, MCP) can pass already-validated
input without paying twice.

## Chaining

`ActionContext.dispatch` lets a registered action invoke another
registered action without taking a direct dependency on the
registry. This is the right primitive when an action wants to
trigger a follow-on operation as part of its own response (for
example, a `session.nudge` that, on success, dispatches a small
`session.refresh` to materialize updated sidebar metadata on
non-active tabs).

Chaining preserves the `source` tag of the outer caller by
default, but actions are free to construct a fresh `ActionContext`
when they want the inner dispatch to look like a different
originator. Idempotency keys propagate independently — chained
calls do not share a key with the outer dispatch unless the
action explicitly forwards one.

Tests cover this via the `_ctx(dispatch=registry.dispatch)`
pattern; see `tests/test_action_bus.py::test_action_can_chain_via_context_dispatch`.

## Registration

Production callers use the module-level singleton:

```python
from api.actions import default_registry, register_builtins
register_builtins(default_registry)   # idempotent
default_registry.dispatch("echo.test", {"content": "ping"}, ctx)
```

`register_builtins(default_registry)` is guarded by
`_BUILTINS_LOCK` + `_BUILTINS_REGISTERED` so it is safe to call on
every request — the first call registers the v1 builtins, every
subsequent call returns immediately under the lock. New builtins
added in follow-up PRs are picked up automatically the first time
the process starts after the upgrade; the route hook does not
need to be touched.

Test code constructs its own `ActionRegistry()` and passes it
explicitly, which bypasses the singleton flag and gives every
test a fresh registry. `register_builtins(reg)` against a
non-default registry always registers fresh, which is what test
isolation needs.

## HTTP entry point

```text
POST /api/actions
Content-Type: application/json

{
  "action": "echo.test",
  "payload": {"content": "ping"},
  "session_id": "...",        // optional
  "idempotency_key": "..."    // optional
}

200 OK
{
  "ok": true,
  "silent": false,
  "assistant_message": "ping",
  "refresh_chat": false,
  "error": null,
  "meta": {}
}
```

CSRF is enforced via the existing `_check_csrf` path in
`api/routes.py`; `/api/actions` is intentionally not on the
exempt list. Curl callers without an `Origin` header pass the
`_is_browser_unsafe_request` gate; browser callers go through the
same-origin path.

## What's out of scope for v1

- Per-action `allowed_sources` allowlist (e.g. `session.nudge`
  restricted to non-browser entry points). Defer until the action
  surface grows beyond browser-driven smoke tests.
- Durable idempotency cache (sqlite or Redis backend).
- Async actions / async dispatch. Synchronous matches the WebUI's
  threading model and the rest of `api/*`.
- A plugin loader / dynamic action registration from disk.
- Inline batch dispatch (`POST /api/actions` with a list of
  actions). Each call is one action; chaining within actions
  covers the small set of cases where one call should fan out.

## Follow-ups planned

- **`session.nudge`** (separate PR, gated on this one landing).
  First session-touching builtin. Runs an inference-only synthetic
  user turn against an existing session, persists only a
  non-silent assistant response, and emits
  `session.message_appended` on the WebUI SSE channel so open
  tabs refresh.
- **`session.refresh`** (further follow-up). Recomputes sidebar
  metadata without invoking the agent. Useful as a chain target
  for `session.nudge` and for cron-driven housekeeping.
- **Refactor** of agent construction. `session.nudge` currently
  inlines the model/provider/api_key resolution that
  `/api/chat` does. A small extraction (`build_session_agent(session)`)
  can land later and be called from both sites.
