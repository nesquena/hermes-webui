"""Metadata-only Capy autonomy/security/model-routing policy status.

This module intentionally returns fixed, bounded labels. Environment input selects the
mode only; it is never reflected as display text, because policy status is shown in
Spaces and can be adjacent to untrusted source/prompt context.
"""
from __future__ import annotations

import hashlib
import os
import re
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

_PREFLIGHT_RULES = [
    (
        "role_override",
        re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions|disregard\s+(?:all\s+)?instructions|override\s+(?:system|developer)", re.I),
    ),
    (
        "system_prompt_exfiltration",
        re.compile(r"(?:system|developer)\s+prompt|hidden\s+instructions|reveal\s+(?:your\s+)?instructions", re.I),
    ),
    (
        "credential_request",
        re.compile(r"api[_\s-]?key|api[_\s-]?auth|bearer\b|access\s+token|password\b|credential", re.I),
    ),
    (
        "tool_coercion",
        re.compile(r"bypass\s+approval|disable\s+approval|without\s+asking|exfiltrat|delete\s+all|sudo\b", re.I),
    ),
    (
        "executable_content_marker",
        re.compile(r"<\s*script\b|renderer\b|render\s*code|generated\s+(?:widget\s+)?body|raw\s+prompt", re.I),
    ),
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


def _normalized_boundary(boundary: Any) -> str:
    text = str(boundary or "").strip().lower().replace("-", "_")
    return text if text in _PROTECTED_BOUNDARIES else "unknown_boundary"


def prompt_preflight(prompt: Any, *, boundary: str = "creator_preview") -> Dict[str, Any]:
    """Classify a high-risk prompt/source boundary without echoing raw text.

    The receipt is intentionally metadata-only: it returns fixed category labels,
    a SHA-256 prompt hash for audit correlation, and no raw prompt/source fields.
    """

    text = "" if prompt is None else str(prompt)
    if not text.strip():
        raise ValueError("prompt is required")
    categories: list[str] = []
    for category, pattern in _PREFLIGHT_RULES:
        if pattern.search(text) and category not in categories:
            categories.append(category)
    status = "block" if categories else "pass"
    severity = "high" if categories else "none"
    return {
        "available": True,
        "action": "capy.prompt_preflight",
        "boundary": _normalized_boundary(boundary),
        "status": status,
        "severity": severity,
        "categories": categories,
        "prompt_hash": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        "metadata_only": True,
        "raw_prompt_stored": False,
        "local_only": True,
    }
