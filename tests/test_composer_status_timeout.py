import json
import re
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"could not extract {name}")


def _composer_status_source(source: str) -> str:
    owner = "let _composerStatusTimer=null;"
    owner_start = source.index(owner)
    function_start = source.index("function setComposerStatus(", owner_start)
    function_source = _function_source(source, "setComposerStatus")
    assert owner_start < function_start
    return source[owner_start : function_start + len(function_source)]


def test_composer_status_timeout_is_race_safe():
    composer_status_source = _composer_status_source(UI_JS)
    script = textwrap.dedent(
        f"""
        const statusElement = {{
          style: {{display: 'none'}},
          textContent: '',
          classList: {{remove() {{}}}},
          removeAttribute() {{}},
        }};
        globalThis.window = {{_composerControlVisibility: {{}}}};
        globalThis.$ = (id) => id === 'composerStatus' ? statusElement : null;

        let now = 0;
        let nextTimerId = 1;
        const timers = new Map();
        globalThis.setTimeout = (fn, delay) => {{
          const id = nextTimerId++;
          timers.set(id, {{fn, due: now + delay}});
          return id;
        }};
        globalThis.clearTimeout = (id) => timers.delete(id);
        function advance(ms) {{
          const target = now + ms;
          while (true) {{
            let next = null;
            for (const [id, timer] of timers) {{
              if (timer.due <= target && (!next || timer.due < next.timer.due)) {{
                next = {{id, timer}};
              }}
            }}
            if (!next) break;
            timers.delete(next.id);
            now = next.timer.due;
            next.timer.fn();
          }}
          now = target;
        }}
        function visible() {{
          return statusElement.style.display !== 'none' && statusElement.textContent;
        }}
        function expect(condition, message) {{
          if (!condition) throw new Error(message);
        }}

        {composer_status_source}

        setComposerStatus('Reconnected', 1000);
        expect(visible() === 'Reconnected', 'timed status should be visible immediately');
        advance(999);
        expect(visible() === 'Reconnected', 'timed status should remain visible before expiry');
        advance(1);
        expect(!visible(), 'timed status should hide at expiry');

        setComposerStatus('Old status', 1000);
        advance(500);
        setComposerStatus('New status');
        advance(500);
        expect(visible() === 'New status', 'old timer must not clear a newer untimed status');

        setComposerStatus('Same status', 1000);
        advance(500);
        setComposerStatus('Same status', 1000);
        advance(500);
        expect(visible() === 'Same status', 'repeated timed status needs a fresh timeout');
        advance(500);
        expect(!visible(), 'repeated timed status should hide after its fresh timeout');

        setComposerStatus('Untimed status');
        advance(5000);
        expect(visible() === 'Untimed status', 'untimed status must remain visible');

        setComposerStatus('Clear me', 1000);
        setComposerStatus('');
        expect(!visible(), 'explicit clear must hide immediately');
        advance(1000);
        expect(!visible(), 'explicit clear must cancel the owned timer');

        setComposerStatus('Hide me', 1000);
        window._composerControlVisibility.hide_composer_status = true;
        setComposerStatus('Still hidden');
        expect(!visible(), 'hidden-status preference must hide the status');
        advance(1000);
        expect(!visible(), 'hidden-status preference must cancel timed expiry');
        window._composerControlVisibility.hide_composer_status = false;

        process.stdout.write(JSON.stringify({{ok: true}}));
        """
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"ok": True}


def test_messages_wires_all_timed_composer_statuses_through_shared_owner():
    reconnected_calls = re.findall(
        r"setComposerStatus\('Reconnected'(?:,[^)]*)?\);", MESSAGES_JS
    )
    assert reconnected_calls == [
        "setComposerStatus('Reconnected',1000);",
        "setComposerStatus('Reconnected',1000);",
    ]
    assert "setComposerStatus('Reconnected');" not in MESSAGES_JS
    assert (
        "setComposerStatus(`${d.message||'Warning'}`,d.type==='fallback'?4000:undefined);"
        in MESSAGES_JS
    )
    assert "setTimeout(()=>setComposerStatus(''),4000)" not in MESSAGES_JS
