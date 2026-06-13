"""echo.test -- trivial round-trip action for liveness and bus testing.

This action does not touch the session database, the agent, or the SSE
channel. It exists so the dispatch path can be exercised end-to-end in
unit tests and via the manual smoke test, without requiring any of the
helpers that later builtins (``session.nudge``) will introduce.

Payload contract::

    {"content": "<some short string>"}

Result contract::

    ok=True
    silent=False if content is non-empty, True otherwise
    assistant_message=content (or None if empty)
"""

from __future__ import annotations

from ..types import ActionContext, ActionResult


class EchoTestAction:
    name = "echo.test"

    def run(self, payload: dict, context: ActionContext) -> ActionResult:
        content = payload.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            return ActionResult(
                ok=False,
                silent=True,
                error="content must be a string",
            )

        body = content.strip()
        if not body:
            return ActionResult(ok=True, silent=True)

        return ActionResult(
            ok=True,
            silent=False,
            assistant_message=body,
            refresh_chat=False,
            meta={"source": context.source},
        )
