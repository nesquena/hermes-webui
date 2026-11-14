# Per-user access and request review

Governance → Users has independent access level, access mode and access-request review
controls. Changes require `governance:write`; browser controls do not replace
server authorization. User edits retain the policy's optimistic `If-Match`
revision check and audit trail.

Activate the global policy's `mode: enforce` to apply these restrictions and
mandatory command reviews. `off` disables these policy decisions; `report_only` records
them without policy blocking. Independent workspace, bot and project ACLs still
apply. Resumed jobs retain their original hard ceilings; a continuation guarded
by an enforced policy can fail closed if enforcement is later disabled.
Saving per-user controls does not silently
change this global operating mode. Upgrade the WebUI and engine together before
activation, and retain an authenticated bootstrap recovery owner.

```yaml
users:
  person@example.test:
    roles: [team-member]
    access_level: user
    access_mode: whitelist
    approval:
      mode: automatic
      prompt: >-
        Allow public research and reading assigned project documents.
        Deny sharing private project content externally.
        Ask for manual review when the destination or purpose is unclear.
    grants:
      permissions: [chat:use, profiles:read]
      routes: [/api/chat*, /api/profiles*, /api/profile*]
      profiles: [assigned-profile]
      tools:
        builtins: [web_search]
    deny:
      tools:
        builtins: [execute_code]
```

The assigned role/group grants define available resources, including profiles,
workspaces, files, models, tools and command scopes. Whitelist mode limits direct
user grants to that envelope. Blacklist mode allows operational resources by
default, subject to explicit per-user denies and the access-level ceiling.
Environment secrets still require explicit grants. Bot Screens independently
require explicit screen grants regardless of the selected access mode.

| Control | Behavior |
|---|---|
| User | Uses assigned work capabilities; cannot gain governance administration, terminal/arbitrary code execution, settings/provider writes or administrative permissions. |
| Elevated | Uses assigned additional operational capabilities, including an explicitly granted terminal. Governance administration stays excluded. |
| Admin | Provides the administrative capability envelope. It remains subject to the selected access mode and explicit denies. |
| Whitelist | Starts without work capabilities. Direct user grants permit only explicitly selected capabilities within the role/resource envelope. |
| Blacklist | Allows operational resources except explicit per-user denies, subject to the access-level ceiling. |
| Manual approval | Requests for additional capabilities remain pending in Governance → Approvals for an authorized reviewer. |
| Automatic approval | A configured auxiliary model applies this user's admin-authored prompt to the exact access request. It can approve, reject, or leave the request pending for a human. |

The editor suggests known identities, roles and skills. Fields without a complete catalog, such as file paths, model names and environment names, accept a typed value. The server validates the resulting policy when it is saved.

An explicit deny wins over wildcard allowances and all role/group grants. Tool,
MCP tool/server, route, profile, model, skill, file and command checks retain the
deny separately from the displayed effective allowlist. Roles never grant access
to unassigned profiles/workspaces implicitly. Self identity and own request
status remain reachable for an empty managed whitelist; work APIs stay denied.

Bootstrap owners are the existing recovery exception: they bypass ordinary
policy. The user editor identifies them and disables per-user controls so the
screen does not suggest they are restricted. An assignable Admin is distinct
from a bootstrap owner.

## Access requests and command approvals

Since the 14 September governance change, a person's `approval: {mode, prompt}`
controls access requests in Governance → Approvals. It does not review every
ordinary tool call. Already permitted reads and writes continue normally.
An automatic approval grants only the requested capability to its owner and
must pass the same hard bounds as a human decision. Explicit denials, role
ceilings and resource restrictions cannot be overridden by the model.

The reviewer uses the auxiliary-model `approval` route, a 30-second timeout,
a strict decision/reason/confidence response and a 0.9 confidence floor.
Unavailable, uncertain or oversized reviews leave the request pending.
Request text is untrusted data, separate from the administrator's rules;
the reviewer has no tools. An automatic prompt must be nonempty and at most
8,000 characters. Decisions and their reasons remain on the request and in the
normal grant-decision audit trail.

Mandatory `cli.approval_commands` remain a separate runtime safeguard. Matching
commands require a one-action human approval through the connected chat,
voice or gateway interface even if access-request review is automatic.
The command must already pass hard permission and resource checks. The engine
rechecks current access after the wait. Approval cannot make a denied command
permitted; no connected review interface means it cannot proceed.

Managed users with an explicit file-root, file-pattern or environment-variable
deny cannot run host terminal or arbitrary-code tools, including the direct
terminal API. Repository-wide Git APIs are also blocked: diff, status, history
and Git operations can expose or modify files beyond a selected file grant.
Programs can derive paths internally, so command text review cannot enforce
those resource restrictions. A separately isolated execution environment is
required to combine restricted resources with arbitrary programs. An elevated
user without such denies may be granted trusted host execution; that remains
host privilege. Neither AI nor human approval overrides this boundary.

## Compatibility and paired upgrade

Existing users with omitted access controls and approval settings keep their
previous behavior. Creating a new user explicitly defaults to User, Whitelist
and Manual. Saving an unrelated field on a legacy user does not silently opt
them into a new mode.
Changing only the access level preserves the existing role/group/direct grant
union, subject to the selected level's ceiling. An omitted approval setting
preserves existing approval behavior even when another access control changes.
Manual access-request review leaves requests pending, including when access mode
and level are left unchanged. Session-wide approval and skip-all requests cannot
bypass mandatory command review; the server enforces its one-action scope.

WebUI and Hermes Agent must be upgraded together. New runtime context payloads
carry deny, role ceiling, access mode and approval configuration. The WebUI
refuses a governed turn if the engine cannot round-trip the new controls.
The runtime evaluates current policy even when a generated profile's resources
have not yet synchronized; a profile copy is not an authorization decision.
In-process delegated agents inherit the live bot/project/workspace access checkers and
policy source through the existing context propagation. Their mandatory command reviews
use the parent's connected review interface; unattended subagent autoapproval
does not satisfy mandatory governance review. External-process payloads retain resource
ceilings but cannot carry live ACL callbacks. Tool and model entry points
that require bot/project/workspace checks remain blocked until that host
supplies the authoritative checkers.

## API additions

User create/update and full-policy validation accept `access_level`,
`access_mode`, and `approval: {mode, prompt}`. Unknown values and malformed
prompts return validation errors. `/api/governance/users` additionally returns
`bootstrap_admins` separately from persisted entries. `/api/governance/me` and
preview include the selected controls; preview provides full policy-shaped
`grants`, `deny`, and `role_ceiling`. Own identity responses omit the review
prompt. The admin policy endpoints retain the prompt for editing.

Tests: `tests/test_governance_per_user_modes.py`, `tests/test_grant_auto_review.py`
and `tests/test_governance_request_bounds.py` in WebUI, plus
`tests/test_governance_per_user_actions.py` and
`tests/test_governance_cli_approval_commands.py` in Hermes Agent. They cover the
actual gateway queue, serialization, policy races, model parser/fallback,
concrete deny under wildcard, and final dispatch side effects. Browser evidence
and real-provider checks are separate from deterministic automated tests.
