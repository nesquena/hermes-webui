"""Safe agent tool availability diagnostics for WebUI runtime prompts.

The Hermes Agent tool list is the result of two separate filters:

1. platform/session toolsets (for example, whether ``browser`` is enabled), and
2. per-tool requirement checks (for example, whether the selected browser
   backend has a CLI, browser binary, or cloud credentials).

WebUI should not let the model infer permanent platform limitations from a tool
that is enabled but currently filtered out.  This module builds a tiny,
non-secret diagnostic block that can be included in ephemeral session context.
"""
from __future__ import annotations

from typing import Any, Iterable

_BROWSER_TOOL_PREFIX = "browser_"
_BROWSER_CLOUD_ENV_HINTS = {
    "browser-use": ("BROWSER_USE_API_KEY",),
    "browser_use": ("BROWSER_USE_API_KEY",),
    "browserbase": ("BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"),
    "firecrawl": ("FIRECRAWL_API_KEY",),
}


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _tool_names(tools: Iterable[Any] | None) -> set[str]:
    return {name for tool in (tools or []) if (name := _tool_name(tool))}


def _browser_provider_from_config(cfg: dict[str, Any] | None) -> str:
    browser_cfg = (cfg or {}).get("browser") or {}
    provider = browser_cfg.get("cloud_provider")
    if provider is None or str(provider).strip() == "":
        return "local"
    return str(provider).strip()


def _browser_hint(provider: str, available: bool) -> str:
    if available:
        return "Browser tools are available in this WebUI session."
    normalized = provider.lower()
    env_names = _BROWSER_CLOUD_ENV_HINTS.get(normalized)
    if env_names:
        return (
            "Browser toolset is enabled, but the selected browser backend is not ready. "
            f"Configure {', '.join(env_names)} or switch browser.cloud_provider to local."
        )
    if normalized == "local":
        return (
            "Browser toolset is enabled, but local browser requirements are not ready. "
            "Install/configure agent-browser and a supported browser engine."
        )
    return (
        "Browser toolset is enabled, but browser tools were filtered out by Hermes Agent "
        "requirement checks for the selected backend."
    )


def build_tool_availability_context(
    *,
    enabled_toolsets: Iterable[str] | None,
    agent_tools: Iterable[Any] | None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a small safe summary of tool availability for WebUI context.

    The payload intentionally contains no paths, command lines, tokens, or raw
    config.  It reports only the distinction a user-facing answer needs: whether
    a capability's toolset was requested and whether actual schemas survived
    Hermes Agent's requirement checks.
    """
    toolsets = {str(name).strip() for name in (enabled_toolsets or []) if str(name).strip()}
    names = _tool_names(agent_tools)

    browser_toolset_enabled = "browser" in toolsets
    browser_tools = sorted(name for name in names if name.startswith(_BROWSER_TOOL_PREFIX))
    browser_available = bool(browser_tools)
    provider = _browser_provider_from_config(cfg)

    if not browser_toolset_enabled and not browser_available:
        return {
            "browser": {
                "toolset_enabled": False,
                "available": False,
                "reason": "browser_toolset_not_enabled",
                "hint": "Browser tools are not enabled for this WebUI session.",
            }
        }

    reason = "available" if browser_available else "browser_requirements_unmet"
    return {
        "browser": {
            "toolset_enabled": browser_toolset_enabled,
            "available": browser_available,
            "reason": reason,
            "provider": provider,
            "tools": browser_tools[:12],
            "hint": _browser_hint(provider, browser_available),
        }
    }


def tool_availability_prompt_lines(tool_availability: dict[str, Any] | None) -> list[str]:
    """Render a compact prompt-safe diagnostic list for the agent."""
    if not isinstance(tool_availability, dict):
        return []
    browser = tool_availability.get("browser")
    if not isinstance(browser, dict):
        return []

    enabled = bool(browser.get("toolset_enabled"))
    available = bool(browser.get("available"))
    lines = [
        "- Browser toolset enabled: " + ("yes" if enabled else "no"),
        "- Browser tools available: " + ("yes" if available else "no"),
    ]
    provider = str(browser.get("provider") or "").strip()
    if provider:
        lines.append(f"- Browser backend: {provider}")
    hint = str(browser.get("hint") or "").strip()
    if hint:
        lines.append(f"- Browser availability note: {hint}")
    if enabled and not available:
        lines.append(
            "- If asked about browser access, explain this as enabled-but-unavailable "
            "runtime configuration, not as a permanent WebUI/API limitation."
        )
    return lines
