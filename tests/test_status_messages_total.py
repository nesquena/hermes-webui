"""Behavioural regression tests for the /status card message counts.

``/status`` reports a *visible* message count (hidden internal rows such as
quiet-delegation wakeups removed). It must ALSO surface the raw persisted
``message_count`` whenever the two differ, so a user can see the true size of a
conversation instead of only the filtered count.

The status card is rendered entirely client-side, so the function is extracted
from ``static/commands.js`` and executed under node with stubbed session globals
(same probe technique as ``tests/test_status_command_card.py``). The probe
asserts the emitted rows, not the source text of the function.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"Could not extract {name}()")


def _run_node(source: str) -> str:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _status_card_cases() -> dict:
    body = _function_body(COMMANDS_JS, "_statusCardFromSession")
    probe = """
// Stubs for the session globals and the unrelated formatters the card reads.
// They are NOT under test; only the message-count rows are.
const t = (key) => key;
// The loaded transcript for the no-count-fields case: 5 rows total, of which a
// tool row and a hidden delegation wakeup must NOT be counted. Expected visible
// count is therefore 3. Non-trivial on purpose: a fallback that always returned
// 0, or the raw length, would fail the assertion below.
const _bareTranscript = [
  {role:'user',     _source:null},
  {role:'assistant',_source:null},
  {role:'tool',     _source:null},
  {role:'assistant',_source:'delegation_wakeup'},
  {role:'assistant',_source:null},
];
const S = {activeProfile:'default', currentDir:'/ws', busy:false, activeStreamId:null, lastUsage:{}, messages:_bareTranscript};
const window = {};
const $ = () => ({value:''});
function _statusProviderForSession(s) { return s.model_provider || ''; }
function _formatStatusTimestamp(v) { return 'TS'; }
function _formatStatusTokens(s) { return 'TOK'; }

""" + body + """

function rowsFor(session) {
  const card = _statusCardFromSession(session);
  const out = {};
  for (const row of card.rows) { out[row.label] = row.value; }
  return out;
}

const base = {session_id:'s1', title:'T', model:'m', model_provider:'p', personality:null,
  created_at:1, updated_at:2, input_tokens:0, output_tokens:0};

const cases = {
  // Hidden internal rows present: visible count and raw total differ.
  diverging: rowsFor({...base, visible_message_count:3, message_count:5}),
  // No hidden rows: both counts agree; no separate total row.
  agreeing: rowsFor({...base, visible_message_count:5, message_count:5}),
  // Legacy sidecar (no visible_message_count): total falls back to message_count.
  legacy: rowsFor({...base, message_count:7}),
  // Legacy sidecar with neither field: falls back to the loaded transcript.
  bare: rowsFor({...base}),
};
console.log(JSON.stringify(cases));
"""
    return json.loads(_run_node(probe))


def test_status_card_shows_total_when_it_differs_from_visible():
    case = _status_card_cases()["diverging"]
    assert case["status_messages"] == "3"
    assert case["status_messages_total"] == "5"


def test_status_card_omits_total_row_when_counts_agree():
    case = _status_card_cases()["agreeing"]
    assert case["status_messages"] == "5"
    assert "status_messages_total" not in case


def test_status_card_legacy_session_falls_back_to_message_count():
    case = _status_card_cases()["legacy"]
    assert case["status_messages"] == "7"
    assert "status_messages_total" not in case


def test_status_card_without_count_fields_falls_back_to_transcript():
    # Neither visible_message_count nor message_count on the session: the card
    # must count the loaded transcript, excluding tool rows and hidden
    # delegation wakeups. The stub transcript has 5 rows → 3 visible.
    # A fallback that always returned 0, or the raw row count (5), fails here.
    case = _status_card_cases()["bare"]
    assert case["status_messages"] == "3"
    assert "status_messages_total" not in case


def test_status_messages_total_label_is_translated():
    # The key must exist in the English locale map so t() has a fallback for
    # every other locale (t() falls back to LOCALES.en).
    assert "status_messages_total: 'Total messages'" in I18N_JS


def _locales_defining(key: str) -> set:
    """Locale codes whose own map declares `key`.

    The render probe below only visits locales that already define the key, so
    it cannot notice a locale that silently falls back to English.
    """
    probe = """
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = {window:{}, document:{documentElement:{lang:''}, addEventListener(){},
    querySelectorAll(){return []}},
  navigator:{language:'en'}, localStorage:{getItem(){return null},setItem(){}}, console};
