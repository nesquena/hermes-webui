"""Helpers for persisted session runtime contracts.

Project OS dedicated sessions can opt into ``session_mode='project_narrow'`` to
pin a session to a single profile and workspace subtree. The contract is stored
on the Session itself so every runtime surface can re-validate the same facts.
"""

from __future__ import annotations

from pathlib import Path

from api.workspace import resolve_trusted_workspace

PROJECT_NARROW_SESSION_MODE = "project_narrow"
_SUPPORTED_PROFILE_POLICIES = {"pinned"}
_SUPPORTED_PREFILL_POLICIES = {"disabled", "inherit_global", "project_only"}


def normalize_runtime_contract(raw_contract) -> dict:
    return dict(raw_contract) if isinstance(raw_contract, dict) else {}


def _normalize_string_list(raw_value) -> list[str]:
    if not isinstance(raw_value, (list, tuple, set)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def normalize_session_mode(raw_mode) -> str | None:
    mode = str(raw_mode or "").strip().lower()
    return mode or None


def is_project_narrow_mode(session_or_mode) -> bool:
    if isinstance(session_or_mode, str):
        mode = normalize_session_mode(session_or_mode)
    else:
        mode = normalize_session_mode(getattr(session_or_mode, "session_mode", None))
    return mode == PROJECT_NARROW_SESSION_MODE


def build_project_narrow_runtime_contract(raw_contract, *, workspace_root, profile) -> dict:
    contract = normalize_runtime_contract(raw_contract)
    root_candidate = contract.get("workspace_root") or workspace_root
    if not root_candidate:
        raise ValueError("project-narrow sessions require a workspace_root")
    profile_name = str(profile or "").strip()
    if not profile_name:
        raise ValueError("project-narrow sessions require a pinned profile")
    profile_policy = str(contract.get("profile_policy") or "pinned").strip().lower()
    if profile_policy not in _SUPPORTED_PROFILE_POLICIES:
        raise ValueError(f"unsupported profile_policy: {profile_policy}")
    prefill_policy = str(contract.get("prefill_policy") or "disabled").strip().lower()
    if prefill_policy not in _SUPPORTED_PREFILL_POLICIES:
        raise ValueError(f"unsupported prefill_policy: {prefill_policy}")
    contract["workspace_root"] = str(resolve_trusted_workspace(root_candidate))
    contract["profile_policy"] = profile_policy
    contract["prefill_policy"] = prefill_policy
    contract["allowed_note_sources"] = _normalize_string_list(contract.get("allowed_note_sources"))
    return contract


def enforce_pinned_profile(session, requested_profile=None) -> str:
    session_profile = str(getattr(session, "profile", None) or "").strip()
    requested = str(requested_profile or "").strip()
    if not is_project_narrow_mode(session):
        return requested or session_profile
    contract = normalize_runtime_contract(getattr(session, "runtime_contract", None))
    profile_policy = str(contract.get("profile_policy") or "pinned").strip().lower()
    if profile_policy not in _SUPPORTED_PROFILE_POLICIES:
        raise ValueError(f"unsupported profile_policy: {profile_policy}")
    if not session_profile:
        raise ValueError("project-narrow session is missing its persisted profile")
    if requested and requested != session_profile:
        raise ValueError(f"project-narrow session profile is pinned to '{session_profile}'")
    return session_profile


def clamp_workspace_to_contract(session, requested_workspace=None, *, fallback_to_root=False) -> str:
    candidate = requested_workspace
    if candidate in (None, ""):
        candidate = getattr(session, "workspace", None)
    if not is_project_narrow_mode(session):
        return str(resolve_trusted_workspace(candidate))
    contract = normalize_runtime_contract(getattr(session, "runtime_contract", None))
    root = str(resolve_trusted_workspace(contract.get("workspace_root") or candidate))
    try:
        resolved = str(resolve_trusted_workspace(candidate or root))
    except ValueError:
        if fallback_to_root:
            return root
        raise
    try:
        relative = Path(resolved).relative_to(Path(root))
        _ = relative
    except ValueError:
        if fallback_to_root:
            return root
        raise ValueError(f"project-narrow session workspace must stay under {root}")
    return resolved


def get_project_narrow_toolsets(contract_or_session) -> list[str]:
    if hasattr(contract_or_session, "runtime_contract"):
        contract = normalize_runtime_contract(getattr(contract_or_session, "runtime_contract", None))
    else:
        contract = normalize_runtime_contract(contract_or_session)
    return _normalize_string_list(contract.get("toolsets"))


def get_project_narrow_allowed_skills(contract_or_session) -> list[str]:
    if hasattr(contract_or_session, "runtime_contract"):
        contract = normalize_runtime_contract(getattr(contract_or_session, "runtime_contract", None))
    else:
        contract = normalize_runtime_contract(contract_or_session)
    return _normalize_string_list(contract.get("allowed_skills"))


def get_project_narrow_prefill_policy(contract_or_session) -> str:
    if hasattr(contract_or_session, "runtime_contract"):
        contract = normalize_runtime_contract(getattr(contract_or_session, "runtime_contract", None))
    else:
        contract = normalize_runtime_contract(contract_or_session)
    policy = str(contract.get("prefill_policy") or "disabled").strip().lower()
    return policy if policy in _SUPPORTED_PREFILL_POLICIES else "disabled"


def get_project_narrow_allowed_note_sources(contract_or_session) -> list[str]:
    if hasattr(contract_or_session, "runtime_contract"):
        contract = normalize_runtime_contract(getattr(contract_or_session, "runtime_contract", None))
    else:
        contract = normalize_runtime_contract(contract_or_session)
    return _normalize_string_list(contract.get("allowed_note_sources"))


def enforce_toolset_contract(session, requested_toolsets):
    if not is_project_narrow_mode(session):
        return requested_toolsets
    allowed = get_project_narrow_toolsets(session)
    if not allowed:
        return requested_toolsets
    if requested_toolsets is None:
        raise ValueError("project-narrow session toolsets are pinned and cannot be cleared")
    requested = _normalize_string_list(requested_toolsets)
    if not requested:
        raise ValueError("project-narrow session toolsets must stay within the persisted allowlist")
    extras = [name for name in requested if name not in allowed]
    if extras:
        raise ValueError(
            "project-narrow session toolsets must stay within the persisted allowlist: "
            + ", ".join(extras)
        )
    return requested


def is_allowed_skill_name(allowed_skills, skill_name: str) -> bool:
    allowed = _normalize_string_list(allowed_skills)
    if not allowed:
        return True
    candidate = str(skill_name or "").strip()
    if not candidate:
        return False
    bare_candidate = candidate.split(":", 1)[-1]
    for allowed_name in allowed:
        if candidate == allowed_name:
            return True
        if bare_candidate == allowed_name.split(":", 1)[-1]:
            return True
    return False


def is_allowed_note_source(allowed_sources, source_name: str) -> bool:
    allowed = {item.lower() for item in _normalize_string_list(allowed_sources)}
    if not allowed:
        return True
    candidate = str(source_name or "").strip().lower()
    return bool(candidate) and candidate in allowed


def filter_skill_summaries_for_contract(skills, allowed_skills):
    allowed = _normalize_string_list(allowed_skills)
    if not allowed:
        return list(skills or [])
    return [
        skill
        for skill in (skills or [])
        if isinstance(skill, dict) and is_allowed_skill_name(allowed, skill.get("name", ""))
    ]


def filter_note_sources_for_contract(sources, allowed_sources):
    allowed = _normalize_string_list(allowed_sources)
    if not allowed:
        return list(sources or [])
    return [
        source
        for source in (sources or [])
        if isinstance(source, dict) and is_allowed_note_source(allowed, source.get("name", ""))
    ]
