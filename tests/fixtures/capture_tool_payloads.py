#!/usr/bin/env python3
"""Capture REAL ``on_tool_complete`` payloads from the Agent's own tools.

Why this exists
---------------
The #7358 review found that the WebUI's ``tool_complete`` ``is_error``
classifier was tested only against hand-written **dict** fixtures, while
the production Agent hands the structured callback a JSON **string**:

.. code-block:: python

    # run_agent.py (structured callback invocation)
    self.tool_complete_callback(tool_call.id, function_name, function_args, function_result)

``function_result`` is the verbatim return value of the tool handler.
``tools/terminal_tool.py`` returns ``json.dumps({...})``, so a captured
*successful* terminal call is the literal text::

    {"output": "hello-world", "exit_code": 0, "error": null}

The round-2 WebUI helper substring-scanned that text for ``"error"``,
matched the **key name**, and painted every successful terminal command
red. A hand-written dict fixture could never catch it.

Run
---
.. code-block:: bash

    python3 tests/fixtures/capture_tool_payloads.py

Writes ``/tmp/captured_tool_payloads.json``. Requires the hermes-agent
checkout (``HERMES_WEBUI_AGENT_DIR``) to be importable — this script is
a diagnostic, not a test, and is skipped when the agent repo is absent.
"""

import json
import os
import sys

AGENT_DIR = os.environ.get("HERMES_WEBUI_AGENT_DIR", "")
OUT = os.environ.get("CAPTURE_OUT", "/tmp/captured_tool_payloads.json")


def main() -> int:
    if not AGENT_DIR or not os.path.isdir(AGENT_DIR):
        print(
            "SKIP: set HERMES_WEBUI_AGENT_DIR to the hermes-agent checkout "
            "to capture real tool_complete payloads."
        )
        return 0

    sys.path.insert(0, AGENT_DIR)
    os.chdir(AGENT_DIR)

    captured = []

    def on_tool_complete(tool_call_id, name, args, function_result):
        """The exact 4-arg shape run_agent.py invokes."""
        captured.append(
            {
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "args": args,
                "function_result": function_result,
                "function_result_type": type(function_result).__name__,
            }
        )

    # Importing the tool module registers it in the tool registry.
    from tools import terminal_tool  # noqa: F401
    from tools.registry import registry

    cases = [
        # (label, args, why this case matters)
        ("success", {"command": "echo hello-world"}, "canonical success shape — exit_code 0, error null"),
        ("failure", {"command": "exit 3"}, "canonical failure shape — non-zero exit_code"),
        (
            "grep_no_match",
            {"command": "echo hi | grep zzz-nonexistent-token"},
            "exit 1 with an explanatory exit_code_meaning",
        ),
    ]

    for label, args, _why in cases:
        try:
            result = registry.dispatch("terminal", args)
        except Exception as exc:  # pragma: no cover - diagnostic only
            print(f"{label}: dispatch failed: {exc}")
            continue
        on_tool_complete(f"call_{label}", "terminal", args, result)
        print(f"{label}: {result}")

    with open(OUT, "w") as fh:
        json.dump(captured, fh, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(captured)} captured payloads -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
