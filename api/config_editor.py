"""Raw config.yaml viewer/editor for the WebUI Settings > System panel.

Scope (kept deliberately small, mirrors the read-only viewer proposed
upstream in nesquena/hermes-webui#5088, but adds a gated write path):

  * GET  /api/config/raw — returns the active profile's config.yaml as raw
    text (comments preserved), with credential-shaped values replaced by a
    placeholder and the list of redacted key paths.
  * PUT  /api/config/raw — writes a new config.yaml, but only when an
    operator has opted in via ``HERMES_WEBUI_ALLOW_CONFIG_RAW_WRITE`` AND the
    submitted YAML does not touch security-critical keys (auth/security
    sections, trusted proxies, allowlists). This keeps a compromised WebUI
    session from using the raw editor to disable its own gates.

Both endpoints operate on the *raw text* of config.yaml rather than
round-tripping through ``yaml.safe_dump`` — that is what preserves comments
and key ordering across a save.
"""

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WRITE_GATE_ENV = "HERMES_WEBUI_ALLOW_CONFIG_RAW_WRITE"
_REDACTED_PLACEHOLDER = "•••REDACTED•••"
# Path segment used when the KEY itself was credential material. The
# manifest tells the operator WHERE something was redacted; spelling the
# secret out to do so would undo the redaction one field over.
_REDACTED_KEY_SEGMENT = "<redacted-key>"
_BACKUP_SUFFIX = ".webui-editor-bak"

# Key names whose value is credential-shaped. Matched as a substring of the
# key name after stripping underscores/hyphens and lowercasing, so
# "api_key", "apiKey", "API-KEY", "access_token", "client_secret", and
# "password_hash" all match — case-insensitive substring, as specified.
#
# This vocabulary is deliberately DEFENSIVE, not minimal. Over-matching only
# hides a value the operator can still read on disk; under-matching hands a
# live credential to the browser while the response claims it was redacted.
# The earlier four-entry list missed the most concrete real case in this
# codebase: literal `Authorization` headers on MCP servers are supported
# configuration today, and `authorization` contains none of apikey/token/
# secret/password.
_SENSITIVE_SUBSTRINGS = (
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "authorization",
    "auth",  # bearer/basic/oauth blocks, `auth:` mappings
    "credential",
    "cookie",
    "privatekey",
    "signingkey",
    "accesskey",
    "sessionkey",
    "clientsecret",
    "bearer",
    "salt",
    "signature",
)



