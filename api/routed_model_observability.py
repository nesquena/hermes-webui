"""WebUI-only capture of routed models reported by Hermes API responses."""

from contextvars import ContextVar, Token
from dataclasses import dataclass
import logging
import threading
from typing import Any
import unicodedata

from api.config import _custom_provider_slug_from_name


logger = logging.getLogger(__name__)

_MAX_SCALAR_CHARS = 240


def _safe_scalar(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > _MAX_SCALAR_CHARS:
        return ""
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        return ""
    return value


@dataclass
class RoutedModelCapture:
    session_id: str
    stream_id: str
    task_id: str
    requested_model: str
    requested_provider: str
    response_model: str | None = None
    response_provider: str | None = None


_CAPTURE: ContextVar[RoutedModelCapture | None] = ContextVar(
    "routed_model_capture",
    default=None,
)
_INSTALL_LOCK = threading.RLock()


def provider_display_name(
    provider_context: Any,
    resolved_provider: Any,
    config: Any,
) -> str:
    context = _safe_scalar(provider_context)
    resolved = _safe_scalar(resolved_provider)

    if context.lower().startswith("custom:"):
        entries = getattr(config, "custom_providers", None)
        if isinstance(config, dict):
            entries = config.get("custom_providers")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = _safe_scalar(entry.get("name"))
                if not name:
                    continue
                try:
                    slug = _custom_provider_slug_from_name(name)
                except Exception:
                    continue
                if slug.lower() == context.lower():
                    return name

    return context or resolved or ""


def _observe_post_api_request(
    *,
    platform: Any = None,
    session_id: Any = None,
    task_id: Any = None,
    response_model: Any = None,
    provider: Any = None,
    **_kwargs: Any,
) -> None:
    capture = _CAPTURE.get()
    if capture is None or _safe_scalar(platform).lower() != "webui":
        return
    if session_id != capture.session_id or task_id != capture.task_id:
        return

    safe_model = _safe_scalar(response_model)
    if not safe_model:
        return

    capture.response_model = safe_model
    safe_provider = _safe_scalar(provider)
    if safe_provider:
        capture.response_provider = safe_provider


def _install_post_api_request_observer() -> None:
    try:
        from hermes_cli import plugins

        plugins.discover_plugins()
        manager = plugins.get_plugin_manager()
        with _INSTALL_LOCK:
            hooks = getattr(manager, "_hooks", None)
            if not isinstance(hooks, dict):
                return
            callbacks = hooks.setdefault("post_api_request", [])
            if isinstance(callbacks, list) and _observe_post_api_request not in callbacks:
                callbacks.append(_observe_post_api_request)
    except Exception:
        logger.debug("Unable to install routed-model lifecycle observer", exc_info=True)


def begin_routed_model_capture(
    *,
    session_id: Any,
    stream_id: Any,
    task_id: Any,
    requested_model: Any,
    requested_provider: Any,
) -> Token:
    _install_post_api_request_observer()
    capture = RoutedModelCapture(
        session_id=str(session_id),
        stream_id=str(stream_id),
        task_id=str(task_id),
        requested_model=_safe_scalar(requested_model),
        requested_provider=_safe_scalar(requested_provider),
    )
    return _CAPTURE.set(capture)


def snapshot_routed_model_capture() -> dict[str, str] | None:
    capture = _CAPTURE.get()
    if capture is None or not capture.response_model:
        return None

    payload = {
        "requested_model": capture.requested_model,
        "requested_provider": capture.requested_provider,
        "used_model": capture.response_model,
        "used_provider": capture.requested_provider or capture.response_provider or "",
        "source": "openai-compatible-sse",
    }
    return {key: value for key, value in payload.items() if value}


def reset_routed_model_capture(token: Token) -> None:
    _CAPTURE.reset(token)
