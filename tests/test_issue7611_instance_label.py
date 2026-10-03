"""Regression coverage for issue #7611: per-deployment instance
label distinguishes multi-instance browser tabs and desktop
windows. The label is installation-scoped, sourced from the env
var ``HERMES_WEBUI_INSTANCE_NAME`` or ``config.yaml``'s
``instance_name`` / ``webui.instance_name``, and exposed via
``GET /api/settings`` as ``settings["instance_label"]`` so the
WebUI can prefix the title without overloading the assistant
name or the profile identity.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _load_routes():
    # Import lazily so test collection stays cheap; the module is
    # the same one already loaded by sibling tests, so this is a
    # cache hit after the first call.
    import api.routes as routes
    return routes


def _install_config(monkeypatch, tmp_path: Path, config: dict) -> Path:
    """Write an INSTALLATION-scoped config.yaml and point the resolver at it.

    The helper under test must never read the request profile's config, so
    these tests pin the installation config via ``HERMES_CONFIG_PATH`` (the
    deployment-level override the whole config layer honours). Writing a real
    file also means the race-safe disk read is genuinely exercised rather
    than a monkeypatched in-memory dict.
    """
    path = tmp_path / "installation-config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    return path


def _purge_settings_cache():
    """``/api/settings`` calls ``load_settings()`` which may cache.
    We don't need a fresh load for these tests — we just need
    ``_read_instance_label()`` to re-evaluate env / config on
    every call. The helper itself reads the installation config
    off disk on each invocation, so no module-level cache (in
    ``api.routes`` or ``api.config``) needs clearing."""
    pass


# ── _read_instance_label: env precedence ──────────────────────────────────────


def test_read_instance_label_env_var_beats_installation_config(monkeypatch, tmp_path):
    """The env var is the primary source — it lets operators
    distinguish Production / Staging / Dev without editing
    config.yaml. Order of precedence is env > config > empty,
    so the env value always wins when set."""
    import api.routes as routes
    monkeypatch.setenv("HERMES_WEBUI_INSTANCE_NAME", "Production")
    _install_config(monkeypatch, tmp_path, {"instance_name": "ConfigShouldBeIgnored"})
    assert routes._read_instance_label() == "Production", (
        "env var must take precedence over config.yaml so operators "
        "can override without editing the file"
    )


def test_read_instance_label_strips_whitespace(monkeypatch):
    """The env var is allowed to have surrounding whitespace from
    shell quoting or env-file editors; the helper trims so
    ``'   Production  '`` does not render as ``'   Production  •
    Hermes'`` in the tab title."""
    import api.routes as routes
    monkeypatch.setenv("HERMES_WEBUI_INSTANCE_NAME", "   Staging   ")
    assert routes._read_instance_label() == "Staging"


def test_read_instance_label_falls_back_to_config_top_level(monkeypatch, tmp_path):
    """When the env var is unset, the helper falls back to
    ``config.yaml``'s top-level ``instance_name`` key. This
    matches the configuration shape suggested in the issue body."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(monkeypatch, tmp_path, {"instance_name": "Dev"})
    assert routes._read_instance_label() == "Dev"


def test_read_instance_label_falls_back_to_config_nested(monkeypatch, tmp_path):
    """The helper also accepts ``webui.instance_name`` because
    some operators prefer a sectioned config layout. The
    nested key is only consulted when the top-level key is
    absent or empty."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(monkeypatch, tmp_path, {"webui": {"instance_name": "Staging"}})
    assert routes._read_instance_label() == "Staging"


def test_read_instance_label_expands_env_placeholder(monkeypatch, tmp_path):
    """A ``${VAR}`` placeholder in ``instance_name`` must resolve to the
    variable's value, exactly like every other config read does
    (``api.config._expand_env_vars``).

    A raw YAML read leaves the reference untouched, so an operator deploying
    with ``instance_name: ${DEPLOYMENT_NAME}`` gets the literal
    ``${DEPLOYMENT_NAME}`` in the browser tab instead of their deployment
    name — the placeholder is a deployment-time idiom, and the title is where
    the user actually sees it.
    """
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setenv("HERMES_TEST_DEPLOYMENT_NAME", "Prod-East")
    _install_config(
        monkeypatch,
        tmp_path,
        {
            "webui": {
                "instance_name": "Deployment-${HERMES_TEST_DEPLOYMENT_NAME}",
            }
        },
    )
    label = routes._read_instance_label()
    assert label == "Deployment-Prod-East", (
        f"placeholder must be expanded to the variable's value, got {label!r}"
    )
    assert "${" not in label, (
        f"the raw placeholder must never reach the title, got {label!r}"
    )


