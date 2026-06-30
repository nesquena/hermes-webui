"""DEFAULT-OFF safety contract for the opt-in Git controls UI (#2668 salvage).

This is the load-bearing test for the salvage: it locks in the "zero behaviour
when off" guarantee so a future edit cannot silently turn the feature on, leak a
background git poll, or render the Git/Changes surface for users who never opted
in. It asserts via source/structural checks (no browser needed), mirroring the
repo's existing static-source-assertion test style.

The four gates verified here:
  (i)   config: ``workspace_git_enabled`` defaults to ``False`` (and is a bool key).
  (ii)  POLL gate: ``_installWorkspaceGitAutoRefresh`` early-returns on
        ``!window._workspaceGitEnabled`` AND its only call site is inside
        ``_applyWorkspaceGitVisibility`` (never at module top-level) — so with
        the flag off there is no interval and no /api/git traffic.
  (iii) markup: the Changes-tab button is ``hidden`` by default in index.html.
  (iv)  RENDER gate: ``_applyWorkspaceGitVisibility`` hides every git surface
        when the flag is false, and the render entry points no-op when off.
"""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
STATIC = REPO_ROOT / "static"


@pytest.fixture(scope="module")
def workspace_js():
    return (STATIC / "workspace.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def boot_js():
    return (STATIC / "boot.js").read_text(encoding="utf-8")


def _function_body_span(src, signature):
    """Return (start, end) char offsets of the {...} body of a JS function.

    ``signature`` is the literal function header up to and including the
    parameter-list ``(`` — e.g. ``"function _applyWorkspaceGitVisibility("``.
    First the parameter list is skipped (so a default like ``opts={}`` is not
    mistaken for the body), then brace matching runs from the body's ``{``.
    """
    head = src.index(signature)
    # Position of the param-list open paren is the last char of the signature.
    paren = head + len(signature) - 1
    assert src[paren] == "(", f"signature must end with '(': {signature!r}"
    # Match parens to find the end of the parameter list.
    pdepth = 0
    for i in range(paren, len(src)):
        if src[i] == "(":
            pdepth += 1
        elif src[i] == ")":
            pdepth -= 1
            if pdepth == 0:
                params_end = i
                break
    else:
        raise AssertionError(f"unbalanced parens for {signature!r}")
    brace = src.index("{", params_end)
    depth = 0
    for i in range(brace, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return brace, i + 1
    raise AssertionError(f"unbalanced braces for {signature!r}")


# ---------------------------------------------------------------------------
# Gate (i) — config defaults the feature OFF.
# ---------------------------------------------------------------------------

def test_config_defaults_workspace_git_disabled():
    import api.config as cfg

    assert "workspace_git_enabled" in cfg._SETTINGS_DEFAULTS, (
        "workspace_git_enabled must be declared in _SETTINGS_DEFAULTS"
    )
    assert cfg._SETTINGS_DEFAULTS["workspace_git_enabled"] is False, (
        "workspace_git_enabled must default to False (opt-in feature)"
    )
    # Coerced as a boolean setting so the API can't be tricked into a truthy str.
    assert "workspace_git_enabled" in cfg._SETTINGS_BOOL_KEYS


def test_config_source_declares_default_false_inline():
    # Belt-and-braces: assert the literal default in source too, so an in-place
    # flip of the value is caught even if import-time merging changes.
    src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert '"workspace_git_enabled": False' in src


# ---------------------------------------------------------------------------
# Gate (ii) — POLL gate: no background git poll unless explicitly enabled.
# ---------------------------------------------------------------------------

def test_install_auto_refresh_early_returns_when_disabled(workspace_js):
    start, end = _function_body_span(workspace_js, "function _installWorkspaceGitAutoRefresh(")
    body = workspace_js[start:end]
    assert "if(!window._workspaceGitEnabled)return;" in body, (
        "_installWorkspaceGitAutoRefresh must early-return when the flag is off"
    )
    # The guard must come BEFORE the interval is installed, or the poll leaks.
    assert body.index("if(!window._workspaceGitEnabled)return;") < body.index("setInterval"), (
        "the disabled-guard must precede setInterval so no interval is created when off"
    )


def test_auto_refresh_only_installed_from_visibility_gate(workspace_js):
    """The ONLY call site of _installWorkspaceGitAutoRefresh() must be inside
    _applyWorkspaceGitVisibility() — never at module top-level. Otherwise a
    poll could start at load time regardless of the flag."""
    call = "_installWorkspaceGitAutoRefresh()"

    # Collect real call sites: exclude the definition and any comment lines.
    call_lines = []
    for lineno, line in enumerate(workspace_js.splitlines(), start=1):
        if call not in line:
            continue
        if "function " + call.rstrip("()") + "(" in line:  # definition
            continue
        if line.lstrip().startswith("//"):  # documentation
            continue
        call_lines.append((lineno, line))

    assert len(call_lines) == 1, (
        f"expected exactly one call site for {call}; found {len(call_lines)}: "
        f"{[ln for ln, _ in call_lines]}"
    )
    _, call_line = call_lines[0]

    # Must be indented (inside a function), never a bare module-top statement.
    assert call_line[0].isspace(), (
        "the auto-refresh installer must be called from inside a function, "
        "not at module top-level"
    )

    # And specifically inside _applyWorkspaceGitVisibility's body.
    vis_start, vis_end = _function_body_span(
        workspace_js, "function _applyWorkspaceGitVisibility("
    )
    # Offset of the (single) call occurrence (skip the function-definition match).
    call_offset = workspace_js.index(call + ";")
    if not (vis_start < call_offset < vis_end):
        # index() may have landed on something earlier; re-scan for the in-span one.
        positions = [m.start() for m in re.finditer(re.escape(call + ";"), workspace_js)]
        assert any(vis_start < p < vis_end for p in positions), (
            "the auto-refresh installer call must live inside "
            "_applyWorkspaceGitVisibility()"
        )


def test_auto_refresh_runner_and_status_refresh_short_circuit(workspace_js):
    # The poll runner bails immediately when off.
    a_start, a_end = _function_body_span(workspace_js, "async function _autoRefreshWorkspaceGitStatus(")
    assert "if(!window._workspaceGitEnabled)return;" in workspace_js[a_start:a_end]

    # refreshGitStatus returns null immediately when off (no network call).
    r_start, r_end = _function_body_span(workspace_js, "async function refreshGitStatus(")
    body = workspace_js[r_start:r_end]
    assert "if(!window._workspaceGitEnabled)return null;" in body
    assert body.index("if(!window._workspaceGitEnabled)return null;") < body.index("/api/git/status"), (
        "refreshGitStatus must short-circuit before issuing the /api/git/status call"
    )


# ---------------------------------------------------------------------------
# Gate (iii) — markup hidden by default.
# ---------------------------------------------------------------------------

def test_changes_tab_markup_hidden_by_default(index_html):
    # Locate the Changes-tab button element and assert it carries `hidden`.
    m = re.search(r'<button[^>]*id="btnWorkspaceChangesTab"[^>]*>', index_html)
    assert m, "btnWorkspaceChangesTab element not found in index.html"
    tag = m.group(0)
    assert "hidden" in tag, "Changes tab button must be hidden by default"

    # The git surfaces are display:none in static markup until the gate reveals.
    assert re.search(r'id="gitChangesView"[^>]*style="display:none"', index_html), (
        "gitChangesView must start display:none"
    )
    assert re.search(r'id="gitBranchControl"[^>]*style="display:none"', index_html), (
        "gitBranchControl must start display:none"
    )
    assert re.search(r'id="gitDiffView"[^>]*style="display:none"', index_html), (
        "gitDiffView must start display:none"
    )
    assert re.search(r'id="gitBranchMenu"[^>]*hidden', index_html), (
        "gitBranchMenu must start hidden"
    )


# ---------------------------------------------------------------------------
# Gate (iv) — RENDER gate hides all surfaces when the flag is false.
# ---------------------------------------------------------------------------

def test_visibility_gate_hides_all_surfaces_when_off(workspace_js):
    start, end = _function_body_span(workspace_js, "function _applyWorkspaceGitVisibility(")
    body = workspace_js[start:end]

    # The disabled branch.
    assert "if(!window._workspaceGitEnabled){" in body
    disabled_branch = body[body.index("if(!window._workspaceGitEnabled){"):]

    # Every git surface is hidden / reset inside the disabled branch.
    assert "changesTab.hidden=true" in disabled_branch
    assert "control.style.display='none'" in disabled_branch
    assert "changesView.style.display='none'" in disabled_branch
    assert "diffView.style.display='none'" in disabled_branch
    # And if we were on the Changes tab, we fall back to Files.
    assert "switchWorkspacePanelTab('files')" in disabled_branch
    # The disabled branch returns before any poll install / refresh.
    assert "return;" in disabled_branch
    assert disabled_branch.index("return;") < (
        body.index("_installWorkspaceGitAutoRefresh()") - body.index("if(!window._workspaceGitEnabled){")
    ), "the off-branch must return before installing the poll"


def test_render_entry_points_no_op_when_off(workspace_js):
    # renderGitChanges short-circuits when off.
    rc_start, rc_end = _function_body_span(workspace_js, "function renderGitChanges(")
    assert "if(!window._workspaceGitEnabled)return;" in workspace_js[rc_start:rc_end]

    # _gitStatusForPath returns null when off (keeps tree badges inert).
    gp_start, gp_end = _function_body_span(workspace_js, "function _gitStatusForPath(")
    assert "if(!window._workspaceGitEnabled)return null;" in workspace_js[gp_start:gp_end]

    # renderGitBadge does not paint the gated badge UI when off.
    rb_start, rb_end = _function_body_span(workspace_js, "function renderGitBadge(")
    assert "if(!window._workspaceGitEnabled" in workspace_js[rb_start:rb_end]

    # switchWorkspacePanelTab('changes') only honored when flag AND repo present.
    sw_start, sw_end = _function_body_span(workspace_js, "function switchWorkspacePanelTab(")
    sw_body = workspace_js[sw_start:sw_end]
    assert "window._workspaceGitEnabled && git && git.status && git.status.is_git" in sw_body
    # Otherwise it falls back to files.
    assert "tab = 'files';" in sw_body


def test_boot_default_branch_forces_flag_false(boot_js):
    # The settings-load failure / signed-out path must set the flag false.
    assert "window._workspaceGitEnabled=false;" in boot_js
