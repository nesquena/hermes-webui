"""Browser vault card rendering: every prompt kind renders without throwing.

Runs the real ``_showVaultCardImpl`` from static/messages.js under Node with a
minimal DOM stub, so a scoping or reference error in one kind's branch (the
save-login card once read a block-scoped ``ident`` outside its block) fails
here instead of silently leaving the agent waiting for an answer that the user
was never asked for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MESSAGES_JS = Path(__file__).resolve().parent.parent / "static" / "messages.js"
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name} function not found in static/messages.js"
    brace = source.find("{", start)
    depth = 0
    for idx in range(brace, len(source)):
        if source[idx] == "{":
            depth += 1
        elif source[idx] == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"{name} function body not closed")


_HARNESS = r"""
class El {
  constructor(tag, id) { this.tagName = tag; this.id = id || ''; this.children = []; this.attrs = {};
    this.value = ''; this._html = ''; this.classList = new Set(); this.classList.contains = this.classList.has; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); registry[c.id] = c; return c; }
  focus() {}
  set innerHTML(v) { this._html = v; for (const c of this.children) delete registry[c.id]; this.children = []; }
  get innerHTML() { return this._html; }
}
const registry = {};
for (const id of ['vaultCard', 'vaultHeading', 'vaultHint', 'vaultQuestion', 'vaultPrimaryInput', 'vaultFields', 'msg'])
  registry[id] = new El('div', id);
const document = { createElement: (tag) => new El(tag), activeElement: null };
const $ = (id) => registry[id] || null;
const t = (key) => key;
const S = { session: { session_id: 'sid-1' } };
let _vaultSessionId = null, _vaultPromptId = null, _vaultKind = null, _vaultSignature = '', _vaultVisibleSince = 0;
const _notifyPromptCard = () => {};
const _vaultBelongsToActiveSession = () => true;
const _ensureVaultCardDom = () => registry.vaultCard;
const _startVaultCountdown = () => {};
const _clearVaultCountdownTimer = () => {};
const _setPromptFlyoutHidden = () => {};
const _vaultSetControlsDisabled = () => {};
const respondVault = () => {};
"""


@pytest.mark.skipif(
    NODE is None, reason="node is required for the vault card render check"
)
@pytest.mark.parametrize("kind", ["unlock", "save_login", "code"])
def test_every_vault_card_kind_renders(kind):
    impl = _extract_function(
        MESSAGES_JS.read_text(encoding="utf-8"), "_showVaultCardImpl"
    )
    pending = {
        "kind": kind,
        "display_name": "1Password",
        "site": "github.com",
        "origin": "https://github.com",
        "label": "GitHub",
        "prompt_id": f"p-{kind}",
        "session_id": "sid-1",
        "timeout_seconds": 120,
    }
    script = (
        _HARNESS
        + impl
        + f"""
_showVaultCardImpl({json.dumps(pending)});
_showVaultCardImpl({json.dumps(pending)});  // re-render with the fields already present
console.log(JSON.stringify({{
  visible: registry.vaultCard.classList.has('visible'),
  identifier: !!registry.vaultIdentifierInput,
  primaryType: registry.vaultPrimaryInput.attrs.type || null,
}}));
"""
    )
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["visible"]
    assert state["identifier"] is (kind == "save_login")
    if kind == "unlock":
        assert state["primaryType"] == "password"


def test_vault_card_has_no_debug_toasts():
    assert "[vault-debug]" not in MESSAGES_JS.read_text(encoding="utf-8")