def test_read_instance_label_keeps_unresolvable_placeholder_literal(monkeypatch, tmp_path):
    """An UNDEFINED variable is left as the literal reference rather than
    expanded to an empty string — a label that silently became
    ``"Deployment-"`` would look like a misconfigured title, whereas the
    placeholder at least tells the operator which variable failed to resolve.
    This also pins that expansion never raises out of the helper.
    """
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.delenv("HERMES_TEST_DEFINITELY_UNSET", raising=False)
    _install_config(
        monkeypatch,
        tmp_path,
        {"instance_name": "Build-${HERMES_TEST_DEFINITELY_UNSET}"},
    )
    label = routes._read_instance_label()
    assert label == "Build-${HERMES_TEST_DEFINITELY_UNSET}"


def test_read_instance_label_returns_empty_when_unset(monkeypatch, tmp_path):
    """When neither env var nor config keys are set, the helper
    returns the empty string. The frontend treats the empty
    string as "no label" and leaves the default title
    untouched — this is the contract that keeps single-instance
    deployments behaviorally identical to before this PR."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(monkeypatch, tmp_path, {"instance_name": "", "webui": {}})
    assert routes._read_instance_label() == "", (
        "empty/unset must round-trip as empty so the frontend can "
        "branch on length without a separate sentinel"
    )


def test_read_instance_label_handles_config_exception(monkeypatch, tmp_path):
    """A malformed installation config.yaml must degrade to the empty
    default rather than raise — ``/api/settings`` must never 500
    because of a broken config. Read off disk (not via the
    ambient ``get_config()`` cache), so this exercises the real
    parse-failure path of the race-safe reader."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    bad = tmp_path / "installation-config.yaml"
    bad.write_text("instance_name: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(bad))
    assert routes._read_instance_label() == "", (
        "a config read failure must degrade to the empty default; "
        "/api/settings should not 500 because of a broken config"
    )


def test_read_instance_label_rejects_non_string_config_values(monkeypatch, tmp_path):
    """A non-string ``instance_name`` (int, dict, list) must
    not be coerced; the helper skips it and tries the next
    candidate. A future operator who accidentally sets
    ``instance_name: 42`` should not see ``'42 • Hermes'`` in
    their tab title."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(
        monkeypatch, tmp_path, {"instance_name": 42, "webui": {"instance_name": ["bad", "shape"]}}
    )
    assert routes._read_instance_label() == "", (
        "non-string candidates must be skipped so a malformed "
        "config value cannot become a tab-title fragment"
    )


def test_read_instance_label_ignores_request_profile_config(monkeypatch, tmp_path):
    """Re-gate finding 1: the label must be INSTALLATION-scoped, so a
    request profile's ``config.yaml`` must never be consulted — not
    even indirectly through the ambient ``get_config()``, which
    resolves the active (request) profile's file and is exactly the
    profile-scoped lookup the reviewer reproduced (default profile
    → "Deployment", named profile → "AliceProfile").

    Here the installation config carries the deployment label and the
    *active profile's* home carries a different one. Whatever the
    active profile resolves to, the helper must return the
    installation value."""
    import api.routes as routes
    import api.profiles as profiles

    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)

    base = tmp_path / "hermes-home"
    alice = base / "profiles" / "alice"
    alice.mkdir(parents=True)
    (base / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "Deployment"}), encoding="utf-8"
    )
    (alice / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "AliceProfile"}), encoding="utf-8"
    )

    # Point the profiler at the temp installation while the request profile
    # is a named one, and make HERMES_CONFIG_PATH ambient-free so resolution
    # has to go through the base-home path.
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_active_profile", "alice")

    assert routes._read_instance_label() == "Deployment", (
        "the label must come from the installation/base Hermes home, never from "
        "the active request profile's config.yaml"
    )


def test_read_instance_label_stable_across_profiles(monkeypatch, tmp_path):
    """Re-gate finding 1 (the actual reported symptom): on one
    installation, the default profile and every named profile must
    agree on the label. Each profile here declares its OWN
    ``instance_name``; the installation root's value is the only one
    that may survive, so both resolutions must return it."""
    import api.routes as routes
    import api.profiles as profiles

    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)

    base = tmp_path / "hermes-home"
    for name in ("default-home", "alice", "bob"):
        (base / "profiles" / name).mkdir(parents=True)
    (base / "config.yaml").write_text(
        yaml.safe_dump({"webui": {"instance_name": "Production"}}), encoding="utf-8"
    )
    (base / "profiles" / "alice" / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "AliceProfile"}), encoding="utf-8"
    )
    (base / "profiles" / "bob" / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "BobProfile"}), encoding="utf-8"
    )

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)

    labels = {}
    for name in ("default", "alice", "bob"):
        monkeypatch.setattr(profiles, "_active_profile", name)
        labels[name] = routes._read_instance_label()

    assert labels == {"default": "Production", "alice": "Production", "bob": "Production"}, (
        "every profile on one installation must resolve the SAME label; "
        f"got {labels} — the label is profile-scoped again"
    )


def test_read_instance_label_env_var_beats_profile_env_override(monkeypatch, tmp_path):
    """Re-gate finding 2: a profile's own ``.env`` must not be able to
    rewrite the deployment label. ``_reload_dotenv()`` loads the
    active profile's ``.env`` into ``os.environ`` (and that value is
    what ``_read_instance_label()`` reads first), so the deployment
    label has to survive the profile ``.env`` load — otherwise
    whichever profile loads last wins."""
    import api.routes as routes

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    base = tmp_path / "hermes-home"
    alice = base / "profiles" / "alice"
    alice.mkdir(parents=True)
    (base / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "Deployment"}), encoding="utf-8"
    )
    # Alice's profile .env tries to claim the deployment label for herself.
    (alice / ".env").write_text(
        "HERMES_WEBUI_INSTANCE_NAME=AliceProfileEnv\nSOME_OTHER_KEY=ok\n", encoding="utf-8"
    )

    import api.profiles as profiles

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)

    # The loader records what it loaded in a MODULE-LEVEL set and pops every
    # one of those keys on the next call — so a test that leaves it polluted
    # lets a later profile reload delete environment values belonging to
    # another test (or to the server). Snapshot and restore both halves: the
    # keys it tracked, and the env values it overwrote.
    prev_loaded = profiles._loaded_profile_env_keys
    preexisting = {
        k: os.environ.get(k)
        for k in ("HERMES_WEBUI_INSTANCE_NAME", "SOME_OTHER_KEY")
    }

    profiles._reload_dotenv(alice)

    try:
        assert os.environ.get("SOME_OTHER_KEY") == "ok", (
            "non-protected profile .env keys must still load normally"
        )
        assert os.environ.get("HERMES_WEBUI_INSTANCE_NAME") != "AliceProfileEnv", (
            "a profile .env must not be able to override the deployment-level instance label"
        )
        assert routes._read_instance_label() == "Deployment", (
            "the deployment label must survive a profile .env load"
        )
    finally:
        # Restore the exact prior state so this test cannot influence any
        # other test through the loader's global (greptile P2).
        profiles._loaded_profile_env_keys = prev_loaded
        for k, v in preexisting.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_profile_env_cannot_override_protected_instance_label_via_runtime_env(tmp_path):
    """Re-gate finding 2, second door: ``get_profile_runtime_env()`` projects
    a profile's ``.env`` onto the background worker's environment. The
    deployment label must not ride along there either, or the worker sees
    a per-profile label."""
    import api.profiles as profiles

    alice = tmp_path / "profiles" / "alice"
    alice.mkdir(parents=True)
    (alice / ".env").write_text(
        "HERMES_WEBUI_INSTANCE_NAME=AliceProfileEnv\nMY_PROFILE_KEY=value1\n", encoding="utf-8"
    )
    env = profiles.get_profile_runtime_env(alice)
    assert "HERMES_WEBUI_INSTANCE_NAME" not in env, (
        "the runtime env must not carry the deployment instance label out of a profile .env"
    )
    assert env.get("MY_PROFILE_KEY") == "value1"


# ── frontend wiring ──────────────────────────────────────────────────────────


def test_instance_label_round_trips_through_window_global():
    """The boot path must read ``s.instance_label`` from the
    ``/api/settings`` response and pin it on
    ``window._instanceLabel`` so the synchronous ``applyBotName``
    and the later ``syncTopbar`` calls can both prefix the
    title without re-reading the settings payload."""
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    assert "window._instanceLabel=(s.instance_label||'').trim();" in src, (
        "boot.js must read s.instance_label from /api/settings and "
        "expose it on window._instanceLabel"
    )


def test_apply_bot_name_prefixes_with_instance_label():
    """``applyBotName`` must consume ``window._instanceLabel``
    when writing ``document.title`` for the no-session branch
    so the empty-state tab title reflects the deployment."""
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "applyBotName")
    assert "window._instanceLabel" in body, (
        "applyBotName must consult window._instanceLabel"
    )
    assert "_instanceLabel+' \\u2022 '+document.title" in body or (
        "_instanceLabel+' • '+document.title"
    ) in body, (
        "applyBotName must prefix the document.title with "
        "'<label> • <name>' so the empty-state tab is distinguishable"
    )
    # The prefix is applied in-place after the bare title is
    # written, so the same code path covers empty and non-empty
    # labels.
    assert body.count("_instanceLabel") >= 2, (
        "the prefix branch must consult _instanceLabel so the "
        "empty and non-empty cases share the same code path"
    )


def test_apply_bot_name_prefix_is_guarded_by_no_session():
    """Re-gate finding 3: the prefix must live INSIDE the ``!S.session``
    branch.

    ``applyBotName()`` runs on every boot and profile switch, but while a
    chat session is open ``syncTopbar()`` is the sole owner of
    ``document.title`` (#4086). An unconditional prefix re-reads the
    title some other owner already composed and stacks on top of it, so
    two calls turn
    ``Production • Chat — Hermes`` into
    ``Production • Production • Production • Chat — Hermes``.

    Static lock on the shape: the prefix statement's line must be nested
    in the same block that writes the bare ``name``.
    """
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "applyBotName")
    # Every prefix site must come after a no-session guard opening.
    assert re.search(r"if\s*\(\s*!\s*S\.session\s*\)\s*\{", body), (
        "applyBotName must open an explicit !S.session block that owns the "
        "document.title write (and the instance-label prefix) so the prefix "
        "cannot be applied to a title syncTopbar() already composed"
    )

    bare = body.index("document.title=name;")
    prefix = body.index("_instanceLabel+' \\u2022 '+document.title")
    assert bare < prefix, (
        "the bare title must be written before it is prefixed"
    )
    # The guard that opens the block containing the bare write must precede
    # the prefix, and the prefix must sit inside that same brace block.
    guard = body.index("if(!S.session){")
    assert guard < bare < prefix, (
        "the instance-label prefix must be applied inside the !S.session "
        "branch that wrote the bare name — otherwise it accumulates on a "
        "session title owned by syncTopbar()"
    )
    block = body[guard:prefix]
    assert block.count("{") > block.count("}") or block.endswith("{")


def _run_node_apply_bot_name(calls: int, label: str, session: bool) -> dict:
    """Drive the REAL applyBotName() from static/boot.js in a Node VM."""
    driver = r"""
const fs = require('fs');
const boot = fs.readFileSync(process.argv[1], 'utf8');

function extractFunc(name) {
  let i = boot.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found');
  let depth = 0, started = false;
  let start = i;
  for (let j = i; j < boot.length; j++) {
    if (boot[j] === '{') { if (!started) { start = j + 1; started = true; } depth++; }
    else if (boot[j] === '}') { depth--; if (depth === 0) return boot.slice(start, j); }
  }
  throw new Error(name + ' did not terminate');
}

let _title = '';
let _titleWrites = 0;
const document = {
  get title() { return _title; },
  set title(v) { _title = v; _titleWrites++; },
  querySelector() { return null; },
};
const window = { _instanceLabel: process.argv[2] };
let S = { session: process.argv[3] === 'with-session' ? { title: 'Chat' } : null };
const $ = () => null;
function assistantDisplayName() { return 'Hermes'; }
function _applyBusyComposerPlaceholder() {}

const _applyBotName = new Function(
  'document', 'window', 'S', '$', 'assistantDisplayName', '_applyBusyComposerPlaceholder',
  extractFunc('applyBotName')
);
const titles = [];
// S is captured by reference through the closure, so mutating it per call
// mirrors a live profile/session switch the way the browser would.
// document.title is a LIVE property, so the value is snapshotted AFTER the
// call returns while a write flag records whether the call touched it at
// all: a caller that composes the title in two steps (bare write then prefix)
// leaves exactly one final value per call -- never both intermediate writes
// -- and a branch that never writes the title (a session is open, so
// syncTopbar owns it) leaves NO entry at all.
for (let n = 0; n < Number(process.argv[4]); n++) {
  const _before = _titleWrites;
  _applyBotName(document, window, S, $, assistantDisplayName, _applyBusyComposerPlaceholder);
  // Snapshot the LIVE property value only when this call actually wrote
  // it: a caller that composes the title in two steps leaves exactly one
  // final value per call, and a branch that never writes the title (a
  // session is open, so syncTopbar owns it) leaves NO entry at all.
  if (_titleWrites > _before) titles.push(_title);
}
console.log(JSON.stringify(titles));

"""
    if NODE is None:  # pragma: no cover - guarded by skipif markers
        raise RuntimeError("node not on PATH")
    r = subprocess.run(
        [NODE, "-e", driver, str(ROOT / "static" / "boot.js"), label,
         "with-session" if session else "no-session", str(calls)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_is_idempotent_for_instance_label():
    """Re-gate finding 3, behavioral: two real applyBotName() calls must
    not stack the prefix. The reviewer reproduced
    ``Production • Production • Production • Chat — Hermes`` from a
    version that prefix-fed an existing title."""
    titles = _run_node_apply_bot_name(calls=2, label="Production", session=False)
    assert titles == ["Production • Hermes", "Production • Hermes"], (
        "applyBotName must rebuild the bare title before prefixing it, so "
        f"repeated calls are idempotent; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_does_not_accumulate_after_three_calls():
    """Same contract, wider: three calls still yield exactly one prefix."""
    titles = _run_node_apply_bot_name(calls=3, label="Production", session=False)
    assert all(t == "Production • Hermes" for t in titles), (
        f"the prefix must never accumulate; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_leaves_active_session_title_alone():
    """#4086 + #7611: with a session open, applyBotName() must not touch
    document.title at all — syncTopbar() owns it, and it applies the label
    prefix itself. A record of no title writes proves both."""
    titles = _run_node_apply_bot_name(calls=2, label="Production", session=True)
    assert titles == [], (
        "applyBotName must not write document.title while a session is "
        f"active (the prefix would accumulate on syncTopbar's title); got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_empty_label_keeps_bare_title():
    """Empty label → the default title is byte-identical to pre-PR output."""
    titles = _run_node_apply_bot_name(calls=2, label="", session=False)
    assert titles == ["Hermes", "Hermes"], (
        f"an unset label must leave the title untouched; got {titles}"
    )


def test_sync_topbar_prefixes_with_instance_label():
    """``syncTopbar`` must consume the same label for both the
    no-session and with-session title paths so the tab title
    stays distinguishable while a session is open."""
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "syncTopbar")
    assert "window._instanceLabel" in body, (
        "syncTopbar must consult window._instanceLabel"
    )
    # Both title assignments (no-session and with-session) must
    # apply the same in-place prefix so the empty and non-empty
    # label branches stay consistent.
    assert body.count("_instanceLabel+' \\u2022 '+document.title") >= 2 or (
        body.count("_instanceLabel+' • '+document.title") >= 2
    ), (
        "syncTopbar must apply the instance-label prefix to every "
        "document.title assignment; found fewer than two prefix sites"
    )


def test_instance_label_is_not_writable_from_post_settings(monkeypatch):
    """The label is installation-scoped and must not be
    editable from the WebUI settings UI. The POST handler at
    ``/api/settings`` accepts ``bot_name`` and other user-tunable
    fields, but the ``instance_label`` field must not be
    consumed from the request body. A user submitting
    ``{instance_label: 'CustomLabel'}`` must not have it
    applied; only the env var and config.yaml control the
    value."""
    src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    # The GET path reads the label from the helper; the POST
    # path must not interpret it. The simplest check: the
    # docstring for bot_name handling explicitly mentions the
    # field name, but instance_label must not appear in any
    # 'body[...]' reads inside the POST handler.
    # Concretely: find the POST /api/settings block and ensure
    # it does not branch on "instance_label" in body.
    assert '"instance_label" in body' not in src, (
        "instance_label must not be a writable body field on "
        "/api/settings POST — the value is installation-scoped"
    )
    # And the GET path must surface it from the helper.
    assert "settings[\"instance_label\"] = _read_instance_label()" in src, (
        "/api/settings GET must surface instance_label so the "
        "frontend can prefix the title"
    )


# ── helpers ─────────────────────────────────────────────────────────────────

# ── re-gate finding 4: non-chat panel titles keep the label ──────────────────


def test_sync_app_titlebar_prefixes_panel_titles_with_instance_label():
    """Re-gate finding 4, static: ``syncAppTitlebar`` must prefix the
    instance label onto the title it builds for non-chat panels.

    The reviewer's reproduction: on the chat panel the label shows up
    (``syncTopbar`` owns the title there), but the moment the user
    switches to Settings / Tasks / Kanban the tab title reverts to a
    bare ``Settings -- Hermes``. ``syncAppTitlebar`` REBUILDS
    ``document.title`` from the panel name instead of appending to an
    existing title, so a label that lived only in the boot paths was
    silently discarded exactly while the user is configuring a
    multi-instance install.

    Locked shape: the prefix statement must sit inside the same
    non-chat block that (re)builds ``document.title``, so the label is
    applied to a title this function owns and the chat branch -- which
    never reaches it -- cannot accumulate prefixes.
    """
    src = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "syncAppTitlebar")
    assert "window._instanceLabel" in body, (
        "syncAppTitlebar must consult window._instanceLabel"
    )
    guard = body.index("if (panel !== 'chat') {")
    bare = body.index("document.title = bot ? mainText + ' \\u2014 ' + bot : mainText;")
    prefix = body.index("_instanceLabel+' \\u2022 '+document.title")
    assert guard < bare < prefix, (
        "the instance-label prefix must be applied inside the "
        "non-chat branch, AFTER that branch rebuilt document.title from "
        "the panel name -- otherwise the label is dropped on every "
        "non-chat panel"
    )
    block = body[guard:prefix]
    assert block.count("{") > block.count("}") or block.endswith("{")
    # Only one prefix site: the chat panel never reaches the block, so a
    # second prefix site could only mean prefixing a title the function
    # did not just rebuild -- which is how finding 3's accumulation began.
    assert body.count("_instanceLabel+' \\u2022 '+document.title") == 1, (
        "syncAppTitlebar must apply the prefix at exactly one site, "
        "inside the branch that owns the title rebuild"
    )


def _run_node_sync_app_titlebar(
    panel: str, label: str, session_title: str = "", calls: int = 1
) -> dict:
    """Drive the REAL syncAppTitlebar() from static/panels.js in a Node VM."""
    driver = r"""
const fs = require('fs');
const panels = fs.readFileSync(process.argv[1], 'utf8');

function extractFunc(name) {
  let i = panels.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found');
  let depth = 0, started = false;
  let start = i;
  for (let j = i; j < panels.length; j++) {
    if (panels[j] === '{') { if (!started) { start = j + 1; started = true; } depth++; }
    else if (panels[j] === '}') { depth--; if (depth === 0) return panels.slice(start, j); }
  }
  throw new Error(name + ' did not terminate');
}

// Copy the module-level APP_TITLEBAR_KEYS map VERBATIM so the extracted
// body resolves the real panel->i18n-key table rather than a test copy.
function extractConst(name) {
  const marker = 'const ' + name + ' = ';
  let i = panels.indexOf(marker);
  if (i < 0) throw new Error(name + ' not found');
  let depth = 0;
  for (let j = i + marker.length; j < panels.length; j++) {
    const ch = panels[j];
    if (ch === '{' || ch === '[' || ch === '(') depth++;
    else if (ch === '}' || ch === ']' || ch === ')') depth--;
    else if (ch === ';' && depth === 0) return panels.slice(i, j + 1);
  }
  throw new Error(name + ' did not terminate');
}

function fakeEl() {
  return {
    textContent: '', hidden: false, value: '', className: '', type: '',
    style: {}, onclick: null, onblur: null, onkeydown: null,
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
    appendChild() {}, replaceWith() {}, remove() {}, focus() {}, select() {},
    contains() { return false; },
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0 }; },
  };
}

