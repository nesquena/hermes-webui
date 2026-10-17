"""Bounded suppression of botocore's EC2 instance-metadata (IMDS) probe.

Why this exists
---------------
Several WebUI paths enumerate provider auth state (`hermes_cli.models.
list_available_providers()`, `hermes_cli.auth.get_auth_status()`). For the
``bedrock`` provider those land in ``agent.bedrock_adapter.has_aws_credentials()``,
which — after finding no AWS env vars — falls back to botocore's full credential
chain. On a host that is not an EC2 instance, botocore still tries to reach the
Instance Metadata Service at ``169.254.169.254``. That address is a link-local
black hole off-EC2, so each attempt burns botocore's full connect timeout.

Measured on a non-EC2 macOS host, one cold
``/api/models?freshness=session_visit`` catalog rebuild:

    retrieve_iam_role_credentials calls: 2
      call[0] = 1.007s          <- first PUT eats the full connect timeout
      call[1] = 0.003s          <- botocore's own negative cache
    raw IMDS HTTP attempts: 4 (2x PUT token, 2x GET security-credentials)

With the probe suppressed the same credential resolution returns ``None`` in
~31ms and issues **zero** IMDS HTTP requests.

What this does *not* break
--------------------------
Suppression only disables the EC2 *instance metadata* leg of the chain. Verified
against botocore 1.42.97 with ``AWS_EC2_METADATA_DISABLED=true`` set:

* ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` still resolve (method ``env``)
* ``~/.aws/credentials`` still resolves (method ``shared-credentials-file``)
* ECS/container credentials are unaffected — that provider uses a *different*
  host (``ContainerMetadataFetcher.IP_ADDRESS == '169.254.170.2'``)

Genuine instance-role discovery is preserved because suppression is applied
**only** after an affirmative, bounded reachability check proves the IMDS
endpoint is unreachable from this host. If the endpoint answers — or if we
cannot tell — we leave botocore completely alone and pay the cost. Unknown is
not treated as "not on EC2".

An operator who has set ``AWS_EC2_METADATA_DISABLED`` themselves (to either
value) is always authoritative; this module never overwrites their choice.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

# The IMDS endpoint. Deliberately not the ECS container-credentials endpoint
# (169.254.170.2) — that provider is a different credential source and stays
# enabled.
_IMDS_HOST = "169.254.169.254"
_IMDS_PORT = 80

_ENV_VAR = "AWS_EC2_METADATA_DISABLED"

# How long a TCP connect to the IMDS endpoint may take before we call the host
# "not on EC2". A real instance answers on the link-local address in ~1ms, so
# this is a very wide margin; off-EC2 it is the entire cost we pay instead of
# botocore's multi-second timeout.
try:
    _REACHABILITY_TIMEOUT_SECONDS: float = float(
        os.getenv("HERMES_WEBUI_IMDS_PROBE_TIMEOUT", "0.15") or "0.15"
    )
except (TypeError, ValueError):
    _REACHABILITY_TIMEOUT_SECONDS = 0.15

# Re-probe interval for a negative ("unreachable") result. Caching the negative
# forever would mean a transient network fault permanently disables genuine
# instance-role discovery — the dangerous direction — so it expires. A positive
# result is not cached at all: if IMDS answers we never suppress anyway.
try:
    _REACHABILITY_TTL_SECONDS: float = float(
        os.getenv("HERMES_WEBUI_IMDS_PROBE_TTL", "300") or "300"
    )
except (TypeError, ValueError):
    _REACHABILITY_TTL_SECONDS = 300.0

_state_lock = threading.Lock()
# (checked_at_monotonic, reachable) for the last completed probe, or None.
_reachability_cache: tuple[float, bool] | None = None

# os.environ is process-global, so concurrent scopes (the models rebuild runs on
# a worker thread while a Settings request may be in flight) must not restore
# the variable out from under each other. Refcount the active scopes and only
# restore when the last one exits.
_scope_depth = 0
_saved_env_value: str | None = None


def reset_reachability_cache() -> None:
    """Forget the cached probe result. Test seam; not used in production."""
    global _reachability_cache
    with _state_lock:
        _reachability_cache = None


def _probe_imds_reachable() -> bool:
    """Return True iff a TCP connect to the IMDS endpoint succeeds quickly.

    Any failure (timeout, no route, refused) means the endpoint is not usable
    from this host, which is exactly the condition under which suppressing the
    probe changes nothing except latency.
    """
    try:
        with socket.create_connection(
            (_IMDS_HOST, _IMDS_PORT), timeout=_REACHABILITY_TIMEOUT_SECONDS
        ):
            return True
    except OSError:
        return False
    except Exception:  # pragma: no cover — defensive; never fail the caller
        logger.debug("IMDS reachability probe raised unexpectedly", exc_info=True)
        return True  # fail closed: unknown is not "not on EC2"


def imds_is_reachable() -> bool:
    """Cached reachability answer for the IMDS endpoint."""
    global _reachability_cache
    now = time.monotonic()
    with _state_lock:
        cached = _reachability_cache
        if cached is not None and (now - cached[0]) < _REACHABILITY_TTL_SECONDS:
            return cached[1]

    # Probe outside the lock: a concurrent caller doing the same bounded probe
    # is cheaper than serializing every request behind it.
    reachable = _probe_imds_reachable()

    with _state_lock:
        _reachability_cache = (time.monotonic(), reachable)
    return reachable


def should_suppress_imds() -> bool:
    """Return True iff we may disable the EC2 metadata probe for this host.

    False when the operator has already expressed an explicit preference, or
    when the IMDS endpoint is reachable (or its state is unknown).

    Only meaningful when no module-owned scope is active: while one is, the
    value in the environment is *ours*, not the operator's, so reading it here
    cannot distinguish the two. :func:`suppress_ec2_imds_probe` establishes
    that precondition by checking ``_scope_depth`` first.
    """
    if os.environ.get(_ENV_VAR) is not None:
        # Operator/deployment already decided — honour it either way.
        return False
    return not imds_is_reachable()


@contextlib.contextmanager
def suppress_ec2_imds_probe(reason: str = ""):
    """Disable botocore's EC2 IMDS probe for the duration of the block.

    A no-op unless :func:`should_suppress_imds` confirms this host cannot reach
    the metadata endpoint. Safe to nest and to enter concurrently from several
    threads: scopes are refcounted and the previous environment value is
    restored exactly once, on exit of the outermost scope, on every exit path
    (success, exception, cancellation).

    Ownership matters for the concurrent case. ``os.environ`` is process-global,
    so a scope that begins while another is already active would otherwise read
    the suppression value this module installed and mistake it for an operator
    decision — declining to join the refcount, and then losing suppression the
    moment the first scope exits. A late entrant therefore joins the existing
    refcount under ``_state_lock`` without consulting the environment; only a
    true outermost entrant reads and preserves the operator's value.
    """
    global _scope_depth, _saved_env_value

    with _state_lock:
        joined_active_scope = _scope_depth > 0
        if joined_active_scope:
            _scope_depth += 1

    if not joined_active_scope:
        if not should_suppress_imds():
            yield False
            return

        with _state_lock:
            # Re-check under the lock: reachability probing happens outside it,
            # so another thread may have installed an outer scope meanwhile. If
            # it did, join it rather than overwriting its saved value.
            if _scope_depth == 0:
                _saved_env_value = os.environ.get(_ENV_VAR)
                os.environ[_ENV_VAR] = "true"
                if reason:
                    logger.debug(
                        "EC2 IMDS probe suppressed (%s): %s is unreachable from this host",
                        reason,
                        _IMDS_HOST,
                    )
            _scope_depth += 1

    try:
        yield True
    finally:
        with _state_lock:
            _scope_depth -= 1
            if _scope_depth <= 0:
                _scope_depth = 0
                if _saved_env_value is None:
                    os.environ.pop(_ENV_VAR, None)
                else:
                    os.environ[_ENV_VAR] = _saved_env_value
                _saved_env_value = None
