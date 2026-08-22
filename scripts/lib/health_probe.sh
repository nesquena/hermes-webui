#!/usr/bin/env bash
# Shared, TLS-aware /health probe used by every shell launcher (start.sh,
# ctl.sh, the WSL autostart helper) and the Docker HEALTHCHECK.
#
# The WebUI serves HTTPS when both HERMES_WEBUI_TLS_CERT and
# HERMES_WEBUI_TLS_KEY are set (see api/config.py:TLS_ENABLED). The probe must
# mirror that scheme, otherwise an http:// probe against an https listener (or
# vice-versa) reports a healthy server as down.
#
# Probe order when TLS is configured:
#   1. Verified HTTPS.
#   2. Self-signed fallback: if verification fails, retry without verification
#      and print a one-line "self-signed certificate" warning (once).
#   3. Plain HTTP: server.py intentionally falls back to serving HTTP when the
#      cert/key are present but unloadable (tests/test_tls_support.py::
#      test_tls_startup_failure_fallback_to_http). Probe HTTP last so that
#      contract is honored instead of polling HTTPS forever.
#
# HERMES_WEBUI_TLS_INSECURE_PROBE=1 is an explicit opt-in that skips verified
# HTTPS and goes straight to the unverified attempt. By contract this is
# silent (the user already accepted the risk), so no warning is printed.
#
# This file is safe to `source` (defines functions only) and is also runnable
# directly as a standalone probe:
#   bash scripts/lib/health_probe.sh <host> <port> [path] [max_time]
# On success it prints the response body to stdout and exits 0.
#
# Kept bash 3.2 compatible under `set -u` (ctl.sh sources this).

# Guard so the self-signed warning is printed at most once per process even
# when the probe is retried in a wait loop.
_HERMES_WEBUI_SELF_SIGNED_WARNED="${_HERMES_WEBUI_SELF_SIGNED_WARNED:-0}"

# Records the scheme that actually answered the most recent successful probe
# ("https" or "http"). Defaults empty; set by hermes_webui_probe_health.
_HERMES_WEBUI_PROBE_SCHEME="${_HERMES_WEBUI_PROBE_SCHEME:-}"

_hermes_webui_truthy() {
  case "${1:-}" in
    1 | true | TRUE | True | yes | YES | on | ON) return 0 ;;
    *) return 1 ;;
  esac
}

# Echo "https" when TLS is configured (both cert and key present), else "http".
hermes_webui_probe_scheme() {
  if [[ -n "${HERMES_WEBUI_TLS_CERT:-}" && -n "${HERMES_WEBUI_TLS_KEY:-}" ]]; then
    printf 'https'
  else
    printf 'http'
  fi
}

_hermes_webui_warn_self_signed() {
  [[ "${_HERMES_WEBUI_SELF_SIGNED_WARNED}" == "1" ]] && return 0
  _HERMES_WEBUI_SELF_SIGNED_WARNED=1
  printf '[warn] Health probe: TLS certificate at %s is self-signed or not trusted; proceeding without verification.\n' \
    "$1" >&2
}