let _title = '';
let _titleWrites = 0;
const document = {
  get title() { return _title; },
  set title(v) { _title = v; _titleWrites++; },
  // appTitlebarSub is deliberately absent so the mocked surface stays
  // minimal; the title assertions do not depend on it.
  getElementById(id) { return id === 'appTitlebarTitle' ? fakeEl() : null; },
  querySelector() { return null; },
  createElement() { return fakeEl(); },
  createTextNode(t) { return { text: t }; },
  removeEventListener() {}, addEventListener() {},
  body: { appendChild() {} },
};
const window = { _instanceLabel: process.argv[2], innerWidth: 1280 };
const S = { session: process.argv[4] ? { title: process.argv[4] } : null,
            messages: [] };
const _currentPanel = process.argv[5];
const _renamingAppTitlebar = false;
const t = (key) => key;
function assistantDisplayName() { return 'Hermes'; }

const _syncAppTitlebar = new Function(
  'document', 'window', 'S', 't', 'assistantDisplayName',
  '_currentPanel', '_renamingAppTitlebar',
  // Inline the const (not a parameter name -- that would be a
  // redeclaration SyntaxError) and wrap the extracted body so it
  // returns nothing, exactly like the real function.
  extractConst('APP_TITLEBAR_KEYS') + '\n' +
  extractFunc('syncAppTitlebar')
);
const titles = [];
for (let n = 0; n < Number(process.argv[3]); n++) {
  const _before = _titleWrites;
  _syncAppTitlebar(document, window, S, t, assistantDisplayName,
                   _currentPanel, _renamingAppTitlebar);
  // Snapshot the LIVE property value only when this call actually wrote
  // it: a caller that composes the title in two steps (bare rebuild then
  // prefix) leaves exactly one final value per call, never both
  // intermediate writes; a branch that never writes the title (chat
  // panel: syncTopbar owns it) leaves NO entry at all.
  if (_titleWrites > _before) titles.push(_title);
}
console.log(JSON.stringify(titles));

