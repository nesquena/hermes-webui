"""Fail-closed, manifest-derived verifier for the turn-admission caller surface.

The verifier performs one strict tracked-Python scan and compares complete
five-part identities in both phases.  Product modules are never imported.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import cast

MANIFEST_PATH = Path(__file__).parent / "turn_admission_signature_manifest.json"

Identity = tuple[str, str, str, int, str]

DIAGNOSTIC_CODES = frozenset(
    {
        "E_ALIAS_AMBIGUOUS",
        "E_ALIAS_EXTRA",
        "E_ALIAS_MISSING",
        "E_BEHAVIOR_ONLY_EXTRA",
        "E_BEHAVIOR_ONLY_MISSING",
        "E_C50_MISSING",
        "E_CALLBACK_EXTRA",
        "E_CALLBACK_MISSING",
        "E_CALL_EXTRA",
        "E_CALL_MISSING",
        "E_CORE_BYPASS_FORBIDDEN",
        "E_CORE_DUPLICATE_PRODUCTION_CALL",
        "E_CORE_TEST_IDENTITY_MISSING",
        "E_CORE_TEST_IDENTITY_UNKNOWN",
        "E_DISPOSITION_UNMAPPED",
        "E_DYNAMIC_EXTRA",
        "E_DYNAMIC_INDIRECTION_FORBIDDEN",
        "E_DYNAMIC_MISSING",
        "E_DYNAMIC_NAME_UNRESOLVED",
        "E_FILE_READ_ERROR",
        "E_GIT_ENUMERATION_FAILED",
        "E_IMPORT_ALIAS_AMBIGUOUS",
        "E_KWARGS_MISSING_REQUIRED",
        "E_KWARGS_UNRESOLVED",
        "E_LEXICAL_SHADOW_AMBIGUOUS",
        "E_MANIFEST_SELF_INCONSISTENT",
        "E_N03_EXTRA",
        "E_N03_MISSING",
        "E_PARSE_ERROR",
        "E_PARTIAL_EXTRA",
        "E_PARTIAL_MISSING",
        "E_RETIRED_SYMBOL_REFERENCE",
        "E_SIGNATURE_DEFINITION_DUPLICATE",
        "E_SIGNATURE_DEFINITION_MISSING",
        "E_SIGNATURE_FIELD_HAS_DEFAULT",
        "E_SIGNATURE_FIELD_MISSING",
        "E_SIGNATURE_FIELD_NOT_KWONLY",
        "E_SIGNATURE_RETIRED_DEFINITION_PRESENT",
        "E_SIGNATURE_UNIVERSE_EXTRA",
        "E_STARARGS_UNRESOLVED",
        "E_SYMBOL_ESCAPE_UNRESOLVED",
        "E_THREAD_KWARGS_MISSING_REQUIRED",
        "E_THREAD_KWARGS_UNRESOLVED",
        "E_THREAD_TARGET_EXTRA",
        "E_THREAD_TARGET_MISSING",
    }
)

if len(DIAGNOSTIC_CODES) != 45:
    raise RuntimeError("V7R diagnostic table must contain exactly 45 codes")


# V7R §4.2 authorizes exactly these two production wrapper-to-core calls.
PERMITTED_PRODUCTION_CORE: frozenset[Identity] = frozenset(
    {
        (
            "api/streaming.py",
            "_run_admitted_agent_streaming",
            "_run_agent_streaming_core",
            1,
            "call",
        ),
        (
            "api/gateway_chat.py",
            "_run_admitted_gateway_chat_streaming",
            "_run_gateway_chat_streaming_core",
            1,
            "call",
        ),
    }
)


# V7R §4.1 requires this literal, checked mapping for P6 §4.5.3's eleven
# non-call identities.  Values are the exact implementation identity.
NON_CALL_DISPOSITION_MAP: dict[Identity, Identity] = {
    (
        "api/routes.py",
        "_handle_btw",
        "_run_agent_streaming",
        1,
        "thread_target",
    ): (
        "api/routes.py",
        "_handle_btw",
        "_run_admitted_agent_streaming",
        1,
        "thread_target",
    ),
    (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_agent_streaming",
        1,
        "assignment_alias",
    ): (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_admitted_agent_streaming",
        1,
        "assignment_alias",
    ),
    (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_gateway_chat_streaming",
        1,
        "assignment_alias",
    ): (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_admitted_gateway_chat_streaming",
        1,
        "assignment_alias",
    ),
    (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_agent_streaming",
        1,
        "thread_target_alias",
    ): (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_admitted_agent_streaming",
        1,
        "thread_target_alias",
    ),
    (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_gateway_chat_streaming",
        1,
        "thread_target_alias",
    ): (
        "api/routes.py",
        "_start_chat_stream_for_session",
        "_run_admitted_gateway_chat_streaming",
        1,
        "thread_target_alias",
    ),
    (
        "tests/test_agent_runtime_revision_guard.py",
        "test_stream_admission_uses_one_gateway_ownership_snapshot",
        "_run_agent_streaming",
        1,
        "assignment_alias",
    ): (
        "tests/test_agent_runtime_revision_guard.py",
        "test_stream_admission_uses_one_gateway_ownership_snapshot",
        "_run_admitted_agent_streaming",
        1,
        "assignment_alias",
    ),
    (
        "tests/test_agent_runtime_revision_guard.py",
        "test_stream_admission_uses_one_gateway_ownership_snapshot",
        "_run_gateway_chat_streaming",
        1,
        "assignment_alias",
    ): (
        "tests/test_agent_runtime_revision_guard.py",
        "test_stream_admission_uses_one_gateway_ownership_snapshot",
        "_run_admitted_gateway_chat_streaming",
        1,
        "assignment_alias",
    ),
    (
        "tests/test_gateway_approval_runs_api.py",
        "test_chat_cancel_waits_for_worker_published_run_id_before_settlement",
        "_run_gateway_chat_streaming",
        1,
        "thread_target",
    ): (
        "tests/test_gateway_approval_runs_api.py",
        "test_chat_cancel_waits_for_worker_published_run_id_before_settlement",
        "_run_admitted_gateway_chat_streaming",
        1,
        "thread_target",
    ),
    (
        "tests/test_gateway_approval_runs_api.py",
        "test_gateway_worker_prelude_exception_retires_failed_start_after_waiter_consumes",
        "_run_gateway_chat_streaming",
        1,
        "thread_target",
    ): (
        "tests/test_gateway_approval_runs_api.py",
        "test_gateway_worker_prelude_exception_retires_failed_start_after_waiter_consumes",
        "_run_admitted_gateway_chat_streaming",
        1,
        "thread_target",
    ),
    (
        "tests/test_session_active_profile_authorization.py",
        "test_chat_start_body_profile_cannot_retag_visible_empty_session_without_active_profile",
        "_start_run",
        1,
        "callback_argument",
    ): (
        "tests/test_session_active_profile_authorization.py",
        "test_chat_start_body_profile_cannot_retag_visible_empty_session_without_active_profile",
        "_start_run",
        1,
        "callback_argument",
    ),
    (
        "tests/test_session_sidecar_repair.py",
        "TestCheckpointOrdering.test_checkpoint_stops_before_recovery_code_structure",
        "_run_agent_streaming",
        1,
        "callback_argument",
    ): (
        "tests/test_session_sidecar_repair.py",
        "TestCheckpointOrdering.test_checkpoint_stops_before_recovery_code_structure",
        "_run_agent_streaming_core",
        1,
        "callback_argument",
    ),
}


# C43-C49 are behavior-only and unchanged.  The symbol is validated against the
# manifest key before these plan identities are used.
BASELINE_BEHAVIOR_IDENTITIES: frozenset[Identity] = frozenset(
    {
        (
            "api/gateway_chat.py",
            "_run_gateway_runs_api_streaming",
            "update_active_run",
            1,
            "call",
        ),
        (
            "api/gateway_chat.py",
            "_run_gateway_runs_api_streaming",
            "update_active_run",
            2,
            "call",
        ),
        (
            "api/gateway_chat.py",
            "_run_gateway_chat_streaming",
            "update_active_run",
            1,
            "call",
        ),
        (
            "api/gateway_chat.py",
            "_run_gateway_chat_streaming",
            "update_active_run",
            2,
            "call",
        ),
        (
            "api/streaming.py",
            "_run_agent_streaming",
            "update_active_run",
            1,
            "call",
        ),
        (
            "api/streaming.py",
            "_run_agent_streaming",
            "update_active_run",
            2,
            "call",
        ),
        (
            "api/streaming.py",
            "cancel_stream",
            "update_active_run",
            1,
            "call",
        ),
    }
)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    identity: Identity = ("<repository>", "<module>", "<none>", 0, "diagnostic")
    detail: str = ""

    def __post_init__(self) -> None:
        if self.code not in DIAGNOSTIC_CODES:
            raise ValueError(f"unknown diagnostic code: {self.code}")

    def render(self) -> str:
        rendered = json.dumps(self.identity, separators=(",", ":"))
        detail = " ".join(self.detail.split())
        return f"{self.code} {rendered}{f' {detail}' if detail else ''}"


def diagnostic_codes(failures: list[str]) -> set[str]:
    """Return exact stable codes from rendered verifier failures."""
    return {failure.split(" ", 1)[0] for failure in failures}


class MappingResolution(Enum):
    ABSENT = "ABSENT"
    UNRESOLVED = "UNRESOLVED"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class MappingState:
    resolution: MappingResolution
    keys: frozenset[str] = frozenset()
    alias_depth: int = 0


ABSENT_MAPPING = MappingState(MappingResolution.ABSENT)
UNRESOLVED_MAPPING = MappingState(MappingResolution.UNRESOLVED)


@dataclass(frozen=True)
class Definition:
    file: str
    scope: str
    name: str
    line: int
    positional: frozenset[str]
    kwonly: tuple[tuple[str, bool], ...]

    @property
    def identity(self) -> Identity:
        return (self.file, self.scope, self.name, 1, "definition")

    @property
    def kwonly_names(self) -> frozenset[str]:
        return frozenset(name for name, _has_default in self.kwonly)

    def field_has_default(self, name: str) -> bool:
        return any(field == name and has_default for field, has_default in self.kwonly)


@dataclass
class Policy:
    phase: str
    base_required: frozenset[str]
    impl_required: frozenset[str]
    required_fields: dict[str, frozenset[str]]
    retired: frozenset[str]
    core: frozenset[str]
    behavior_only: frozenset[str]
    transitive: frozenset[str]
    n03_symbol: str | None
    wrapper_for_old: dict[str, str]
    core_for_old: dict[str, str]
    all_watched: frozenset[str]
    host_modules: frozenset[str]

    @property
    def phase_required(self) -> frozenset[str]:
        return self.base_required if self.phase == "baseline" else self.impl_required


@dataclass
class ExpectedSurface:
    calls: set[Identity] = field(default_factory=set)
    harness_calls: set[Identity] = field(default_factory=set)
    thread_targets: set[Identity] = field(default_factory=set)
    thread_aliases: set[Identity] = field(default_factory=set)
    aliases: set[Identity] = field(default_factory=set)
    callbacks: set[Identity] = field(default_factory=set)
    partials: set[Identity] = field(default_factory=set)
    dynamics: set[Identity] = field(default_factory=set)
    behavior_only: set[Identity] = field(default_factory=set)
    core_tests: set[Identity] = field(default_factory=set)
    core_production: set[Identity] = field(default_factory=set)
    diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass
class VerifierResult:
    phase: str
    repo_root: Path | None = None
    tracked_py_files: list[str] = field(default_factory=list)
    direct_calls: list[dict] = field(default_factory=list)
    thread_targets: list[dict] = field(default_factory=list)
    aliases: list[dict] = field(default_factory=list)
    callbacks: list[dict] = field(default_factory=list)
    dynamic_refs: list[dict] = field(default_factory=list)
    core_calls: list[dict] = field(default_factory=list)
    core_refs: list[dict] = field(default_factory=list)
    transitive_calls: list[dict] = field(default_factory=list)
    behavior_only_calls: list[dict] = field(default_factory=list)
    partial_calls: list[dict] = field(default_factory=list)
    harness_calls: list[dict] = field(default_factory=list)
    n03_calls: list[dict] = field(default_factory=list)
    retired_refs: list[dict] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    scan_diagnostics: list[Diagnostic] = field(default_factory=list)
    manifest_diagnostics: list[Diagnostic] = field(default_factory=list)
    shadow_candidates: list[Diagnostic] = field(default_factory=list)
    tracked_file_count: int = 0
    status: str = "pending"
    errors: list[str] = field(default_factory=list)


def load_manifest(manifest_path: Path | None = None) -> dict:
    path = manifest_path or MANIFEST_PATH
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _identity(row: dict) -> Identity:
    value = tuple(row["identity"])
    if len(value) != 5:
        raise ValueError(f"invalid identity: {value!r}")
    return cast(Identity, value)


def _normal_execution_name(symbol: str) -> str:
    return symbol.replace("_admitted_", "_").removesuffix("_core")


def _build_policy(manifest: dict, phase: str) -> Policy:
    p5 = manifest.get("p5_required_old_symbol_manifest", {})
    design = manifest.get("chosen_p6_design_b", {})
    base_required = frozenset(p5)
    impl_required = frozenset(design.get("required_signature_universe", []))
    retired = base_required - impl_required
    added = impl_required - base_required
    core = frozenset(design.get("expected_core_direct_calls", {}))
    behavior = frozenset(manifest.get("behavior_only_manifest", {}))

    wrapper_for_old: dict[str, str] = {}
    for old in retired:
        matches = [new for new in added if _normal_execution_name(new) == old]
        if len(matches) == 1:
            wrapper_for_old[old] = matches[0]

    core_for_old: dict[str, str] = {}
    for old in retired:
        matches = [candidate for candidate in core if _normal_execution_name(candidate) == old]
        if len(matches) == 1:
            core_for_old[old] = matches[0]

    required_fields: dict[str, frozenset[str]] = {
        symbol: frozenset(fields) for symbol, fields in p5.items()
    }
    for symbol in added:
        old_matches = [old for old, wrapper in wrapper_for_old.items() if wrapper == symbol]
        if len(old_matches) == 1:
            required_fields[symbol] = frozenset(p5[old_matches[0]])
    if manifest.get("schema") == "S2-current-admission-surface-manifest-v2":
        required_fields.update(
            {
                symbol: frozenset(fields)
                for symbol, fields in manifest.get("implementation_signature_fields", {}).items()
            }
        )

    transitive_rows = manifest.get("transitive_and_planned_cleanup", {})
    transitive = frozenset(
        row[2]
        for key, row in transitive_rows.items()
        if key != "N03" and isinstance(row, list) and len(row) >= 3
    )
    n03 = transitive_rows.get("N03")
    n03_symbol = n03[2] if isinstance(n03, list) and len(n03) >= 3 else None

    watched = base_required | impl_required | retired | core | behavior | transitive
    if n03_symbol:
        watched |= {n03_symbol}

    host_modules: set[str] = set()
    baseline = manifest.get("baseline", {})
    for section in (
        "direct_call_identities",
        "non_call_reference_identities",
        "dynamic_reference_identities",
    ):
        for row in baseline.get(section, []):
            file_name = row.get("identity", [""])[0]
            if isinstance(file_name, str) and file_name.endswith(".py"):
                host_modules.add(file_name[:-3].replace("/", "."))

    return Policy(
        phase=phase,
        base_required=base_required,
        impl_required=impl_required,
        required_fields=required_fields,
        retired=retired,
        core=core,
        behavior_only=behavior,
        transitive=transitive,
        n03_symbol=n03_symbol,
        wrapper_for_old=wrapper_for_old,
        core_for_old=core_for_old,
        all_watched=frozenset(watched),
        host_modules=frozenset(host_modules),
    )


def _diagnostic(code: str, identity: Identity, detail: str = "") -> Diagnostic:
    return Diagnostic(code, identity, detail)


def _map_scope(scope: str, mapping: dict[str, str]) -> str:
    parts = scope.split(".")
    return ".".join(mapping.get(part, part) for part in parts)


def _project_behavior(manifest: dict, policy: Policy, implementation: bool) -> set[Identity]:
    behavior = manifest.get("behavior_only_manifest", {})
    explicit: list[dict] = []
    for value in behavior.values():
        if isinstance(value, list):
            explicit.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            explicit.extend(
                item for item in value.get("identities", []) if isinstance(item, dict)
            )
    if explicit:
        identities = {_identity(row) for row in explicit}
    elif manifest.get("schema") == "S2-admission-surface-manifest-v1":
        identities = set(BASELINE_BEHAVIOR_IDENTITIES)
    else:
        identities = set()
    if not implementation:
        return identities
    return {
        (file_name, _map_scope(scope, policy.core_for_old), symbol, ordinal, kind)
        for file_name, scope, symbol, ordinal, kind in identities
    }


def _project_implementation(manifest: dict, policy: Policy) -> ExpectedSurface:
    expected = ExpectedSurface()
    baseline = manifest.get("baseline", {})
    frozen = manifest.get("schema") == "S2-admission-surface-manifest-v1"

    if manifest.get("schema") == "S2-current-admission-surface-manifest-v2":
        surface = manifest.get("implementation_surface", {})

        def identities(section: str) -> set[Identity]:
            rows = surface.get(section, [])
            if not isinstance(rows, list):
                expected.diagnostics.append(
                    _diagnostic(
                        "E_MANIFEST_SELF_INCONSISTENT",
                        ("<manifest>", "<implementation_surface>", section, 0, "manifest"),
                        "section must be a list",
                    )
                )
                return set()
            return {_identity(row) for row in rows if isinstance(row, dict)}

        expected.calls = identities("direct_calls")
        expected.thread_targets = identities("thread_targets")
        expected.thread_aliases = identities("thread_target_aliases")
        expected.aliases = identities("assignment_aliases")
        expected.callbacks = identities("callbacks")
        expected.partials = identities("partials")
        expected.dynamics = identities("dynamic_references")
        expected.behavior_only = identities("behavior_only_calls")
        expected.core_tests = identities("core_test_references")
        expected.core_production = {
            identity for identity in PERMITTED_PRODUCTION_CORE if identity[2] in policy.core
        }
        return expected

    for row in baseline.get("direct_call_identities", []):
        identity = _identity(row)
        file_name, scope, symbol, ordinal, _kind = identity
        disposition = row.get("disposition")
        if not isinstance(disposition, str):
            expected.diagnostics.append(
                _diagnostic("E_DISPOSITION_UNMAPPED", identity, "missing disposition")
            )
            continue

        if disposition.startswith("pass exact admission") or disposition.startswith(
            "add admission to"
        ):
            expected.calls.add(identity)
        elif disposition.startswith("rename call to explicit non-production"):
            core = policy.core_for_old.get(symbol)
            if core:
                expected.core_tests.add((file_name, scope, core, ordinal, "call"))
            else:
                expected.diagnostics.append(
                    _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
                )
        elif disposition.startswith("move registration into corresponding admitted wrapper"):
            wrapper = policy.wrapper_for_old.get(scope)
            if wrapper:
                expected.calls.add((file_name, wrapper, symbol, 1, "call"))
            else:
                expected.diagnostics.append(
                    _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
                )
        elif disposition.startswith("move cleanup into corresponding admitted wrapper"):
            wrapper = policy.wrapper_for_old.get(scope)
            if wrapper:
                expected.calls.add((file_name, wrapper, symbol, 1, "call"))
            else:
                expected.diagnostics.append(
                    _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
                )
        elif "TurnAdmissionHarness.merge_worker" in disposition:
            expected.harness_calls.add(
                (file_name, scope, "merge_worker", ordinal, "call")
            )
        elif "TurnAdmissionHarness.close" in disposition:
            expected.harness_calls.add((file_name, scope, "close", ordinal, "call"))
        elif disposition.startswith("replace hidden-background direct call"):
            wrapper = policy.wrapper_for_old.get(symbol)
            if wrapper:
                expected.thread_targets.add(
                    (file_name, scope, wrapper, 1, "thread_target")
                )
            else:
                expected.diagnostics.append(
                    _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
                )
        elif disposition.startswith("migrate q-missing ownership test"):
            wrapper = policy.wrapper_for_old.get(symbol)
            if wrapper:
                expected.calls.add((file_name, scope, wrapper, 1, "call"))
            else:
                expected.diagnostics.append(
                    _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
                )
        elif disposition.startswith("remove product unregister") or disposition.startswith(
            "replace with explicit legacy_permitless"
        ):
            pass
        else:
            expected.diagnostics.append(
                _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
            )

    for row in baseline.get("non_call_reference_identities", []):
        identity = _identity(row)
        mapped = NON_CALL_DISPOSITION_MAP.get(identity)
        if mapped is None and identity[2] in policy.impl_required:
            mapped = identity
        if mapped is None:
            expected.diagnostics.append(
                _diagnostic("E_DISPOSITION_UNMAPPED", identity, "non-call identity")
            )
            continue
        kind = mapped[4]
        if kind == "thread_target":
            expected.thread_targets.add(mapped)
        elif kind == "thread_target_alias":
            expected.thread_aliases.add(mapped)
        elif kind == "assignment_alias":
            expected.aliases.add(mapped)
        elif kind == "callback_argument":
            expected.callbacks.add(mapped)
        else:
            expected.diagnostics.append(
                _diagnostic("E_DISPOSITION_UNMAPPED", identity, f"kind={kind}")
            )

    known_dynamic_prefixes = (
        "fake accepts",
        "negative sentinel",
        "positive/recording fake",
        "retain behavior-only monkeypatch",
    )
    for row in baseline.get("dynamic_reference_identities", []):
        identity = _identity(row)
        disposition = row.get("disposition")
        if not isinstance(disposition, str):
            if identity[2] in policy.impl_required:
                expected.dynamics.add(identity)
            else:
                expected.diagnostics.append(
                    _diagnostic("E_DISPOSITION_UNMAPPED", identity, "dynamic identity")
                )
        elif disposition.startswith("remove obsolete core-worker monkeypatch"):
            pass
        elif disposition.startswith("patch admitted wrapper selected"):
            wrapper = policy.wrapper_for_old.get(identity[2])
            if wrapper:
                expected.dynamics.add(
                    (identity[0], identity[1], wrapper, identity[3], identity[4])
                )
            else:
                expected.diagnostics.append(
                    _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
                )
        elif disposition.startswith(known_dynamic_prefixes):
            expected.dynamics.add(identity)
        else:
            expected.diagnostics.append(
                _diagnostic("E_DISPOSITION_UNMAPPED", identity, disposition)
            )

    for row in baseline.get("partial_reference_identities", []):
        identity = _identity(row)
        if identity[2] in policy.impl_required:
            expected.partials.add(identity)
        else:
            expected.diagnostics.append(
                _diagnostic("E_DISPOSITION_UNMAPPED", identity, "partial identity")
            )

    expected.behavior_only = _project_behavior(manifest, policy, implementation=True)
    expected.core_production = {
        identity for identity in PERMITTED_PRODUCTION_CORE if identity[2] in policy.core
    }

    if frozen:
        behavior_symbols = {identity[2] for identity in BASELINE_BEHAVIOR_IDENTITIES}
        if behavior_symbols != set(policy.behavior_only):
            expected.diagnostics.append(
                _diagnostic(
                    "E_MANIFEST_SELF_INCONSISTENT",
                    ("<manifest>", "<projection>", "behavior_only", 0, "manifest"),
                    f"identity_symbols={sorted(behavior_symbols)} "
                    f"manifest_symbols={sorted(policy.behavior_only)}",
                )
            )
        prepare_symbols = [
            symbol
            for symbol, fields in policy.required_fields.items()
            if "inspected_completion_events" in fields
        ]
        register_symbols = [
            symbol
            for symbol, fields in policy.required_fields.items()
            if "admission" in fields and symbol.startswith("register_")
        ]
        unregister_symbols = [
            symbol
            for symbol, fields in policy.required_fields.items()
            if {"owner_token", "reason"}.issubset(fields)
        ]
        if len(prepare_symbols) == len(register_symbols) == len(unregister_symbols) == 1:
            prepare = prepare_symbols[0]
            register = register_symbols[0]
            unregister = unregister_symbols[0]
            expected.calls.update(
                {
                    ("api/routes.py", "_handle_btw", prepare, 1, "call"),
                    ("api/routes.py", "_handle_background", prepare, 1, "call"),
                    (
                        "tests/turn_admission_factory.py",
                        "TurnAdmissionHarness.merge_worker",
                        register,
                        1,
                        "call",
                    ),
                    (
                        "tests/turn_admission_factory.py",
                        "TurnAdmissionHarness.close",
                        unregister,
                        1,
                        "call",
                    ),
                }
            )
            n03 = manifest.get("transitive_and_planned_cleanup", {}).get("N03")
            if isinstance(n03, list) and len(n03) >= 3:
                expected.calls.add((n03[0], n03[1], n03[2], 1, "call"))
        else:
            expected.diagnostics.append(
                _diagnostic(
                    "E_MANIFEST_SELF_INCONSISTENT",
                    ("<manifest>", "<projection>", "signature_roles", 0, "manifest"),
                    "unable to derive prepare/register/unregister roles",
                )
            )

    if not expected.diagnostics:
        design = manifest.get("chosen_p6_design_b", {})
        observed_direct = Counter(identity[2] for identity in expected.calls)
        declared_direct = Counter(design.get("expected_direct_calls_by_symbol", {}))
        core_counts = Counter(
            identity[2] for identity in expected.core_tests | expected.core_production
        )
        declared_core = Counter(design.get("expected_core_direct_calls", {}))
        declared_threads = design.get("expected_admitted_thread_target_edges", 0)
        observed_threads = len(expected.thread_targets | expected.thread_aliases)
        reasons: list[str] = []
        if observed_direct != declared_direct:
            reasons.append(f"direct={dict(observed_direct)} declared={dict(declared_direct)}")
        if len(expected.calls) != design.get("expected_direct_calls_total", 0):
            reasons.append(
                f"direct_total={len(expected.calls)} "
                f"declared={design.get('expected_direct_calls_total', 0)}"
            )
        if core_counts != declared_core:
            reasons.append(f"core={dict(core_counts)} declared={dict(declared_core)}")
        if observed_threads != declared_threads:
            reasons.append(f"threads={observed_threads} declared={declared_threads}")
        if frozen:
            baseline_count = sum(
                len(baseline.get(section, []))
                for section in (
                    "direct_call_identities",
                    "non_call_reference_identities",
                    "dynamic_reference_identities",
                )
            )
            if baseline_count != 191:
                reasons.append(f"baseline_projection_rows={baseline_count} declared=191")
        if reasons:
            expected.diagnostics.append(
                _diagnostic(
                    "E_MANIFEST_SELF_INCONSISTENT",
                    ("<manifest>", "<projection>", "Design-B", 0, "manifest"),
                    "; ".join(reasons),
                )
            )

    return expected


@dataclass
class _Frame:
    aliases: dict[str, frozenset[str]] = field(default_factory=dict)
    dicts: dict[str, MappingState] = field(default_factory=dict)
    parameters: frozenset[str] = frozenset()


class _ScopeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        filename: str,
        policy: Policy,
        expected_harness: set[Identity],
    ) -> None:
        self.filename = filename
        self.policy = policy
        self.expected_harness = expected_harness
        self.scope_stack: list[str] = []
        self.frames: list[_Frame] = [_Frame()]
        self.ordinals: dict[tuple[str, str, str], int] = {}
        self.imported_symbols: dict[str, frozenset[str]] = {}
        self.module_aliases: dict[str, str] = {}
        self.partial_aliases: set[str] = {"partial"}
        self.thread_aliases: set[str] = {"Thread"}
        self.dynamic_anchors: set[tuple[str, str]] = set()

        self.direct_calls: list[dict] = []
        self.thread_targets: list[dict] = []
        self.aliases: list[dict] = []
        self.callbacks: list[dict] = []
        self.dynamic_refs: list[dict] = []
        self.core_calls: list[dict] = []
        self.core_refs: list[dict] = []
        self.transitive_calls: list[dict] = []
        self.behavior_only_calls: list[dict] = []
        self.partial_calls: list[dict] = []
        self.harness_calls: list[dict] = []
        self.n03_calls: list[dict] = []
        self.retired_refs: list[dict] = []
        self.definitions: list[Definition] = []
        self.diagnostics: list[Diagnostic] = []
        self.shadow_candidates: list[Diagnostic] = []

    @property
    def frame(self) -> _Frame:
        return self.frames[-1]

    def _scope(self) -> str:
        return ".".join(self.scope_stack) if self.scope_stack else "<module>"

    def _ordinal(self, symbol: str, kind: str) -> int:
        key = (self._scope(), symbol, kind)
        self.ordinals[key] = self.ordinals.get(key, 0) + 1
        return self.ordinals[key]

    def _make_identity(self, symbol: str, kind: str) -> Identity:
        return (
            self.filename,
            self._scope(),
            symbol,
            self._ordinal(symbol, kind),
            kind,
        )

    def _append_ref(self, destination: list[dict], symbol: str, kind: str, **extra) -> dict:
        row = {"identity": self._make_identity(symbol, kind), **extra}
        destination.append(row)
        return row

    def _resolve_symbols(self, node: ast.AST) -> frozenset[str]:
        if isinstance(node, ast.Name):
            if node.id in self.frame.aliases:
                return self.frame.aliases[node.id]
            if node.id in self.imported_symbols:
                return self.imported_symbols[node.id]
            if node.id in self.policy.all_watched:
                return frozenset({node.id})
        elif isinstance(node, ast.Attribute):
            if node.attr in self.policy.all_watched:
                return frozenset({node.attr})
        elif isinstance(node, ast.IfExp):
            return self._resolve_symbols(node.body) | self._resolve_symbols(node.orelse)
        return frozenset()

    def _record_reference(self, symbol: str, kind: str, **extra) -> None:
        if symbol in self.policy.core:
            row = self._append_ref(self.core_refs, symbol, kind, **extra)
            if kind == "call":
                self.core_calls.append(row)
            return
        if self.policy.phase == "implementation" and symbol in self.policy.retired:
            self._append_ref(self.retired_refs, symbol, kind, **extra)
            return
        if symbol == self.policy.n03_symbol and kind == "call" and self._scope().endswith(
            "release_turn_admission"
        ):
            self._append_ref(self.n03_calls, symbol, kind, **extra)
            return
        if symbol in self.policy.behavior_only and kind == "call":
            self._append_ref(self.behavior_only_calls, symbol, kind, **extra)
            return
        if symbol in self.policy.transitive and kind == "call":
            self._append_ref(self.transitive_calls, symbol, "transitive_call", **extra)
            return
        if kind == "call":
            self._append_ref(self.direct_calls, symbol, kind, **extra)
        elif kind in {"thread_target", "thread_target_alias"}:
            self._append_ref(self.thread_targets, symbol, kind, **extra)
        elif kind == "assignment_alias":
            self._append_ref(self.aliases, symbol, kind, **extra)
        elif kind == "callback_argument":
            self._append_ref(self.callbacks, symbol, kind, **extra)
        elif kind == "partial":
            self._append_ref(self.partial_calls, symbol, kind, **extra)
        else:
            self._append_ref(self.dynamic_refs, symbol, kind, **extra)

    def _mapping_state(self, node: ast.AST, *, alias: bool = False) -> MappingState:
        if isinstance(node, ast.Dict):
            keys: set[str] = set()
            depth = 0
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    unpacked = self._mapping_state(value)
                    if unpacked.resolution is not MappingResolution.RESOLVED:
                        return UNRESOLVED_MAPPING
                    keys.update(unpacked.keys)
                    depth = max(depth, unpacked.alias_depth)
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
                else:
                    return UNRESOLVED_MAPPING
            return MappingState(MappingResolution.RESOLVED, frozenset(keys), depth)
        if isinstance(node, ast.Name):
            state = self.frame.dicts.get(node.id, UNRESOLVED_MAPPING)
            if alias and state.resolution is MappingResolution.RESOLVED:
                if state.alias_depth >= 1:
                    return UNRESOLVED_MAPPING
                return MappingState(state.resolution, state.keys, state.alias_depth + 1)
            return state
        return UNRESOLVED_MAPPING

    def _call_mapping_state(self, node: ast.Call, keyword: str) -> MappingState:
        for item in node.keywords:
            if item.arg == keyword:
                return self._mapping_state(item.value)
        return ABSENT_MAPPING

    def _star_kwargs_state(self, node: ast.Call) -> MappingState:
        unpacked = [item.value for item in node.keywords if item.arg is None]
        if not unpacked:
            return ABSENT_MAPPING
        keys: set[str] = set()
        for value in unpacked:
            state = self._mapping_state(value)
            if state.resolution is not MappingResolution.RESOLVED:
                return UNRESOLVED_MAPPING
            keys.update(state.keys)
        return MappingState(MappingResolution.RESOLVED, frozenset(keys))

    def _starargs_unresolved(self, node: ast.Call) -> bool:
        for argument in node.args:
            if not isinstance(argument, ast.Starred):
                continue
            value = argument.value
            if not isinstance(value, ast.Tuple) or not all(
                isinstance(element, ast.Constant) for element in value.elts
            ):
                return True
        return False

    def _call_metadata(self, node: ast.Call) -> dict:
        state = self._star_kwargs_state(node)
        return {
            "keyword_names": sorted(
                item.arg for item in node.keywords if item.arg is not None
            ),
            "has_star_kwargs": state.resolution is not MappingResolution.ABSENT,
            "mapping_resolution": state.resolution.value,
            "resolved_const_keys": sorted(state.keys),
            "has_star_args": any(isinstance(argument, ast.Starred) for argument in node.args),
        }

    def _record_starargs(self, node: ast.Call, symbols: frozenset[str]) -> None:
        if self._starargs_unresolved(node):
            for symbol in sorted(symbols):
                identity = (
                    self.filename,
                    self._scope(),
                    symbol,
                    self.ordinals.get((self._scope(), symbol, "call"), 0) or 1,
                    "call",
                )
                self.diagnostics.append(
                    _diagnostic("E_STARARGS_UNRESOLVED", identity, "non-constant *args")
                )

    def _add_imported_symbol(self, local: str, symbol: str) -> None:
        existing = self.imported_symbols.get(local, frozenset())
        combined = existing | {symbol}
        self.imported_symbols[local] = frozenset(combined)
        if existing and combined != existing:
            identity = (
                self.filename,
                self._scope(),
                symbol,
                1,
                "import_alias",
            )
            self.diagnostics.append(
                _diagnostic(
                    "E_IMPORT_ALIAS_AMBIGUOUS",
                    identity,
                    f"local={local} symbols={sorted(combined)}",
                )
            )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            self.module_aliases[local] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name in self.policy.all_watched:
                self._add_imported_symbol(local, alias.name)
            if module == "functools" and alias.name == "partial":
                self.partial_aliases.add(local)
            if module == "threading" and alias.name == "Thread":
                self.thread_aliases.add(local)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.scope_stack.pop()

    def _definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Definition:
        positional = frozenset(
            argument.arg
            for argument in node.args.posonlyargs + node.args.args
        )
        kwonly = tuple(
            (argument.arg, default is not None)
            for argument, default in zip(
                node.args.kwonlyargs,
                node.args.kw_defaults,
                strict=True,
            )
        )
        return Definition(
            self.filename,
            self._scope(),
            node.name,
            node.lineno,
            positional,
            kwonly,
        )

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        definition = self._definition(node)
        self.definitions.append(definition)
        if self.scope_stack and node.name in self.policy.all_watched:
            identity = (
                self.filename,
                self._scope(),
                node.name,
                1,
                "lexical_shadow",
            )
            self.shadow_candidates.append(
                _diagnostic("E_LEXICAL_SHADOW_AMBIGUOUS", identity, f"line={node.lineno}")
            )

        parameters = frozenset(
            argument.arg
            for argument in (
                node.args.posonlyargs
                + node.args.args
                + node.args.kwonlyargs
            )
        )
        if node.args.vararg:
            parameters |= {node.args.vararg.arg}
        if node.args.kwarg:
            parameters |= {node.args.kwarg.arg}
        self.scope_stack.append(node.name)
        self.frames.append(_Frame(parameters=parameters))
        for statement in node.body:
            self.visit(statement)
        self.frames.pop()
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _assign_name(self, name: str, value: ast.AST, node: ast.AST) -> None:
        symbols = self._resolve_symbols(value)
        if symbols:
            self.frame.aliases[name] = symbols
            for symbol in sorted(symbols):
                self._record_reference(symbol, "assignment_alias", alias=name)
        elif name in self.frame.aliases:
            prior = sorted(self.frame.aliases.pop(name))
            identity = (
                self.filename,
                self._scope(),
                prior[0],
                1,
                "assignment_alias",
            )
            self.diagnostics.append(
                _diagnostic(
                    "E_ALIAS_AMBIGUOUS",
                    identity,
                    f"alias={name} rebound at line={getattr(node, 'lineno', 0)}",
                )
            )

        if name in self.policy.all_watched and self.scope_stack and not symbols:
            identity = (self.filename, self._scope(), name, 1, "lexical_shadow")
            self.shadow_candidates.append(
                _diagnostic("E_LEXICAL_SHADOW_AMBIGUOUS", identity, "assignment shadow")
            )

        if isinstance(value, ast.Name):
            self.frame.dicts[name] = self._mapping_state(value, alias=True)
        else:
            self.frame.dicts[name] = self._mapping_state(value)

    def _record_mutable_escape(self, value: ast.AST, node: ast.AST) -> None:
        for symbol in sorted(self._resolve_symbols(value)):
            identity = (
                self.filename,
                self._scope(),
                symbol,
                1,
                "symbol_escape",
            )
            self.diagnostics.append(
                _diagnostic(
                    "E_SYMBOL_ESCAPE_UNRESOLVED",
                    identity,
                    f"line={getattr(node, 'lineno', 0)}",
                )
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        if isinstance(node.value, (ast.Dict, ast.List, ast.Tuple)):
            values = node.value.values if isinstance(node.value, ast.Dict) else node.value.elts
            for value in values:
                self._record_mutable_escape(value, node)

        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._assign_name(node.targets[0].id, node.value, node)
            return

        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], (ast.Tuple, ast.List))
            and isinstance(node.value, (ast.Tuple, ast.List))
            and len(node.targets[0].elts) == len(node.value.elts)
        ):
            for target, value in zip(node.targets[0].elts, node.value.elts, strict=True):
                if isinstance(target, ast.Name):
                    self._assign_name(target.id, value, node)
            return

        for target in node.targets:
            if isinstance(target, ast.Subscript):
                self._record_mutable_escape(node.value, node)
                if isinstance(target.value, ast.Name):
                    state = self.frame.dicts.get(target.value.id, UNRESOLVED_MAPPING)
                    if (
                        isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                        and state.resolution is MappingResolution.RESOLVED
                    ):
                        self.frame.dicts[target.value.id] = MappingState(
                            MappingResolution.RESOLVED,
                            state.keys | {target.slice.value},
                            state.alias_depth,
                        )
                    else:
                        self.frame.dicts[target.value.id] = UNRESOLVED_MAPPING
            else:
                self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            if isinstance(node.target, ast.Name):
                self._assign_name(node.target.id, node.value, node)
            elif isinstance(node.target, ast.Subscript):
                self._record_mutable_escape(node.value, node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self._record_mutable_escape(node.value, node)
            self.visit(node.value)

    def _leaf_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _is_thread_call(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id in self.thread_aliases
        if isinstance(node.func, ast.Attribute):
            return node.func.attr == "Thread"
        return False

    def _is_partial_call(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id in self.partial_aliases
        if isinstance(node.func, ast.Attribute):
            return node.func.attr == "partial"
        return False

    def _dynamic_receiver(self, node: ast.AST) -> str:
        return ast.dump(node, include_attributes=False)

    def _record_dynamic_symbol(self, symbol: str, kind: str) -> None:
        self._record_reference(symbol, kind)

    def _handle_dynamic_call(self, node: ast.Call) -> bool:
        leaf = self._leaf_name(node.func)
        if (
            leaf == "get"
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "__dict__"
            and node.args
        ):
            namespace = node.func.value
            name = node.args[0]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                if name.value in self.policy.all_watched:
                    self.dynamic_anchors.add(
                        (self._scope(), self._dynamic_receiver(namespace))
                    )
                    self._record_dynamic_symbol(name.value, "subscript")
            elif (self._scope(), self._dynamic_receiver(namespace)) in self.dynamic_anchors:
                identity = (
                    self.filename,
                    self._scope(),
                    "<computed>",
                    1,
                    "subscript",
                )
                self.diagnostics.append(
                    _diagnostic("E_DYNAMIC_NAME_UNRESOLVED", identity)
                )
            return True
        if leaf not in {"getattr", "setattr", "hasattr", "delattr"}:
            return False
        if len(node.args) < 2:
            return False
        receiver = node.args[0]
        name = node.args[1]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            if name.value in self.policy.all_watched:
                if (
                    isinstance(receiver, ast.Call)
                    and self._leaf_name(receiver.func) == "import_module"
                ):
                    identity = (
                        self.filename,
                        self._scope(),
                        name.value,
                        1,
                        "dynamic_indirection",
                    )
                    self.diagnostics.append(
                        _diagnostic("E_DYNAMIC_INDIRECTION_FORBIDDEN", identity)
                    )
                    return True
                self.dynamic_anchors.add((self._scope(), self._dynamic_receiver(receiver)))
                self._record_dynamic_symbol(name.value, leaf)
            return True

        anchored = (self._scope(), self._dynamic_receiver(receiver)) in self.dynamic_anchors
        if anchored:
            identity = (
                self.filename,
                self._scope(),
                "<computed>",
                1,
                leaf,
            )
            self.diagnostics.append(
                _diagnostic("E_DYNAMIC_NAME_UNRESOLVED", identity, f"line={node.lineno}")
            )
        return True

    def _handle_forbidden_indirection(self, node: ast.Call) -> bool:
        leaf = self._leaf_name(node.func)
        if leaf in {"eval", "exec"} and node.args:
            expression = node.args[0]
            touches = isinstance(expression, ast.Constant) and any(
                symbol in str(expression.value)
                for symbol in self.policy.all_watched
            )
            if touches:
                identity = (
                    self.filename,
                    self._scope(),
                    leaf,
                    1,
                    "dynamic_indirection",
                )
                self.diagnostics.append(
                    _diagnostic("E_DYNAMIC_INDIRECTION_FORBIDDEN", identity)
                )
                return True
        return False

    def _handle_dict_update(self, node: ast.Call) -> bool:
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
        ):
            return False
        name = node.func.value.id
        if name not in self.frame.dicts:
            return False
        current = self.frame.dicts[name]
        if current.resolution is not MappingResolution.RESOLVED or len(node.args) > 1:
            self.frame.dicts[name] = UNRESOLVED_MAPPING
            return True
        additions: set[str] = {item.arg for item in node.keywords if item.arg is not None}
        if any(item.arg is None for item in node.keywords):
            self.frame.dicts[name] = UNRESOLVED_MAPPING
            return True
        if node.args:
            state = self._mapping_state(node.args[0])
            if state.resolution is not MappingResolution.RESOLVED:
                self.frame.dicts[name] = UNRESOLVED_MAPPING
                return True
            additions.update(state.keys)
        self.frame.dicts[name] = MappingState(
            MappingResolution.RESOLVED,
            current.keys | additions,
            current.alias_depth,
        )
        return True

    def _handle_thread(self, node: ast.Call) -> None:
        target: ast.AST | None = None
        for item in node.keywords:
            if item.arg == "target":
                target = item.value
                break
        if target is None:
            return
        symbols = self._resolve_symbols(target)
        alias_target = isinstance(target, ast.Name) and target.id in self.frame.aliases
        state = self._call_mapping_state(node, "kwargs")
        metadata = {
            "thread_mapping_resolution": state.resolution.value,
            "thread_kwarg_names": sorted(state.keys),
        }
        for symbol in sorted(symbols):
            kind = "thread_target_alias" if alias_target else "thread_target"
            self._record_reference(symbol, kind, **metadata)

    def _handle_partial(self, node: ast.Call) -> None:
        if not node.args:
            return
        symbols = self._resolve_symbols(node.args[0])
        metadata = self._call_metadata(node)
        for symbol in sorted(symbols):
            self._record_reference(symbol, "partial", **metadata)
        self._record_starargs(node, symbols)

    def visit_Call(self, node: ast.Call) -> None:
        if self._handle_forbidden_indirection(node):
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return
        if self._handle_dynamic_call(node):
            for argument in node.args[2:]:
                for symbol in sorted(self._resolve_symbols(argument)):
                    self._record_reference(symbol, "callback_argument")
            for keyword in node.keywords:
                for symbol in sorted(self._resolve_symbols(keyword.value)):
                    self._record_reference(symbol, "callback_argument")
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return
        if self._handle_dict_update(node):
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return
        if self._is_thread_call(node):
            self._handle_thread(node)
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return
        if self._is_partial_call(node):
            self._handle_partial(node)
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and node.args
        ):
            self._record_mutable_escape(node.args[0], node)

        metadata = self._call_metadata(node)
        leaf = self._leaf_name(node.func)
        is_harness_site = leaf is not None and any(
            identity[:3] == (self.filename, self._scope(), leaf)
            for identity in self.expected_harness
        )
        if is_harness_site and leaf is not None:
            self._append_ref(self.harness_calls, leaf, "call", **metadata)
            symbols = frozenset()
        else:
            symbols = self._resolve_symbols(node.func)
            for symbol in sorted(symbols):
                self._record_reference(symbol, "call", **metadata)
        self._record_starargs(node, symbols)

        for argument in node.args:
            if isinstance(argument, ast.Starred):
                continue
            for symbol in sorted(self._resolve_symbols(argument)):
                self._record_reference(symbol, "callback_argument")
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            for symbol in sorted(self._resolve_symbols(keyword.value)):
                self._record_reference(symbol, "callback_argument")

        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        is_globals = isinstance(node.value, ast.Call) and self._leaf_name(node.value.func) == "globals"
        if is_globals:
            name = node.slice
            touches = not isinstance(name, ast.Constant) or name.value in self.policy.all_watched
            if touches:
                identity = (
                    self.filename,
                    self._scope(),
                    "globals",
                    1,
                    "dynamic_indirection",
                )
                self.diagnostics.append(
                    _diagnostic("E_DYNAMIC_INDIRECTION_FORBIDDEN", identity)
                )
            return

        namespace = (
            isinstance(node.value, ast.Attribute) and node.value.attr == "__dict__"
        ) or (
            isinstance(node.value, ast.Name) and node.value.id in self.module_aliases
        )
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            if node.slice.value in self.policy.all_watched and namespace:
                self.dynamic_anchors.add((self._scope(), self._dynamic_receiver(node.value)))
                self._record_dynamic_symbol(node.slice.value, "subscript")
        elif namespace and (
            (self._scope(), self._dynamic_receiver(node.value)) in self.dynamic_anchors
        ):
            identity = (
                self.filename,
                self._scope(),
                "<computed>",
                1,
                "subscript",
            )
            self.diagnostics.append(_diagnostic("E_DYNAMIC_NAME_UNRESOLVED", identity))
        self.generic_visit(node)


def _scan_repo(
    repo_root: Path,
    policy: Policy,
    expected_harness: set[Identity],
) -> VerifierResult:
    result = VerifierResult(phase=policy.phase, repo_root=repo_root)
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        result.scan_diagnostics.append(
            _diagnostic(
                "E_GIT_ENUMERATION_FAILED",
                ("<repository>", "<git>", "ls-files", 0, "git"),
                str(exc),
            )
        )
        result.status = "failed"
        return result
    if proc.returncode != 0:
        result.scan_diagnostics.append(
            _diagnostic(
                "E_GIT_ENUMERATION_FAILED",
                ("<repository>", "<git>", "ls-files", 0, "git"),
                f"exit={proc.returncode}",
            )
        )
        result.status = "failed"
        return result
    try:
        tracked = [item.decode("utf-8") for item in proc.stdout.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        result.scan_diagnostics.append(
            _diagnostic(
                "E_GIT_ENUMERATION_FAILED",
                ("<repository>", "<git>", "decode", 0, "git"),
                str(exc),
            )
        )
        result.status = "failed"
        return result
    if not tracked:
        result.scan_diagnostics.append(
            _diagnostic(
                "E_GIT_ENUMERATION_FAILED",
                ("<repository>", "<git>", "empty", 0, "git"),
                "git ls-files returned no tracked Python files",
            )
        )
        result.status = "failed"
        return result

    result.tracked_py_files = tracked
    result.tracked_file_count = len(tracked)
    for relative in tracked:
        path = repo_root / relative
        try:
            source = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.scan_diagnostics.append(
                _diagnostic(
                    "E_FILE_READ_ERROR",
                    (relative, "<module>", "<read>", 0, "file"),
                    str(exc),
                )
            )
            continue
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            result.scan_diagnostics.append(
                _diagnostic(
                    "E_PARSE_ERROR",
                    (relative, "<module>", "<parse>", 0, "file"),
                    f"line={exc.lineno} offset={exc.offset} msg={exc.msg}",
                )
            )
            continue

        visitor = _ScopeVisitor(relative, policy, expected_harness)
        visitor.visit(tree)
        result.direct_calls.extend(visitor.direct_calls)
        result.thread_targets.extend(visitor.thread_targets)
        result.aliases.extend(visitor.aliases)
        result.callbacks.extend(visitor.callbacks)
        result.dynamic_refs.extend(visitor.dynamic_refs)
        result.core_calls.extend(visitor.core_calls)
        result.core_refs.extend(visitor.core_refs)
        result.transitive_calls.extend(visitor.transitive_calls)
        result.behavior_only_calls.extend(visitor.behavior_only_calls)
        result.partial_calls.extend(visitor.partial_calls)
        result.harness_calls.extend(visitor.harness_calls)
        result.n03_calls.extend(visitor.n03_calls)
        result.retired_refs.extend(visitor.retired_refs)
        result.definitions.extend(visitor.definitions)
        result.scan_diagnostics.extend(visitor.diagnostics)
        result.shadow_candidates.extend(visitor.shadow_candidates)
    result.status = "scanned"
    return result


def verify(
    repo_root: Path,
    phase: str,
    manifest: dict | None = None,
    manifest_path: Path | None = None,
) -> VerifierResult:
    if manifest is None:
        manifest = load_manifest(manifest_path)
    policy = _build_policy(manifest, phase)
    projected = _project_implementation(manifest, policy)
    result = _scan_repo(repo_root, policy, projected.harness_calls)
    if phase == "implementation" or manifest.get("schema") == "S2-admission-surface-manifest-v1":
        result.manifest_diagnostics.extend(projected.diagnostics)
    return result


def _found(rows: list[dict]) -> set[Identity]:
    return {_identity(row) for row in rows}


def _set_diagnostics(
    found: set[Identity],
    expected: set[Identity],
    missing_code: str,
    extra_code: str,
) -> list[Diagnostic]:
    diagnostics = [
        _diagnostic(missing_code, identity) for identity in sorted(expected - found)
    ]
    diagnostics.extend(
        _diagnostic(extra_code, identity) for identity in sorted(found - expected)
    )
    return diagnostics


def _baseline_expected(manifest: dict, section: str, kind: str | None = None) -> set[Identity]:
    rows = manifest.get("baseline", {}).get(section, [])
    identities = {_identity(row) for row in rows}
    return identities if kind is None else {identity for identity in identities if identity[4] == kind}


def _signature_diagnostics(
    result: VerifierResult,
    manifest: dict,
    policy: Policy,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    required = policy.base_required if result.phase == "baseline" else policy.impl_required
    module_definitions = [definition for definition in result.definitions if definition.scope == "<module>"]
    by_name: dict[str, list[Definition]] = defaultdict(list)
    for definition in module_definitions:
        by_name[definition.name].append(definition)

    for symbol in sorted(required):
        definitions = by_name.get(symbol, [])
        identity = (
            definitions[0].identity
            if definitions
            else ("<repository>", "<module>", symbol, 1, "definition")
        )
        if not definitions:
            diagnostics.append(_diagnostic("E_SIGNATURE_DEFINITION_MISSING", identity))
            continue
        if len(definitions) > 1:
            diagnostics.append(
                _diagnostic(
                    "E_SIGNATURE_DEFINITION_DUPLICATE",
                    identity,
                    f"count={len(definitions)}",
                )
            )
            continue
        if result.phase == "baseline":
            continue
        definition = definitions[0]
        for field_name in sorted(policy.required_fields.get(symbol, frozenset())):
            if field_name in definition.positional:
                diagnostics.append(
                    _diagnostic("E_SIGNATURE_FIELD_NOT_KWONLY", identity, field_name)
                )
            elif field_name not in definition.kwonly_names:
                diagnostics.append(
                    _diagnostic("E_SIGNATURE_FIELD_MISSING", identity, field_name)
                )
            elif definition.field_has_default(field_name):
                diagnostics.append(
                    _diagnostic("E_SIGNATURE_FIELD_HAS_DEFAULT", identity, field_name)
                )

    if result.phase == "implementation":
        for symbol in sorted(policy.retired):
            for definition in by_name.get(symbol, []):
                diagnostics.append(
                    _diagnostic(
                        "E_SIGNATURE_RETIRED_DEFINITION_PRESENT",
                        definition.identity,
                    )
                )

        for definition in module_definitions:
            if definition.name in required | policy.core | policy.retired:
                continue
            kwonly = definition.kwonly_names
            admission_signal = bool(
                {"admission", "inspected_completion_events"} & kwonly
            )
            cleanup_signal = {"owner_token", "reason"}.issubset(kwonly)
            if admission_signal or cleanup_signal:
                diagnostics.append(
                    _diagnostic(
                        "E_SIGNATURE_UNIVERSE_EXTRA",
                        definition.identity,
                        f"fields={sorted(kwonly)}",
                    )
                )
    return diagnostics


def _kwargs_diagnostics(result: VerifierResult, policy: Policy) -> list[Diagnostic]:
    if result.phase != "implementation":
        return []
    diagnostics: list[Diagnostic] = []
    for row in result.direct_calls + result.partial_calls:
        identity = _identity(row)
        required = policy.required_fields.get(identity[2], frozenset())
        if not required:
            continue
        explicit = set(row.get("keyword_names", []))
        state = MappingResolution(row.get("mapping_resolution", "ABSENT"))
        if state is MappingResolution.UNRESOLVED:
            diagnostics.append(_diagnostic("E_KWARGS_UNRESOLVED", identity))
            continue
        supplied = explicit | set(row.get("resolved_const_keys", []))
        missing = required - supplied
        if missing:
            diagnostics.append(
                _diagnostic(
                    "E_KWARGS_MISSING_REQUIRED",
                    identity,
                    f"missing={sorted(missing)}",
                )
            )

    for row in result.thread_targets:
        identity = _identity(row)
        required = policy.required_fields.get(identity[2], frozenset())
        if not required:
            continue
        state = MappingResolution(row.get("thread_mapping_resolution", "ABSENT"))
        if state is MappingResolution.UNRESOLVED:
            diagnostics.append(_diagnostic("E_THREAD_KWARGS_UNRESOLVED", identity))
            continue
        missing = required - set(row.get("thread_kwarg_names", []))
        if missing:
            diagnostics.append(
                _diagnostic(
                    "E_THREAD_KWARGS_MISSING_REQUIRED",
                    identity,
                    f"missing={sorted(missing)}",
                )
            )
    return diagnostics


def _core_diagnostics(
    result: VerifierResult,
    expected: ExpectedSurface,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    observed_test_calls: set[Identity] = set()
    permitted_triples = {
        identity[:3] for identity in expected.core_production
    }
    for row in result.core_refs:
        identity = _identity(row)
        file_name, scope, symbol, ordinal, kind = identity
        if file_name.startswith("tests/"):
            if identity in expected.core_tests:
                observed_test_calls.add(identity)
            else:
                diagnostics.append(_diagnostic("E_CORE_TEST_IDENTITY_UNKNOWN", identity))
            continue
        if identity in expected.core_production:
            continue
        if (file_name, scope, symbol) in permitted_triples and kind == "call" and ordinal > 1:
            diagnostics.append(
                _diagnostic("E_CORE_DUPLICATE_PRODUCTION_CALL", identity)
            )
        else:
            diagnostics.append(_diagnostic("E_CORE_BYPASS_FORBIDDEN", identity))
    for identity in sorted(expected.core_tests - observed_test_calls):
        diagnostics.append(_diagnostic("E_CORE_TEST_IDENTITY_MISSING", identity))
    return diagnostics


def _n03_diagnostics(result: VerifierResult, manifest: dict) -> list[Diagnostic]:
    n03 = manifest.get("transitive_and_planned_cleanup", {}).get("N03")
    if not isinstance(n03, list) or len(n03) < 3:
        return []
    expected = (n03[0], n03[1], n03[2], 1, "call")
    observed = [
        identity
        for identity in _found(result.n03_calls)
        if identity[:3] == expected[:3]
    ]
    if not observed:
        return [_diagnostic("E_N03_MISSING", expected)]
    if len(observed) > 1:
        return [_diagnostic("E_N03_EXTRA", identity) for identity in sorted(observed[1:])]
    return []


def _c50_diagnostics(result: VerifierResult, manifest: dict) -> list[Diagnostic]:
    c50 = manifest.get("transitive_and_planned_cleanup", {}).get("C50")
    if not isinstance(c50, list) or len(c50) < 5:
        return []
    expected = tuple(c50)
    observed = _found(result.transitive_calls)
    matches = {
        identity
        for identity in observed
        if identity[0] == expected[0]
        and (identity[1] == expected[1] or identity[1].endswith(f".{expected[1]}"))
        and identity[2:] == expected[2:]
    }
    if len(matches) != 1:
        return [_diagnostic("E_C50_MISSING", expected)]
    return []


def _shadow_diagnostics(
    result: VerifierResult,
    expected_references: set[Identity],
) -> list[Diagnostic]:
    pinned = {(identity[0], identity[1], identity[2]) for identity in expected_references}
    return [
        diagnostic
        for diagnostic in result.shadow_candidates
        if diagnostic.identity[:3] not in pinned
    ]


def _render_diagnostics(diagnostics: list[Diagnostic]) -> list[str]:
    unique = {
        (diagnostic.code, diagnostic.identity, diagnostic.detail): diagnostic
        for diagnostic in diagnostics
    }
    ordered = sorted(
        unique.values(),
        key=lambda item: (item.code, *item.identity, item.detail),
    )
    return [diagnostic.render() for diagnostic in ordered]


def check_baseline(result: VerifierResult, manifest: dict) -> list[str]:
    policy = _build_policy(manifest, "baseline")
    diagnostics = list(result.manifest_diagnostics) + list(result.scan_diagnostics)
    if any(item.code == "E_GIT_ENUMERATION_FAILED" for item in diagnostics):
        return _render_diagnostics(diagnostics)

    expected_calls = _baseline_expected(manifest, "direct_call_identities")
    expected_non_calls = _baseline_expected(manifest, "non_call_reference_identities")
    expected_dynamic = _baseline_expected(manifest, "dynamic_reference_identities")
    expected_partial = _baseline_expected(manifest, "partial_reference_identities")
    expected_behavior = _project_behavior(manifest, policy, implementation=False)

    diagnostics.extend(
        _set_diagnostics(
            _found(result.direct_calls),
            expected_calls,
            "E_CALL_MISSING",
            "E_CALL_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            {identity for identity in _found(result.thread_targets) if identity[4] == "thread_target"},
            {identity for identity in expected_non_calls if identity[4] == "thread_target"},
            "E_THREAD_TARGET_MISSING",
            "E_THREAD_TARGET_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            {
                identity
                for identity in _found(result.thread_targets)
                if identity[4] == "thread_target_alias"
            },
            {
                identity
                for identity in expected_non_calls
                if identity[4] == "thread_target_alias"
            },
            "E_THREAD_TARGET_MISSING",
            "E_THREAD_TARGET_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.aliases),
            {identity for identity in expected_non_calls if identity[4] == "assignment_alias"},
            "E_ALIAS_MISSING",
            "E_ALIAS_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.callbacks),
            {identity for identity in expected_non_calls if identity[4] == "callback_argument"},
            "E_CALLBACK_MISSING",
            "E_CALLBACK_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.partial_calls),
            expected_partial,
            "E_PARTIAL_MISSING",
            "E_PARTIAL_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.dynamic_refs),
            expected_dynamic,
            "E_DYNAMIC_MISSING",
            "E_DYNAMIC_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.behavior_only_calls),
            expected_behavior,
            "E_BEHAVIOR_ONLY_MISSING",
            "E_BEHAVIOR_ONLY_EXTRA",
        )
    )
    diagnostics.extend(_core_diagnostics(result, ExpectedSurface()))
    diagnostics.extend(_c50_diagnostics(result, manifest))
    diagnostics.extend(_signature_diagnostics(result, manifest, policy))
    diagnostics.extend(_shadow_diagnostics(result, expected_non_calls | expected_dynamic))
    return _render_diagnostics(diagnostics)


def check_implementation(result: VerifierResult, manifest: dict) -> list[str]:
    policy = _build_policy(manifest, "implementation")
    expected = _project_implementation(manifest, policy)
    diagnostics = list(result.manifest_diagnostics) + list(result.scan_diagnostics)
    if any(item.code == "E_GIT_ENUMERATION_FAILED" for item in diagnostics):
        return _render_diagnostics(diagnostics)

    n03 = manifest.get("transitive_and_planned_cleanup", {}).get("N03")
    n03_identity = (
        (n03[0], n03[1], n03[2], 1, "call")
        if isinstance(n03, list) and len(n03) >= 3
        else None
    )
    expected_calls = set(expected.calls)
    if n03_identity:
        expected_calls.discard(n03_identity)

    diagnostics.extend(
        _set_diagnostics(
            _found(result.direct_calls),
            expected_calls,
            "E_CALL_MISSING",
            "E_CALL_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.harness_calls),
            expected.harness_calls,
            "E_CALL_MISSING",
            "E_CALL_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            {identity for identity in _found(result.thread_targets) if identity[4] == "thread_target"},
            expected.thread_targets,
            "E_THREAD_TARGET_MISSING",
            "E_THREAD_TARGET_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            {
                identity
                for identity in _found(result.thread_targets)
                if identity[4] == "thread_target_alias"
            },
            expected.thread_aliases,
            "E_THREAD_TARGET_MISSING",
            "E_THREAD_TARGET_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.aliases),
            expected.aliases,
            "E_ALIAS_MISSING",
            "E_ALIAS_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.callbacks),
            expected.callbacks,
            "E_CALLBACK_MISSING",
            "E_CALLBACK_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.partial_calls),
            expected.partials,
            "E_PARTIAL_MISSING",
            "E_PARTIAL_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.dynamic_refs),
            expected.dynamics,
            "E_DYNAMIC_MISSING",
            "E_DYNAMIC_EXTRA",
        )
    )
    diagnostics.extend(
        _set_diagnostics(
            _found(result.behavior_only_calls),
            expected.behavior_only,
            "E_BEHAVIOR_ONLY_MISSING",
            "E_BEHAVIOR_ONLY_EXTRA",
        )
    )
    diagnostics.extend(_core_diagnostics(result, expected))
    diagnostics.extend(_n03_diagnostics(result, manifest))
    diagnostics.extend(_c50_diagnostics(result, manifest))
    diagnostics.extend(_signature_diagnostics(result, manifest, policy))
    diagnostics.extend(_kwargs_diagnostics(result, policy))
    diagnostics.extend(
        _diagnostic("E_RETIRED_SYMBOL_REFERENCE", _identity(row))
        for row in result.retired_refs
    )
    expected_references = (
        expected.thread_targets
        | expected.thread_aliases
        | expected.aliases
        | expected.callbacks
        | expected.dynamics
    )
    diagnostics.extend(_shadow_diagnostics(result, expected_references))
    return _render_diagnostics(diagnostics)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["baseline", "implementation"], required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    manifest = load_manifest()
    result = verify(args.repo, args.phase, manifest=manifest)
    failures = (
        check_baseline(result, manifest)
        if args.phase == "baseline"
        else check_implementation(result, manifest)
    )

    ordinary_static = sum(
        not call.get("has_star_kwargs", False) for call in result.direct_calls
    )
    constant_kwargs = sum(
        call.get("has_star_kwargs", False)
        and call.get("mapping_resolution") == MappingResolution.RESOLVED.value
        for call in result.direct_calls
    )
    c50 = manifest.get("transitive_and_planned_cleanup", {}).get("C50")
    c50_found = isinstance(c50, list) and not _c50_diagnostics(result, manifest)
    output = {
        "phase": args.phase,
        "status": result.status,
        "tracked_files": result.tracked_file_count,
        "parse_errors": sum(
            diagnostic.code == "E_PARSE_ERROR" for diagnostic in result.scan_diagnostics
        ),
        "direct_calls": len(result.direct_calls),
        "ordinary_static": ordinary_static,
        "constant_key_star_kwargs": constant_kwargs,
        "thread_targets": len(result.thread_targets),
        "aliases": len(result.aliases),
        "callbacks": len(result.callbacks),
        "non_call_references": (
            len(result.thread_targets) + len(result.aliases) + len(result.callbacks)
        ),
        "partial_calls": len(result.partial_calls),
        "dynamic_refs": len(result.dynamic_refs),
        "behavior_only_direct_calls": len(result.behavior_only_calls),
        "C50": c50_found,
        "diagnostic_codes": sorted(diagnostic_codes(failures)),
        "failures": failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(
        f"PASS phase={args.phase} files={result.tracked_file_count} "
        f"direct={len(result.direct_calls)} "
        f"(ordinary={ordinary_static} const_kwargs={constant_kwargs}) "
        f"thread={len(result.thread_targets)} aliases={len(result.aliases)} "
        f"callbacks={len(result.callbacks)} "
        f"non_call={len(result.thread_targets) + len(result.aliases) + len(result.callbacks)} "
        f"partial={len(result.partial_calls)} dynamic={len(result.dynamic_refs)} "
        f"behonly={len(result.behavior_only_calls)} C50={c50_found}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())