# _hermes_webui_http_get <url> <max_time> <mode> [direct]
# mode: "insecure" disables certificate verification, anything else verifies.
# direct: "direct" bypasses every configured proxy.
# Prints the response body to stdout; returns the underlying client exit code.
_hermes_webui_http_get() {
  local url="$1" max_time="$2" mode="$3" direct="${4:-}"
  if command -v curl >/dev/null 2>&1; then
    if [[ "${mode}" == "insecure" ]]; then
      if [[ "${direct}" == "direct" ]]; then
        curl -fsS -k --noproxy '*' --max-time "${max_time}" "${url}" 2>/dev/null
      else
        curl -fsS -k --max-time "${max_time}" "${url}" 2>/dev/null
      fi
    else
      if [[ "${direct}" == "direct" ]]; then
        curl -fsS --noproxy '*' --max-time "${max_time}" "${url}" 2>/dev/null
      else
        curl -fsS --max-time "${max_time}" "${url}" 2>/dev/null
      fi
    fi
    return $?
  elif command -v wget >/dev/null 2>&1; then
    if [[ "${mode}" == "insecure" ]]; then
      if [[ "${direct}" == "direct" ]]; then
        wget -qO- --no-proxy --no-check-certificate "--timeout=${max_time}" --tries=1 "${url}" 2>/dev/null
      else
        wget -qO- --no-check-certificate "--timeout=${max_time}" --tries=1 "${url}" 2>/dev/null
      fi
    else
      if [[ "${direct}" == "direct" ]]; then
        wget -qO- --no-proxy "--timeout=${max_time}" --tries=1 "${url}" 2>/dev/null
      else
        wget -qO- "--timeout=${max_time}" --tries=1 "${url}" 2>/dev/null
      fi
    fi
    return $?
  fi
  return 127
}

# hermes_webui_probe_health <host> <port> [path] [max_time] [direct]
# Pass "direct" to bypass every configured proxy for all probe attempts.
# Prints the response body to stdout; warnings go to stderr.
# Returns 0 if the server answered, 1 otherwise.
#
# Side effect: sets the global _HERMES_WEBUI_PROBE_SCHEME to the scheme that
# actually answered ("https" or "http"). Callers that print a ready/already-up
# URL should prefer this over the configured scheme, because server.py falls
# back to plain HTTP when the cert/key are unloadable — so the configured
# scheme can be https:// while the live server speaks http://.
hermes_webui_probe_health() {
  local host="$1" port="$2" path="${3:-/health}" max_time="${4:-2}" direct="${5:-}"
  local scheme body
  scheme="$(hermes_webui_probe_scheme)"

  local http_url="http://${host}:${port}${path}"

  if [[ "${scheme}" == "http" ]]; then
    if body="$(_hermes_webui_http_get "${http_url}" "${max_time}" "" "${direct}")"; then
      _HERMES_WEBUI_PROBE_SCHEME="http"
      printf '%s' "${body}"
      return 0
    fi
    return 1
  fi

  # TLS configured: prefer HTTPS, then fall back to HTTP.
  local https_url="https://${host}:${port}${path}"

  if _hermes_webui_truthy "${HERMES_WEBUI_TLS_INSECURE_PROBE:-}"; then
    # Explicit opt-in: skip verification, stay silent by contract.
    if body="$(_hermes_webui_http_get "${https_url}" "${max_time}" "insecure" "${direct}")"; then
      _HERMES_WEBUI_PROBE_SCHEME="https"
      printf '%s' "${body}"
      return 0
    fi
  else
    # 1) Verified HTTPS.
    if body="$(_hermes_webui_http_get "${https_url}" "${max_time}" "" "${direct}")"; then
      _HERMES_WEBUI_PROBE_SCHEME="https"
      printf '%s' "${body}"
      return 0
    fi
    # 2) Self-signed fallback: verification failed, retry unverified + warn.
    if body="$(_hermes_webui_http_get "${https_url}" "${max_time}" "insecure" "${direct}")"; then
      _hermes_webui_warn_self_signed "${https_url}"
      _HERMES_WEBUI_PROBE_SCHEME="https"
      printf '%s' "${body}"
      return 0
    fi
  fi

  # 3) server.py may have fallen back to plain HTTP (cert/key unloadable).
  if body="$(_hermes_webui_http_get "${http_url}" "${max_time}" "" "${direct}")"; then
    _HERMES_WEBUI_PROBE_SCHEME="http"
    printf '%s' "${body}"
    return 0
  fi

  return 1
}

# When executed directly (not sourced), run a single probe.
if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
  if [[ $# -lt 2 ]]; then
    echo "usage: health_probe.sh <host> <port> [path] [max_time]" >&2
    exit 2
  fi
  hermes_webui_probe_health "$@"
  exit $?
fi
