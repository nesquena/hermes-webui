"""
ServeAI API Gateway client for hermes-webui.

Validates a Hermes instance ID + Bearer token against the ServeAI API gateway.
Used during session creation and listing to scope sessions per instance.
"""
import json
import logging
import urllib.error
import urllib.request

from api.config import SERVEAI_API_URL

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5


def get_hermes_instance(instance_id: str, bearer_token: str) -> dict | None | bool:
    """Fetch a Hermes instance from the ServeAI API gateway.

    Args:
        instance_id: The Hermes instance MongoDB ObjectId.
        bearer_token: The caller's JWT Bearer token (from the ServeAI cookie).

    Returns:
        - dict  → success, contains instance data
        - False → explicit 4xx from gateway (unauthorized or not found) — fail closed
        - None  → network/timeout error — fail open (don't lock users out when gateway is down)
    """
    if not instance_id or not bearer_token:
        return False

    url = f"{SERVEAI_API_URL}/hermes-instances/{instance_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            body = resp.read()
            data = json.loads(body)
            return data
    except urllib.error.HTTPError as e:
        logger.debug("ServeAI gateway returned %s for instance %s", e.code, instance_id)
        return False  # explicit 4xx → unauthorized or not found
    except Exception as e:
        logger.warning("ServeAI gateway call failed: %s", e)
        return None  # network error → fail open