class ConfigEditorError(Exception):
    """Raised for any GET/PUT config-editor failure; carries an HTTP status
    and optional extra JSON fields (blocked_paths, line/column, ...)."""

    def __init__(self, message: str, *, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


# ── Path resolution ───────────────────────────────────────────────────────


def _active_profile_config_path() -> Path:
    """THE config path — whatever ``api.config`` says it is.

    This used to reconstruct ``<active_home>/config.yaml`` in production and
    defer to the real resolver only when a test had monkeypatched it (detected
    by module name). Two things were wrong with that. It dropped
    ``HERMES_CONFIG_PATH``, so with an override set the editor read and wrote
    one file while ``reload_config()`` — which does use the real resolver —
    observed another: a save reported success against a file the runtime never
    looked at. And because the branch existed only when NOT under test, every
    test exercised the other path, so nothing covered the production one.

    ``_get_config_path()`` already prefers the override and falls back to the
    active profile home, which is exactly the intended behaviour.
    """
    from api.config import _get_config_path

    return _get_config_path()


def _write_enabled() -> bool:
    return os.getenv(_WRITE_GATE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# ── Redaction (GET) ──────────────────────────────────────────────────────


# Walk bound. The memo key carries the path so a node reached through several
# sensitive paths reports all of them; that makes an aliased DAG re-walkable,
# so cap the total work and fail closed rather than redact half a document.
_MAX_REDACTION_STEPS = 200_000


def _segment_is_sensitive(segment: str) -> bool:
    norm = re.sub(r"[_\-]", "", str(segment).strip().strip("'\"")).lower()
    return any(term in norm for term in _SENSITIVE_SUBSTRINGS)


def _sensitive_value_spans(text: str) -> tuple[list[tuple[int, int]], list[str]]:
    """Return the raw-text spans of every credential-shaped VALUE, plus paths.

    Credential safety must not depend on a partial YAML grammar. The previous
    hand-written indentation/``key: value`` scanner had no idea about flow
    mappings, flow-sequence mappings or aliases, so ``options: {api_key: X}``,
    ``- {name: demo, access_token: X}`` and an aliased anchor all reached the
    browser verbatim while the response claimed the text was redacted.

    So the *structure* is resolved by the real parser (``yaml.compose``), which
    hands back nodes carrying exact source offsets, and only the rendering step
    touches raw text — by slicing out those offsets. That keeps comments,
    ordering and formatting intact (the whole point of a raw editor) while
    making the redaction decision on a complete parse rather than a guess.

    Raises ``yaml.YAMLError`` when the document cannot be parsed; the caller
    must fail closed rather than fall back to showing unredacted text.
    """
    import yaml

    node = yaml.compose(text)
    spans: list[tuple[int, int]] = []
    paths: list[str] = []
    seen_paths: set[str] = set()
    # Memo keyed by (id, sensitive, path). The path belongs in the key: with
    # only (id, sensitive), a node reached through TWO different sensitive
    # paths recorded just the first, so the manifest under-reported what had
    # been redacted. Bounded below so a heavily aliased document cannot turn
    # that completeness into unbounded work.
    visited: set[tuple[int, bool, tuple]] = set()
    steps = 0

    def record(path_segments: list) -> None:
        path_str = ".".join(str(seg) for seg in path_segments if seg != "")
        if path_str and path_str not in seen_paths:
            seen_paths.add(path_str)
            paths.append(path_str)

    def redact_scalar(scalar_node, path: list) -> None:
        if str(scalar_node.value):
            spans.append((scalar_node.start_mark.index, scalar_node.end_mark.index))
            record(path)

    def key_is_sensitive(key_node) -> bool:
        """Classify a key node, including a complex (non-scalar) one.

        A complex key used to be coerced to an empty segment, which dropped
        sensitivity for its whole subtree. Any sensitive scalar anywhere inside
        the key makes the entry sensitive — an unclassifiable key must never
        widen trust.
        """
        if isinstance(key_node, yaml.ScalarNode):
            return _segment_is_sensitive(str(key_node.value))
        if isinstance(key_node, yaml.SequenceNode):
            return any(key_is_sensitive(child) for child in key_node.value)
        if isinstance(key_node, yaml.MappingNode):
            return any(
                key_is_sensitive(k) or key_is_sensitive(v) for k, v in key_node.value
            )
        return False

    def walk(n, path: list, sensitive: bool, stack: frozenset) -> None:
        nonlocal steps
        if n is None:
            return
        steps += 1
        if steps > _MAX_REDACTION_STEPS:
            raise ConfigEditorError(
                "config.yaml is too deeply aliased to redact safely, so it "
                "cannot be shown without risking credential disclosure.",
                status=409,
            )
        key = (id(n), sensitive, tuple(path))
        if key in visited:
            return
        visited.add(key)
        if id(n) in stack:  # recursive anchor
            return
        stack = stack | {id(n)}
        if isinstance(n, yaml.ScalarNode):
            if sensitive:
                redact_scalar(n, path)
            return
        if isinstance(n, yaml.SequenceNode):
            for child in n.value:
                walk(child, path, sensitive, stack)
            return
        if isinstance(n, yaml.MappingNode):
            for key_node, value_node in n.value:
                is_scalar_key = isinstance(key_node, yaml.ScalarNode)
                key_text = str(key_node.value) if is_scalar_key else ""
                # A merge key (`<<: *defaults`) contributes the merged
                # mapping's entries at THIS level, so walk it under the
                # current path rather than treating "<<" as a real segment.
                if is_scalar_key and key_text == "<<":
                    walk(value_node, path, sensitive, stack)
                    continue
                child_sensitive = sensitive or key_is_sensitive(key_node)
                if is_scalar_key:
                    # Under a sensitive ancestor the KEY is credential material
                    # too — a secret can just as well be the mapping key as the
                    # value, and only the value was ever replaced. Note this is
                    # driven by the ANCESTOR: `api_key` itself is a field name,
                    # not a secret, so a sensitive key does not redact itself.
                    if sensitive:
                        # The key IS the credential here, so it must not travel
                        # into the manifest as a path segment — that returned
                        # the secret in the JSON `redacted` list while the YAML
                        # beside it was correctly redacted.
                        redact_scalar(key_node, path + [_REDACTED_KEY_SEGMENT])
                    # Under a sensitive ancestor the key is credential material,
                    # so it must not become a path segment for anything beneath
                    # it either — the manifest would carry it just the same.
                    segment = _REDACTED_KEY_SEGMENT if sensitive else key_text
                    walk(value_node, path + [segment], child_sensitive, stack)
                else:
                    # Complex key: no usable path segment. Treat both halves as
                    # data under a placeholder segment, and never let the
                    # missing name downgrade sensitivity.
                    walk(key_node, path + ["?"], child_sensitive, stack)
                    walk(value_node, path + ["?"], child_sensitive, stack)
            return

    walk(node, [], False, frozenset())
    return spans, paths


# Node properties that precede a scalar's content: an anchor definition, a tag,
# or both — in EITHER order (`&a !t v` and `!t &a v` are both valid). The old
# pattern only matched an anchor at the very start, so `!tag &name value` lost
# its anchor and every alias pointing at it became an unresolvable reference:
# the redacted document no longer parsed at all. Whatever properties are found
# are copied through verbatim, which preserves their original order.
_NODE_PROPERTY_PREFIX_RE = re.compile(
    r"^((?:(?:&[^\s\[\]{},]+|!<[^>]*>|![^\s\[\]{},]*)[ \t]+)+)"
)


def _redact_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace *spans* in *text* with the placeholder, right-to-left."""
    out = text
    for start, end in sorted(set(spans), reverse=True):
        raw = out[start:end]
        # Do not swallow trailing whitespace/newlines: a block scalar's span
        # runs to the end of its last line, and eating that newline would glue
        # the next key onto the redacted line.
        trailing_len = len(raw) - len(raw.rstrip())
        if trailing_len:
            raw = raw[:-trailing_len]
            end -= trailing_len
        # Anchor and/or tag live inside the value span (`&name value`,
        # `!tag &name value`). Keep the ANCHORS — aliases elsewhere must still
        # resolve — and drop the tags.
        #
        # A tag survives only to describe a value that no longer exists. For a
        # type-forcing tag it is actively fatal: `!!binary '•••REDACTED•••'`
        # makes the document fail to load, because the placeholder is not valid
        # base64. Same class of breakage for !!int / !!float / !!bool /
        # !!timestamp. The placeholder is a quoted string, so the honest
        # rendering is an untagged quoted string.
        anchor = ""
        match = _NODE_PROPERTY_PREFIX_RE.match(raw)
        if match:
            anchor = "".join(
                token + " "
                for token in match.group(1).split()
                if token.startswith("&")
            )
        # Always quote: the placeholder has to be a valid scalar inside a flow
        # mapping (`{api_key: '...'}`) as well as in block context.
        out = out[:start] + anchor + "'" + _REDACTED_PLACEHOLDER + "'" + out[end:]
    return out


def _redact_yaml_text(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, redacted_paths) for *text*.

    Formatting and comments survive because only the credential spans are
    rewritten. Raises ``yaml.YAMLError`` for an unparsable document.
    """
    if not text.strip():
        return text, []
    spans, paths = _sensitive_value_spans(text)
    return _redact_spans(text, spans), paths


# ── Denylist (PUT) ────────────────────────────────────────────────────────
#
# Guards security-critical keys against being changed through the raw
# editor, so a compromised/careless WebUI session cannot use it to disable
# auth, widen trust, or open an allowlist. A path is "denylisted" if any
# segment starts with "allow" (allowed_hosts, allowlist, allow_users, ...),
# if it is/starts with a top-level auth/security section, if it starts with
# webui.auth*/webui.security* (nested form), if its top-level key starts
# with one of the sensitive flat webui_* prefixes below, or if any segment
# equals "trusted_proxies". Once a path is denylisted the whole subtree from
# that point is compared as one unit (deep equality) rather than recursed
# into further.
#
# config.yaml does NOT nest WebUI settings under a `webui:` mapping — the
# codebase uses flat `webui_<name>` top-level keys throughout (see
# api/auth_oidc.py, api/streaming.py, api/gateway_chat.py). An earlier
# version of this denylist only matched the nested `webui.auth*` shape via
# `path[0] == "webui"`, which never matches a flat `webui_oidc` key — a
# proven bypass letting the raw editor rewrite auth/execution/routing
# settings the denylist was meant to protect. Matched as a prefix on the
# WHOLE top-level key name (path[0]), same as the auth/security rule above.
_SENSITIVE_WEBUI_FLAT_KEY_PREFIXES = (
    "webui_oidc",  # OIDC issuer/client_id — hijacking these is a full auth bypass (api/auth_oidc.py)
    "webui_auth",
    "webui_security",
    "webui_trusted",
    "webui_passkey",  # passkey auth enable/disable toggle (api/auth.py)
    "webui_prefill_messages_script",  # shell command run via shlex+subprocess on every session prefill — RCE (api/streaming.py)
    "webui_gateway",  # gateway proxy base URL / routing — SSRF + credential exfiltration (api/gateway_chat.py)
    "webui_chat_backend",  # switches chat traffic into gateway-proxied mode (api/gateway_chat.py)
)

# Bare (non-webui_-prefixed) top-level keys with the same auth/execution/
# routing sensitivity as one of the prefixes above, guarded pre-emptively.
# `prefill_messages_script` is read as a fallback alongside
# `webui_prefill_messages_script` by api/routes.py's Joplin-notes prefill
# path (_joplin_prefill_script_path); it is not (yet) consumed by the actual
# subprocess-executing path in api/streaming.py, so it is not exploitable
# today, but it is semantically the same RCE-shaped setting and denylisting
# only the webui_-prefixed sibling would silently stop protecting anything
# the moment a future change makes streaming.py honor the bare fallback too.
# A grep sweep for other bare/webui_-prefixed key pairs (oidc, gateway,
# chat_backend, passkey, auth, security, trusted) found no other instance
# of this pattern.
_SENSITIVE_BARE_KEY_PREFIXES = (
    "prefill_messages_script",
)

# Consumed capability/safety settings that do NOT start with "allow", so a
# name-prefix rule cannot see them. Each grants a surface or disables a guard:
#   toolsets / platform_toolsets — which CLI tool surface the agent may use
#                                  (hermes_cli/toolset_distributions.py)
#   restrict_evaluate            — the browser evaluate restriction
# Matched at any depth, as a whole segment. Prefixes were never going to be
# enough here: the dangerous settings are not named after their danger.
_CAPABILITY_SEGMENTS = frozenset({
    "toolsets",
    "platform_toolsets",
    "restrict_evaluate",
})

# Nested field names that begin with "allow" but are NOT a trust boundary —
# capability/filter knobs whose worst case is a non-working tool list. Keep this
# list short and justify every entry: everything not named here is denied.
_BENIGN_ALLOW_FIELD_NAMES = frozenset({
    "allowed_context_length",  # model capability hint, a number
    "allowed_mentions",        # message-formatting flag, not access control
})
# Deliberately NOT here: `allowed_tools`. It reads like a narrowing filter, but
# an editable allowlist grants as easily as it restricts — adding an entry hands
# the agent a tool the operator had withheld. That is a capability boundary, and
# it was the one example used to argue the nested rule could be relaxed at all.

_MISSING = object()


def _is_denylisted_path(path: tuple) -> bool:
    if not path:
        return False
    lowered = [str(s).lower() for s in path]
    if lowered[0].startswith("allow"):
        return True
    # Nested `allow*` is denylisted too, unless the field name is on the
    # proven-benign list.
    #
    # A previous revision made this rule top-level ONLY, because matching
    # `allow*` at every depth also blocked ordinary application fields such as
    # an MCP server's `allowed_tools`. That relaxation failed open for real
    # trust boundaries the installed agent consumes at exactly this shape:
    #   browser.allow_private_urls / security.allow_private_urls
    #       — the private-IP SSRF guard (tools/url_safety.py,
    #         hermes_cli/config.py); turning it on lets the agent reach
    #         localhost, LAN hosts and cloud metadata endpoints.
    #   <channel>.allowed_users / allowed_chats / allowed_groups /
    #   allowed_rooms / allowed_channels / allow_all_users / allow_bots
    #       — who may talk to the agent at all.
    # Denying by default and carving out the names shown to be harmless fails
    # closed; enumerating the dangerous ones cannot, because the next release
    # adds settings this file has never heard of.
    if any(
        seg.startswith("allow") and seg not in _BENIGN_ALLOW_FIELD_NAMES
        for seg in lowered[1:]
    ):
        return True
    if any(seg in _CAPABILITY_SEGMENTS for seg in lowered):
        return True
    if "trusted_proxies" in lowered:
        return True
    if lowered[0] in ("auth", "security"):
        return True
    if lowered[0] == "webui" and len(lowered) >= 2 and (
        lowered[1].startswith("auth") or lowered[1].startswith("security")
    ):
        return True
    if any(lowered[0].startswith(prefix) for prefix in _SENSITIVE_WEBUI_FLAT_KEY_PREFIXES):
        return True
    if any(lowered[0].startswith(prefix) for prefix in _SENSITIVE_BARE_KEY_PREFIXES):
        return True
    return False


def _find_denylist_violations(old: dict, new: dict) -> list[str]:
    violations: list[str] = []

    def walk(old_node: Any, new_node: Any, path: tuple) -> None:
        if path and _is_denylisted_path(path):
            if old_node != new_node:
                violations.append(".".join(path))
            return
        if isinstance(old_node, dict) or isinstance(new_node, dict):
            old_d = old_node if isinstance(old_node, dict) else {}
            new_d = new_node if isinstance(new_node, dict) else {}
            # Iterate the REAL keys and stringify only for the path.
            #
            # Collecting {str(k)} and then looking those strings up turned any
            # non-string key into a miss on both sides: a mapping under a
            # numeric key (`channels: {1: {allowed_users: [...]}}` — YAML makes
            # that an int) compared _MISSING against _MISSING, reported no
            # difference, and was never descended into. The denylist below it
            # was unreachable, so allowed_users under a numeric key was freely
            # editable.
            keys: list = []
            for key in list(old_d.keys()) + list(new_d.keys()):
                if key not in keys:
                    keys.append(key)
            for key in sorted(keys, key=lambda k: (str(type(k)), str(k))):
                walk(old_d.get(key, _MISSING), new_d.get(key, _MISSING), path + (str(key),))
        # Non-dict, non-denylisted differences are allowed edits — no-op.

    walk(old, new, ())
    return violations


# ── YAML error location ─────────────────────────────────────────────────


def _yaml_error_location(exc) -> tuple:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is None:
        return None, None
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    return (
        line + 1 if isinstance(line, int) else None,
        column + 1 if isinstance(column, int) else None,
    )


# ── Atomic write ─────────────────────────────────────────────────────────


def _write_config_atomic(config_path: Path, text: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = None
    if config_path.exists():
        backup_path = config_path.with_name(config_path.name + _BACKUP_SUFFIX)
        try:
            shutil.copy2(config_path, backup_path)
        except OSError:
            logger.warning("Failed to write config editor backup at %s", backup_path, exc_info=True)
        try:
            original_mode = stat.S_IMODE(config_path.stat().st_mode)
        except OSError:
            original_mode = None
    fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), suffix=".config-editor.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp() creates the tempfile 0600 regardless of the target's real
        # mode. Without re-applying the original mode, os.replace() silently
        # downgrades config.yaml to 0600 on every save — a behavior change
        # from whatever permissions (e.g. group-readable) the deployment had
        # intentionally set.
        if original_mode is not None:
            try:
                os.chmod(tmp, original_mode)
            except OSError:
                logger.warning("Failed to preserve config.yaml file mode on save", exc_info=True)
        os.replace(tmp, config_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Public API ────────────────────────────────────────────────────────────


def _read_config_bytes(config_path: Path) -> bytes:
    """Read config.yaml, failing CLOSED on an I/O error.

    Returning ``b""`` for a read failure made "could not read the file"
    indistinguishable from "the file is absent or empty". That weakened GET
    (an empty editor for a config that is really there) and, worse, the ETag
    and denylist on PUT: both would then compare the submitted document
    against an empty baseline, so every key looks newly added.
    """
    try:
        if not config_path.exists():
            return b""
    except OSError as exc:
        logger.warning("Failed to stat config.yaml", exc_info=True)
        raise ConfigEditorError(
            "Could not read config.yaml.", status=500
        ) from exc
    try:
        return config_path.read_bytes()
    except OSError as exc:
        logger.warning("Failed to read config.yaml", exc_info=True)
        raise ConfigEditorError(
            "Could not read config.yaml.", status=500
        ) from exc


def _etag_for(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def get_config_raw() -> dict:
    import yaml

    config_path = _active_profile_config_path()
    raw_bytes = _read_config_bytes(config_path)
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        redacted_text, redacted_paths = _redact_yaml_text(text)
    except yaml.YAMLError as exc:
        # Fail CLOSED. Redaction is decided on a complete parse, so a document
        # we cannot parse is a document we cannot prove is safe to show. The
        # previous line scanner would happily emit a malformed file verbatim.
        location = _yaml_error_location(exc)
        raise ConfigEditorError(
            "config.yaml could not be parsed, so it cannot be shown without "
            "risking credential disclosure. Fix the file on disk first.",
            status=409,
            extra={"line": location[0], "column": location[1]},
        ) from exc
    return {
        "yaml": redacted_text,
        "redacted": redacted_paths,
        "allowed": _write_enabled(),
        "write_gate_env": _WRITE_GATE_ENV,
        "etag": _etag_for(raw_bytes),
    }


def put_config_raw(yaml_text: Any, *, etag: str | None = None) -> dict:
    if not _write_enabled():
        raise ConfigEditorError(
            f"Raw config editing is disabled. Set {_WRITE_GATE_ENV}=1 to enable it.",
            status=403,
            extra={"write_gate_env": _WRITE_GATE_ENV},
        )
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise ConfigEditorError("yaml is required", status=400)
    if _REDACTED_PLACEHOLDER in yaml_text:
        raise ConfigEditorError(
            "Submitted YAML still contains redacted placeholder values. "
            "Re-fetch and edit without redacted values.",
            status=400,
        )

    try:
        import yaml as _yaml
    except ImportError as exc:
        raise ConfigEditorError("PyYAML is required to save config.yaml", status=500) from exc

    try:
        parsed = _yaml.safe_load(yaml_text)
    except _yaml.YAMLError as exc:
        line, column = _yaml_error_location(exc)
        raise ConfigEditorError(
            f"Invalid YAML: {exc}",
            status=400,
            extra={"line": line, "column": column},
        ) from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ConfigEditorError("Config must be a YAML mapping at the top level", status=400)

    config_path = _active_profile_config_path()

    from api.config import _cfg_lock, reload_config

    # The freshness check, the denylist comparison, and the write must all
    # observe the SAME on-disk snapshot, so they run together under
    # _cfg_lock rather than read-then-lock-then-write. Reading "current"
    # state outside the lock (the original implementation) left a window
    # between that read and the write where a concurrent save could land:
    # the denylist check would then be validating against an already-stale
    # snapshot, and the write would silently clobber the intervening change
    # without ever having checked it.
    with _cfg_lock:
        current_bytes = _read_config_bytes(config_path)

        if etag is not None:
            current_etag = _etag_for(current_bytes)
            if current_etag != etag:
                raise ConfigEditorError(
                    "config.yaml changed on disk since it was loaded. Re-fetch and retry.",
                    status=409,
                    extra={"etag": current_etag},
                )

        current_text = current_bytes.decode("utf-8", errors="replace")
        try:
            current_parsed = _yaml.safe_load(current_text) or {}
        except _yaml.YAMLError:
            current_parsed = {}
        if not isinstance(current_parsed, dict):
            current_parsed = {}

        blocked = _find_denylist_violations(current_parsed, parsed)
        if blocked:
            raise ConfigEditorError(
                "Refusing to change security-critical config keys via the raw editor: "
                + ", ".join(blocked),
                status=400,
                extra={"blocked_paths": blocked},
            )

        try:
            _write_config_atomic(config_path, yaml_text)
        except ConfigEditorError:
            raise
        except OSError as exc:
            # Disk full, EPERM, a read-only mount, an fsync/chmod/replace
            # failure: the route handler catches only ConfigEditorError, so a
            # raw OSError escaped to the HTTP framework and the client got an
            # unstructured connection/server error instead of the editor's
            # JSON error contract. The message deliberately carries no
            # filesystem path.
            logger.warning("config.yaml write failed", exc_info=True)
            raise ConfigEditorError(
                "Could not save config.yaml. The file was left unchanged.",
                status=500,
            ) from exc
        new_etag = _etag_for(yaml_text.encode("utf-8"))
    reload_config()
    return {"ok": True, "etag": new_etag}
