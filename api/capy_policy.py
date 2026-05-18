"""Metadata-only Capy autonomy/security/model-routing policy status.

This module intentionally returns fixed, bounded labels. Environment input selects the
mode only; it is never reflected as display text, because policy status is shown in
Spaces and can be adjacent to untrusted source/prompt context.
"""
from __future__ import annotations

import os
from typing import Any, Dict

_POLICY_BY_MODE: Dict[str, Dict[str, Any]] = {
    "supervised": {
        "label": "Supervised",
        "summary": "Approval required before writes, mutations, network side effects, creator commits, and sandboxed widget execution.",
    },
    "semi_autonomous": {
        "label": "Semi-autonomous",
        "summary": "Safe reads and tests can run; destructive writes still require approval.",
    },
    "autonomous": {
        "label": "Autonomous",
        "summary": "Scheduled safe workflows can run within configured caps; high-risk actions still require approval.",
    },
}

_ALLOWED_MODE_ALIASES = {
    "supervised": "supervised",
    "supervise": "supervised",
    "manual": "supervised",
    "semi": "semi_autonomous",
    "semi-autonomous": "semi_autonomous",
    "semi_autonomous": "semi_autonomous",
    "semiautonomous": "semi_autonomous",
    "autonomous": "autonomous",
    "auto": "autonomous",
}

_APPROVAL_GATES = [
    "creator_commit",
    "destructive_external_action",
    "generated_widget_execution",
    "credential_change",
]

_PROTECTED_BOUNDARIES = [
    "creator_preview",
    "creator_commit",
    "widget_runtime_prompt",
    "auto_fetched_source",
]


def _configured_mode() -> str:
    raw = os.environ.get("CAPY_AUTONOMY_MODE", "supervised")
    key = str(raw or "").strip().lower().replace(" ", "_")
    return _ALLOWED_MODE_ALIASES.get(key, "supervised")


def policy_status() -> Dict[str, Any]:
    """Return bounded policy metadata for product-visible trust controls."""

    mode = _configured_mode()
    policy = _POLICY_BY_MODE[mode]
    return {
        "available": True,
        "mode": mode,
        "label": policy["label"],
        "summary": policy["summary"],
        "approval_gates": list(_APPROVAL_GATES),
        "prompt_preflight": {
            "status": "required",
            "protected_boundaries": list(_PROTECTED_BOUNDARIES),
        },
        "model_routing": {
            "status": "configured_by_hermes",
            "default_hint": "hint:reasoning",
            "safe_fallback": "current Hermes provider",
        },
        "local_only": True,
    }
