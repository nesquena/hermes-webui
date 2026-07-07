"""Extension observability-event registration capability."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def test_public_event_api_present():
    assert "const _HERMES_EXTENSION_EVENT_TYPES=new Set(['tool','tool_complete','approval','clarify','done','stream_end','apperror','cancel'])" in BOOT_JS
    assert "function _hermesExtensionHasEventPermission()" in BOOT_JS
    assert "window.HermesExtensionEvents={" in BOOT_JS
    assert "subscribe:function(types, handler)" in BOOT_JS
    assert "window._publishHermesExtensionEvent=_publishHermesExtensionEvent" in BOOT_JS


def test_event_type_set_excludes_metrics():
    start = BOOT_JS.index("const _HERMES_EXTENSION_EVENT_TYPES")
    end = BOOT_JS.index("const _HERMES_EXTENSION_EVENT_SUBSCRIBERS")
    block = BOOT_JS[start:end]
    assert "metering" not in block
    assert "tool_complete" in block
    assert "stream_end" in block
    assert "apperror" in block


def test_live_stream_handlers_publish_selected_events():
    assert "const publishExtensionEvent=(type,d)=>" in MESSAGES_JS
    for event_type in ("tool", "tool_complete", "approval", "clarify", "done", "stream_end", "apperror", "cancel"):
        assert f"publishExtensionEvent('{event_type}'" in MESSAGES_JS
    assert "publishExtensionEvent('metering'" not in MESSAGES_JS


def test_runtime_config_injects_observability_permission(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    root.mkdir()
    (root / "manifest.json").write_text(
        """
        {
          "extensions": [
            {
              "id": "events-ok",
              "scripts": ["events.js"],
              "permissions": {"observability": {"events": true}}
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "manifest.json")

    from api.extensions import inject_extension_tags

    injected = inject_extension_tags("<html><head></head><body></body></html>")

    assert '"permissions":{"observability":{"events":true}}' in injected
    assert injected.index("window.__HERMES_EXTENSION_CONFIG__") < injected.index("/extensions/events.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_subscription_behavior_and_permission_gate():
    start = BOOT_JS.index("const _HERMES_EXTENSION_EVENT_TYPES")
    end = BOOT_JS.index("// ── Turn-based voice mode (#1333)")
    region = BOOT_JS[start:end]

    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const results = {{}};
        global.window = {{}};
        global.document = {{ getElementById: () => null }};
        {region}

        const publish = (type, payload) => window._publishHermesExtensionEvent(type, payload);

        results.apiPresent = !!window.HermesExtensionEvents && typeof window.HermesExtensionEvents.subscribe === 'function';
        results.badHandlerRejected = window.HermesExtensionEvents.subscribe('tool', null) === false;
        results.unknownTypeRejected = window.HermesExtensionEvents.subscribe('metering', () => {{}}) === false;
        results.noConfigSubscribe = typeof window.HermesExtensionEvents.subscribe('tool', () => {{}}) === 'function';

        delete window.__HERMES_EXTENSION_CONFIG__;
        const noConfigEvents = [];
        const noConfigUnsub = window.HermesExtensionEvents.subscribe('tool', event => noConfigEvents.push(event));
        results.noConfigPublishFalse = publish('tool', {{session_id: 'sid-no-config'}}) === false;
        results.noConfigNoDelivery = noConfigEvents.length === 0;
        noConfigUnsub();

        window.__HERMES_EXTENSION_CONFIG__ = {{
          extensions: [{{
            effective_enabled: true,
            status: 'enabled',
            permissions: {{storage: {{owned: true}}}}
          }}]
        }};
        const deniedEvents = [];
        const deniedUnsub = window.HermesExtensionEvents.subscribe('tool', event => deniedEvents.push(event));
        results.missingPermissionFalse = publish('tool', {{session_id: 'sid-denied'}}) === false;
        results.missingPermissionNoDelivery = deniedEvents.length === 0;
        deniedUnsub();

        window.__HERMES_EXTENSION_CONFIG__ = {{
          extensions: [{{
            effective_enabled: true,
            status: 'enabled',
            permissions: {{observability: {{events: true}}}}
          }}]
        }};

        const toolEvents = [];
        const arrayEvents = [];
        const allEvents = [];
        const toolUnsub = window.HermesExtensionEvents.subscribe('tool', event => toolEvents.push(event));
        const arrayUnsub = window.HermesExtensionEvents.subscribe(['tool', 'approval'], event => arrayEvents.push(event.type));
        const allUnsub = window.HermesExtensionEvents.subscribe(null, event => allEvents.push(event.type));

        results.toolPublishTrue = publish('tool', {{session_id: 'sid-1', nested: {{count: 1}}}}) === true;
        results.toolFilter = toolEvents.length === 1 && toolEvents[0].type === 'tool';
        results.toolTimestamp = typeof toolEvents[0].timestamp === 'string' && toolEvents[0].timestamp.length > 0;
        results.toolSession = toolEvents[0].session_id === 'sid-1';
        results.toolPayloadCloned = toolEvents[0].payload.nested.count === 1;

        results.approvalPublishTrue = publish('approval', {{session_id: 'sid-2', nested: {{count: 2}}}}) === true;
        results.arrayFilter = arrayEvents.join(',') === 'tool,approval';
        results.nullFilter = allEvents.join(',') === 'tool,approval';

        toolUnsub();
        arrayUnsub();
        allUnsub();

        const cloneEvents = [];
        window.HermesExtensionEvents.subscribe('cancel', event => {{
          event.type = 'mutated';
          event.payload.nested.count = 99;
          cloneEvents.push('mutator');
        }});
        window.HermesExtensionEvents.subscribe('cancel', event => {{
          cloneEvents.push({{
            type: event.type,
            count: event.payload.nested.count,
            injected: event.payload.injected,
          }});
        }});
        results.clonePublishTrue = publish('cancel', {{session_id: 'sid-3', nested: {{count: 1}}}}) === true;
        results.cloneReadOnly = cloneEvents.length === 2 &&
          cloneEvents[1].type === 'cancel' &&
          cloneEvents[1].count === 1 &&
          cloneEvents[1].injected === undefined;

        let streamEndHits = 0;
        const streamEndUnsub = window.HermesExtensionEvents.subscribe('stream_end', () => {{ streamEndHits += 1; }});
        results.unsubscribeReturnsFunction = typeof streamEndUnsub === 'function';
        results.unsubscribeBefore = publish('stream_end', {{session_id: 'sid-4'}}) === true && streamEndHits === 1;
        streamEndUnsub();
        publish('stream_end', {{session_id: 'sid-4'}});
        results.unsubscribeAfter = streamEndHits === 1;

        console.log(JSON.stringify(results));
        """
    )

    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=30,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr or proc.stdout}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["apiPresent"] is True
    assert out["badHandlerRejected"] is True
    assert out["unknownTypeRejected"] is True
    assert out["noConfigSubscribe"] is True
    assert out["noConfigPublishFalse"] is True
    assert out["noConfigNoDelivery"] is True
    assert out["missingPermissionFalse"] is True
    assert out["missingPermissionNoDelivery"] is True
    assert out["toolPublishTrue"] is True
    assert out["toolFilter"] is True
    assert out["toolTimestamp"] is True
    assert out["toolSession"] is True
    assert out["toolPayloadCloned"] is True
    assert out["approvalPublishTrue"] is True
    assert out["arrayFilter"] is True
    assert out["nullFilter"] is True
    assert out["clonePublishTrue"] is True
    assert out["cloneReadOnly"] is True
    assert out["unsubscribeReturnsFunction"] is True
    assert out["unsubscribeBefore"] is True
    assert out["unsubscribeAfter"] is True
