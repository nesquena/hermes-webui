"""Resolve whether gateway lifecycle control is local or remote."""

from __future__ import annotations

import os

REMOTE_GATEWAY_CONTROL_ERROR_CODE = "remote_gateway_control_unsupported"
REMOTE_GATEWAY_CONTROL_MESSAGE = (
    "Gateway lifecycle control is unavailable for remote gateway deployments. "
    "Restart the hermes-agent service through its container supervisor."
)

# These variables select the gateway used by WebUI chat/runtime operations.
# Health-only URLs are deliberately excluded: a probe target does not establish
# lifecycle authority over that process.
_REMOTE_GATEWAY_CONTROL_ENV_VARS = (
    "HERMES_API_URL",
    "HERMES_WEBUI_GATEWAY_BASE_URL",
)


class RemoteGatewayControlUnsupported(RuntimeError):
    """Raised when lifecycle control targets a separately owned gateway."""

    error_code = REMOTE_GATEWAY_CONTROL_ERROR_CODE

    def __init__(self) -> None:
        super().__init__(REMOTE_GATEWAY_CONTROL_MESSAGE)


def remote_gateway_configured() -> bool:
    """Return whether WebUI is configured to use a separately owned gateway."""
    return any(os.environ.get(name, "").strip() for name in _REMOTE_GATEWAY_CONTROL_ENV_VARS)


def require_local_gateway_control() -> None:
    """Fail closed unless this WebUI process owns the gateway lifecycle."""
    if remote_gateway_configured():
        raise RemoteGatewayControlUnsupported()