"""
    if NODE is None:  # pragma: no cover - guarded by skipif markers
        raise RuntimeError("node not on PATH")
    r = subprocess.run(
        [NODE, "-e", driver, str(ROOT / "static" / "panels.js"), label,
         str(calls), session_title, panel],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


# The non-chat panels the sidebar can land on, including ones with no
# APP_TITLEBAR_KEYS entry (they fall back to the capitalised name).
_ALL_NON_CHAT_PANELS = sorted(
    {"settings", "skills", "memory", "tasks", "kanban", "workspaces",
     "profiles", "insights", "logs", "todos", "plugin"}
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("panel", _ALL_NON_CHAT_PANELS)
def test_panel_title_keeps_instance_label_prefix(panel):
    """Re-gate finding 4, behavioral: every non-chat panel must carry the
    label in the tab title. Walking the real panel list means a panel later
    added to the sidebar without the prefix fails here."""
    titles = _run_node_sync_app_titlebar(panel, "Production")
    assert titles, f"panel {panel} must write document.title"
    assert all(t.startswith("Production \u2022 ") for t in titles), (
        f"panel '{panel}' lost the instance-label prefix; got {titles}"
    )
    # Exactly one prefix, and the panel name still follows it.
    assert titles[0].count("Production \u2022 ") == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_title_prefix_is_idempotent():
    """Three consecutive syncAppTitlebar() calls on one panel must not stack
    the prefix: the function rebuilds the title from the panel name before
    prefixing it, so later calls are no-ops."""
    titles = _run_node_sync_app_titlebar("settings", "Production", calls=3)
    assert len(titles) == 3 and len(set(titles)) == 1, (
        f"the panel prefix must never accumulate; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_title_without_instance_label_is_bare():
    """Empty label: the panel title is byte-identical to the pre-PR output."""
    titles = _run_node_sync_app_titlebar("settings", "")
    assert titles == ["tab_settings \u2014 Hermes"], (
        f"an unset label must leave the panel title untouched; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_title_prefix_does_not_clobber_session_title():
    """A panel switch after a session was open must not stack the prefix on
    syncTopbar()'s "<session> -- <assistant>" title: the non-chat branch
    rebuilds the title from the panel name first, so the session half is
    replaced, never accumulated."""
    titles = _run_node_sync_app_titlebar(
        "settings", "Production", session_title="My chat"
    )
    assert titles == ["Production \u2022 tab_settings \u2014 Hermes"], (
        "switching panels must replace, not stack on, the session title; "
        f"got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_chat_panel_title_is_owned_by_sync_topbar():
    """The chat panel must NOT touch document.title at all: syncTopbar() is
    the sole owner of "<session> -- <assistant>" and applies the label
    itself. A title write here would double-prefix the session title."""
    titles = _run_node_sync_app_titlebar(
        "chat", "Production", session_title="My chat", calls=2
    )
    assert titles == [], (
        "syncAppTitlebar must not write document.title on the chat panel "
        f"(syncTopbar owns it and applies the label itself); got {titles}"
    )



def _extract_function_body(src: str, name: str) -> str:
    """Inline copy of the same helper used in the issue #7352
    test file — keeps this file self-contained and avoids
    pulling in a private test-utility module."""
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    paren = src.find("(", start)
    assert paren != -1
    depth = 0
    for idx in range(paren, len(src)):
        ch = src[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = src.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_a_profile_dotenv_cannot_repoint_installation_configuration(tmp_path, monkeypatch):
    """A profile must not be able to hijack installation-scoped config (#7611).

    ``_installation_config_path`` reads ``HERMES_CONFIG_PATH`` from the LIVE
    environment, and a profile activation writes that profile's ``.env`` into
    ``os.environ`` via ``_reload_dotenv``. So a profile whose ``.env`` sets
    ``HERMES_CONFIG_PATH`` to its own config.yaml repointed installation
    configuration at a profile file — and because ``_read_installation_config``
    reads ``instance_name`` from whatever that path resolves to, the
    installation label could change per profile. That is exactly the
    profile-scoping the installation-scoped label exists to avoid.

    The protected-key list is the mechanism: the same one that already keeps
    ``HERMES_WEBUI_INSTANCE_NAME`` unspoofable.
    """
    from api import profiles as prof

    assert "HERMES_CONFIG_PATH" in prof._PROTECTED_ENV_KEYS, (
        "HERMES_CONFIG_PATH resolves installation-scoped config, so a profile "
        ".env must not be able to set it"
    )

    # Drive the real loader: a profile .env carrying the key must leave the
    # process env untouched, and must not be recorded as a loaded key (so a
    # later restore cannot drop the operator's value either — greptile P2).
    prev_loaded = prof._loaded_profile_env_keys
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "installation.yaml"))
    profile_home = tmp_path / "profiles" / "sneaky"
    profile_home.mkdir(parents=True)
    sneaky_cfg = tmp_path / "sneaky-config.yaml"
    sneaky_cfg.write_text("instance_name: hijacked\n", encoding="utf-8")
    (profile_home / ".env").write_text(
        f"HERMES_CONFIG_PATH={sneaky_cfg}\nSOME_OTHER_KEY=x\n", encoding="utf-8"
    )

    prev_overridden = getattr(prof, "_profile_overridden_env", {})
    preexisting = {
        k: os.environ.get(k)
        for k in ("HERMES_CONFIG_PATH", "SOME_OTHER_KEY")
    }

    def _restore_profile_env_state():
        prof._loaded_profile_env_keys = prev_loaded
        prof._profile_overridden_env = prev_overridden
        for k, v in preexisting.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    try:
        prof._reload_dotenv(profile_home)

        assert os.environ.get("HERMES_CONFIG_PATH") == str(tmp_path / "installation.yaml"), (
            "a profile .env overwrote the installation config path"
        )
        assert "HERMES_CONFIG_PATH" not in (prof._loaded_profile_env_keys or set()), (
            "the protected key was recorded as loader state, so a later profile "
            "reload could remove the operator's value"
        )
        # The unrelated key is still projected, so protection is scoped to the
        # installation-level key and does not freeze the profile's whole env.
        assert os.environ.get("SOME_OTHER_KEY") == "x"
    finally:
        _restore_profile_env_state()

    # And the label resolver therefore cannot be steered by the profile.
    routes = _load_routes()
    installer = tmp_path / "installation.yaml"
    installer.write_text("instance_name: real-deployment\n", encoding="utf-8")
    assert routes._read_instance_label() == "real-deployment"


# ── #7655: base-config placeholders resolve against operator env only ───────


def test_base_config_placeholder_cannot_be_claimed_by_profile_dotenv(tmp_path, monkeypatch):
    """#7655: an installation-scoped ``${VAR}`` placeholder must not resolve
    against a profile's ``.env``.

    ``_read_installation_config()`` expands ``${VAR}`` the way every other
    config read does — but the ambient expander (``api.config._expand_env_vars``)
    consults the request thread's profile env FIRST and then process env, and
    ``api.profiles._reload_dotenv`` projects a named profile's ``.env`` into
    process env. With base config ``instance_name: ${SLOT_NAME}`` and Alice's
    profile ``.env`` carrying ``SLOT_NAME=AliceProfile``, the advertised
    installation label became profile-owned — and the next process-wide
    profile switch could change it again.

    The fix resolves placeholders exclusively against operator-owned env
    (the process env minus whatever ``_reload_dotenv`` projected), and fails
    closed to the literal ``${VAR}`` when nothing owns the name.
    """
    import api.profiles as prof
    import api.routes as routes

    # Snapshot/restore the loader's globals and the keys it touches, so this
    # test cannot influence any other test through the process env.
    prev_loaded = prof._loaded_profile_env_keys
    preexisting = {k: os.environ.get(k) for k in ("SLOT_NAME", "OPERATOR_SLOT")}

    base = tmp_path / "hermes-home"
    alice = base / "profiles" / "alice"
    alice.mkdir(parents=True)
    (base / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "${SLOT_NAME}"}), encoding="utf-8"
    )
    # Alice's profile .env claims the label variable for herself.
    (alice / ".env").write_text(
        "SLOT_NAME=AliceProfile\nUNRELATED_PROFILE_KEY=x\n", encoding="utf-8"
    )
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(base / "config.yaml"))
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)

    try:
        prof._reload_dotenv(alice)
        # The profile's value IS in process env — that is the leak under test.
        assert os.environ.get("SLOT_NAME") == "AliceProfile"

        label = routes._read_instance_label()
        assert label != "AliceProfile", (
            "a profile .env claimed the installation-scoped label placeholder"
        )
        assert label == "${SLOT_NAME}", (
            "an unowned placeholder must fail closed to its literal form, "
            f"got {label!r}"
        )

        # ── An operator value present at launch still honours ──────────
        os.environ["OPERATOR_SLOT"] = "prod-slot-01"
        (base / "config.yaml").write_text(
            yaml.safe_dump({"instance_name": "${OPERATOR_SLOT}"}), encoding="utf-8"
        )
        assert routes._read_instance_label() == "prod-slot-01", (
            "an operator-owned value must still resolve, not be mistaken for "
            "profile injection"
        )

        # ── A later profile switch must not change the operator label ───
        bob = base / "profiles" / "bob"
        bob.mkdir(parents=True)
        (bob / ".env").write_text("OPERATOR_SLOT=BobProfile\n", encoding="utf-8")
        prof._reload_dotenv(bob)
        assert routes._read_instance_label() == "prod-slot-01", (
            "a process-wide profile switch changed the installation label"
        )
    finally:
        # Restore the loader's globals and the env values it overwrote.
        prof._loaded_profile_env_keys = prev_loaded
        for k, v in preexisting.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_base_config_placeholder_operator_value_survives_profile_key_tracking(tmp_path, monkeypatch):
    """#7655 companion: the loader must keep tracking the profile key it
    projected (so a later restore can drop it) WITHOUT that bookkeeping
    becoming a denial-of-service on the operator's own value.

    ``_reload_dotenv`` records every non-protected key it projected in
    ``_loaded_profile_env_keys``; the next call pops those keys. When the
    operator's value lives in process env from launch, a profile switch must
    not be able to erase it — the loader only pops keys a profile .env
    actually set.
    """
    import api.profiles as prof

    prev_loaded = prof._loaded_profile_env_keys
    preexisting = {k: os.environ.get(k) for k in ("OPERATOR_SLOT", "SOME_OTHER_KEY")}

    base = tmp_path / "hermes-home"
    alice = base / "profiles" / "alice"
    alice.mkdir(parents=True)
    (alice / ".env").write_text("SOME_OTHER_KEY=from-alice\n", encoding="utf-8")
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("OPERATOR_SLOT", "operator-value")

    try:
        prof._reload_dotenv(alice)
        # The profile key is tracked; the operator key is not.
        assert "SOME_OTHER_KEY" in (prof._loaded_profile_env_keys or set())
        assert os.environ.get("SOME_OTHER_KEY") == "from-alice"
        # Switching to a profile with no matching key pops Alice's key but
        # leaves the operator's value intact.
        bob = base / "profiles" / "bob"
        bob.mkdir(parents=True)
        prof._reload_dotenv(bob)
        assert os.environ.get("OPERATOR_SLOT") == "operator-value"
        assert "SOME_OTHER_KEY" not in os.environ
    finally:
        prof._loaded_profile_env_keys = prev_loaded
        for k, v in preexisting.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