sandbox.window.localStorage = sandbox.localStorage;
vm.createContext(sandbox);
vm.runInContext(src + ";globalThis.__i18nProbe = {LOCALES};", sandbox);
const {LOCALES} = sandbox.__i18nProbe;
const out = [];
for (const code of Object.keys(LOCALES)) {
  const map = LOCALES[code];
  if (map && Object.prototype.hasOwnProperty.call(map, process.argv[3])) out.push(code);
}
console.log(JSON.stringify(out));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(probe)
        script_path = pathlib.Path(handle.name)
    if NODE is None:
        pytest.skip("node not on PATH")
    try:
        result = subprocess.run(
            [NODE, str(script_path), str(REPO_ROOT / "static" / "i18n.js"), key],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return set(json.loads(result.stdout.strip()))


def _render_labels_in_all_locales(key: str) -> dict:
    """Render `key` through the REAL i18n.js in every locale that defines it.

    A presence check (`grep -c`, or the substring assert above) only proves the
    key exists somewhere. This loads the classic script into a vm sandbox,
    switches each locale's map and calls the real `t()`, so a key that renders
    undefined, leaves a `{0}` placeholder, or silently falls back to English for
    a locale that *should* translate it is caught.
    """
    probe = """
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = {window:{}, document:{documentElement:{lang:''}, addEventListener(){},
    querySelectorAll(){return []}},
  navigator:{language:'en'}, localStorage:{getItem(){return null},setItem(){}}, console};
sandbox.window.localStorage = sandbox.localStorage;
vm.createContext(sandbox);
// `const LOCALES` in a classic script lands in the script's lexical scope, not on
// the sandbox object. Append an explicit export so the harness can read the maps
// and call the real t() — no re-implementation of the lookup.
vm.runInContext(src + ";globalThis.__i18nProbe = {LOCALES, t, setLocale};", sandbox);
const {LOCALES, t, setLocale} = sandbox.__i18nProbe;
const out = {};
for (const code of Object.keys(LOCALES)) {
  const map = LOCALES[code];
  if (!Object.prototype.hasOwnProperty.call(map, process.argv[3])) continue;
  setLocale(code);
  out[code] = t(process.argv[3]);
}
console.log(JSON.stringify(out));
"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(probe)
        script_path = pathlib.Path(script.name)
    if NODE is None:
        script_path.unlink(missing_ok=True)
        pytest.skip("node not on PATH")
    node_bin = str(NODE)
    try:
        result = subprocess.run(
            [node_bin, str(script_path), str(REPO_ROOT / "static" / "i18n.js"), key],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip())


def test_status_messages_total_renders_in_every_locale_that_defines_it():
    rendered = _render_labels_in_all_locales("status_messages_total")
    assert "en" in rendered, "English must define the key as the fallback"
    assert rendered["en"] == "Total messages"
    for code, label in rendered.items():
        assert label, f"locale {code} rendered an empty label"
        assert "undefined" not in label, f"locale {code} rendered undefined"
        assert "{0}" not in label, f"locale {code} left an unresolved placeholder"
    # The two locales greptile flagged were silently falling back to English.
    # Pin their own translations so a regression is visible rather than inferred.
    assert rendered.get("es") == "Mensajes totales"
    assert rendered.get("de") == "Nachrichten gesamt"
    assert rendered.get("zh") == "消息总数"
    # Own-key coverage, not just "defined somewhere": the probe above skips a
    # locale whose map lacks the key, so a missing translation would silently
    # pass as an English fallback. Every locale that translates the sibling
    # `status_messages` label must carry `status_messages_total` too.
    locales = _locales_defining("status_messages")
    assert "zh-Hant" in locales, sorted(locales)
    # Simplified Chinese does not translate `status_messages`, so the render
    # probe cannot reach it; its entry is pinned directly above.
    assert "status_messages_total: '消息总数'" in I18N_JS
    missing = sorted(locales - set(rendered))
    assert "zh" not in missing, "zh defines status_messages_total but rendered no label"
    assert not missing, f"locales translating status_messages but not status_messages_total: {missing}"


def test_status_message_keys_present_in_es_and_de():
    # t() falls back to English when a locale lacks the key, so a missing
    # translation is a silent language mix rather than a crash. Pin the two
    # locales that shipped without it.
    assert "status_messages: 'Mensajes'" in I18N_JS
    assert "status_messages_total: 'Mensajes totales'" in I18N_JS
    assert "status_messages: 'Nachrichten'" in I18N_JS
    assert "status_messages_total: 'Nachrichten gesamt'" in I18N_JS
