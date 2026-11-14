"""Plain-English capability and risk metadata for governance approvals.

Reported 28 Aug 2026 ("Explain to administrators what they are approving"): the
approvals queue showed a derived label ("API route: /api/crons") and nothing
else, so an administrator had to already know the policy model to decide. This
module is the ONLY place a human-readable capability or risk sentence is
written; every other surface derives its text from here.

Authoritative sources, never guesses
------------------------------------
Each map is keyed off a structure that already decides the behaviour, and a
coverage test pins the key sets together so a new kind cannot ship without a
description:

    GKIND_RISKS      api.grant_requests._GRANT_TARGETS  (what approving writes)
    PERMISSION_RISKS api.governance.catalog.ROUTE_CATALOG + governance.nav
    TOOLSET_RISKS    api.config._DEFAULT_TOOLSETS
    KIND_RISKS       api.approvals.KINDS

Two things this module deliberately refuses to say:

* A route grant does NOT confer a permission. api/grant_requests.py:47-50 is
  explicit: "Approving adds the path to the user's routes allowlist; the
  permission layer still applies, so a route grant alone never confers an admin
  capability." The permission is reported as context ("still required"), never
  as the thing being granted. The spool item carries no HTTP method
  (api/grant_requests.py:132-136), so both the read and the write permission of
  the matching route rule are listed rather than one of them being picked.
* A built-in tool grant does not come with a description of the tool. The tool
  registry lives in the engine, so the honest answer is the policy effect plus
  a note that the tool itself is not described here.

No credential, header name, file content, URL or module path is written into
any sentence below, and nothing here reads a secret-bearing file.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Risk flags. The ids travel into the approval_decision audit row, and the
# audit sink redacts any extra KEY matching api/governance/audit.py's
# _SECRET_KEY_RE (api_key|secret|password|passwd|token|authorization|
# credential|refresh). These five are chosen to miss that pattern so the
# digest of what an administrator was shown stays readable in the trail.
RISK_EXTERNAL_COMMS = "external_comms"
RISK_DATA_ACCESS = "data_access"
RISK_FILE_WRITE = "file_write"
RISK_SCHEDULING = "scheduling"
RISK_FINANCIAL = "financial"
RISKS = (
    RISK_EXTERNAL_COMMS,
    RISK_DATA_ACCESS,
    RISK_FILE_WRITE,
    RISK_SCHEDULING,
    RISK_FINANCIAL,
)

RISK_DESCRIPTIONS = {
    RISK_EXTERNAL_COMMS: "It could send information to people outside the conversation.",
    RISK_DATA_ACCESS: "It could read personal or confidential information within the granted scope.",
    RISK_FILE_WRITE: "It could create, overwrite or change files within the granted scope.",
    RISK_SCHEDULING: "It could schedule work that runs later without someone watching.",
    RISK_FINANCIAL: "It could use paid services or cause spending.",
}


def unavailable_explanation():
    return {
        "capability": "The platform could not determine what this request would allow.",
        "data": "The data and actions in scope have not been verified.",
        "scope_text": "Ask the requester for the exact action and scope before deciding.",
        "risks": [],
        "status": "unavailable",
    }

# There is no expiry mechanism in api/approvals.py or in the policy document,
# so the duration line states that rather than implying a time box.
DURATION_UNTIL_REVOKED = (
    "Stays in effect until an administrator takes it away. Nothing expires on its own."
)

SCOPE_TEXTS = {
    "user": (
        "This one person only. The change is written on their own entry, "
        "not on a role and not on a group."
    ),
    "owner": "Only the person who asked for it. Nobody else gains anything.",
    "global": "Everybody who works in this workstation.",
}


# ── Permissions ─────────────────────────────────────────────────────────────
# Keyed on the permission names the route catalog and the panel map already
# assign. "narrower" is a plain sentence naming a smaller option; raw
# permission slugs are fine here because this text only ever renders inside the
# governance admin panel, which already shows permission chips verbatim.

PERMISSION_RISKS = {
    "analytics:read": {
        "capability": "See the usage and cost reporting for this workstation.",
        "data": "How much everyone used and what it cost.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "chat:use": {
        "capability": "Run the assistant: start conversations and have it do work.",
        "data": "The conversation, its personal memory and the data reachable through the allowed tools.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_FINANCIAL),
        "narrower": "",
        "depends_on": ("sessions:read",),
    },
    "config:read": {
        "capability": "Look at the workstation settings and the saved prompts.",
        "data": "Settings values, prompt text and the command list.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "config:write": {
        "capability": "Change the workstation settings, providers and prompts for everyone.",
        "data": "Every shared setting, including which providers are used.",
        "risks": (RISK_DATA_ACCESS, RISK_FINANCIAL),
        "narrower": "config:read allows looking at the settings without changing them.",
        "depends_on": ("config:read",),
    },
    "cron:read": {
        "capability": "See the scheduled jobs and how they ran.",
        "data": "Job names, schedules and their run history.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "cron:run": {
        "capability": "Start a scheduled job by hand, right now.",
        "data": "Everything the job itself touches, with the job owner's reach.",
        "risks": (RISK_SCHEDULING, RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "narrower": "cron:read allows watching the jobs without starting any.",
        "depends_on": ("cron:read",),
    },
    "cron:write": {
        "capability": "Create, change and remove scheduled jobs.",
        "data": "The schedule itself, and through it whatever a job is set up to do.",
        "risks": (RISK_SCHEDULING,),
        "narrower": "cron:read allows watching the jobs without changing them.",
        "depends_on": ("cron:read",),
    },
    "dashboard:read": {
        "capability": "Open the shared dashboard.",
        "data": "The figures the dashboard is built on.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "dashboard:write": {
        "capability": "Change what the shared dashboard shows.",
        "data": "The dashboard layout everyone sees.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "dashboard:read allows viewing it without changing it.",
        "depends_on": ("dashboard:read",),
    },
    "files:read": {
        "capability": "Open and download files in the allowed folders.",
        "data": "Documents and notes in the folders the person's profile allows.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "files:write": {
        "capability": "Create, change, upload and delete files in the allowed folders.",
        "data": "Documents and notes in the folders the person's profile allows.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "narrower": "files:read allows opening the same files without changing them.",
        "depends_on": ("files:read",),
    },
    "gateway:read": {
        "capability": "See whether the shared gateway is healthy.",
        "data": "Gateway status and its recent activity.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "gateway:restart": {
        "capability": "Restart the shared gateway, which briefly interrupts everyone.",
        "data": "No stored data, but every running conversation is cut off.",
        "risks": (),
        "narrower": "gateway:read allows watching its health without restarting it.",
        "depends_on": ("gateway:read",),
    },
    "git:read": {
        "capability": "See the version history of the working folders.",
        "data": "File history, including anything ever committed to it.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "git:write": {
        "capability": "Commit, revert and otherwise rewrite the version history.",
        "data": "The working folders and everything recorded in their history.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "narrower": "git:read allows reading the history without rewriting it.",
        "depends_on": ("git:read",),
    },
    "governance:audit:read": {
        "capability": "Read the governance trail of who was allowed or refused what.",
        "data": "Every recorded decision, including who asked for what and when.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "governance:preview": {
        "capability": "Try out what a policy change would do before it is saved.",
        "data": "The access rules, and what any person would end up with.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "governance:read": {
        "capability": "Read the access rules: who belongs to which role and group.",
        "data": "The full list of people, roles and what each of them may do.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "governance:usage:read": {
        "capability": "See per-person usage figures for the workstation.",
        "data": "How much each named person used.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "governance:write": {
        "capability": "Change the access rules and decide other people's requests.",
        "data": "Everything, indirectly: this is the permission that hands out permissions.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "governance:read allows reviewing the rules without changing them.",
        "depends_on": ("governance:read",),
    },
    "integrations:connect": {
        "capability": "Connect an outside account, such as a mailbox or a calendar.",
        "data": "Whatever the connected account holds, once it is connected.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_DATA_ACCESS),
        "narrower": "integrations:read allows seeing which services exist without connecting one.",
        "depends_on": ("integrations:read",),
    },
    "integrations:read": {
        "capability": "See which outside services are available and which are connected.",
        "data": "Service names and connection status, never the stored login itself.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "kanban:read": {
        "capability": "Open the shared work board.",
        "data": "The cards on the board and what they say.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "logs:read": {
        "capability": "Read the workstation logs.",
        "data": "Diagnostic records, which can quote what people asked for.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "mcp:read": {
        "capability": "See which connected services are set up.",
        "data": "The names and addresses of the connected services.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "mcp:write": {
        "capability": "Add, change and remove connected services for everyone.",
        "data": "Whatever a newly added service can reach, for every user of this workstation.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_DATA_ACCESS),
        "narrower": "mcp:read allows reviewing the connected services without changing them.",
        "depends_on": ("mcp:read",),
    },
    "model:read": {
        "capability": "See which models are available and which one is selected.",
        "data": "Model names and the current selection.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "model:write": {
        "capability": "Choose which model the workstation uses.",
        "data": "The shared model choice, which sets the going rate for everyone's work.",
        "risks": (RISK_FINANCIAL,),
        "narrower": "model:read allows seeing the choice without changing it.",
        "depends_on": ("model:read",),
    },
    "plugins:read": {
        "capability": "See which add-ons are installed.",
        "data": "Add-on names and their status.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "plugins:write": {
        "capability": "Install, update and remove add-ons for everyone.",
        "data": "Whatever an installed add-on can reach, for every user of this workstation.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "narrower": "plugins:read allows reviewing the add-ons without installing any.",
        "depends_on": ("plugins:read",),
    },
    "profiles:admin": {
        "capability": "Create and change the working profiles other people run under.",
        "data": "The tool and folder reach every profile hands its users.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "profiles:read allows seeing the profiles without changing them.",
        "depends_on": ("profiles:read",),
    },
    "profiles:read": {
        "capability": "See which working profiles exist and switch between the allowed ones.",
        "data": "Profile names and their settings.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "self:read": {
        "capability": "View the status of your own access requests.",
        "data": "The requests and decisions associated with your signed-in identity.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "sessions:read": {
        "capability": "Open past conversations and their history.",
        "data": "Conversation text, which can be as sensitive as the work it was about.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
    "sessions:write": {
        "capability": "Start, rename and delete conversations and projects.",
        "data": "Conversation history, which can be removed as well as added to.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "sessions:read allows opening conversations without changing them.",
        "depends_on": ("sessions:read",),
    },
    "skills:read": {
        "capability": "See the installed skills and read what they do.",
        "data": "Skill instructions, including any working notes their author left in them.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "skills:write": {
        "capability": "Add, edit and delete skills, which are instructions the assistant follows.",
        "data": "The instruction set every conversation on this workstation draws on.",
        "risks": (RISK_FILE_WRITE,),
        "narrower": "skills:read allows reading the skills without editing them.",
        "depends_on": ("skills:read",),
    },
    "status:read": {
        "capability": "Check that the workstation is up.",
        "data": "Health status only.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "system:ops": {
        "capability": "Restart, shut down and update the workstation itself.",
        "data": "No stored data, but everyone's work stops while it happens.",
        "risks": (RISK_FILE_WRITE,),
        "narrower": "system:read allows watching the machine without acting on it.",
        "depends_on": ("system:read",),
    },
    "system:read": {
        "capability": "See how the machine is doing: memory, disk and available updates.",
        "data": "Machine measurements only.",
        "risks": (),
        "narrower": "",
        "depends_on": (),
    },
    "terminal:use": {
        "capability": "Run commands directly on this machine.",
        "data": "Everything the machine account can reach, files included.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "narrower": "files:read covers looking at files without the ability to run anything.",
        "depends_on": (),
    },
    "todos:read": {
        "capability": "Open the shared task list.",
        "data": "Tasks and what they say.",
        "risks": (RISK_DATA_ACCESS,),
        "narrower": "",
        "depends_on": (),
    },
}


# ── Access requests (kind "grant") ──────────────────────────────────────────
# Keyed EXACTLY on api.grant_requests._GRANT_TARGETS, which is what approving
# actually writes into the policy document. Each sentence describes the WRITE,
# not the capability the person may or may not already hold.

GKIND_RISKS = {
    "skill": {
        "capability": "Lets this person use one skill: a set of written instructions the assistant follows.",
        "data": "Whatever the skill's own steps reach when it runs.",
        "risks": (),
        "mitigation": "Read the skill before approving. Its text steers what the assistant does.",
        "narrower": "",
        "depends_on": (),
    },
    "skill_manage": {
        "capability": "Lets this person change one skill: the written instructions the assistant follows for everyone who uses it.",
        "data": "The skill's text and supporting files. Every later run of that skill, by anyone, follows the edited version.",
        "risks": (),
        "mitigation": "Approve for people who maintain that skill. Read the change afterwards: an edit reaches every user of the skill, not only the editor.",
        "narrower": "A one-off change can be made by an admin instead of granting editing rights.",
        "depends_on": (),
    },
    "cli": {
        "capability": "Lets this person have the assistant run one command on this machine.",
        "data": "Everything that command can reach on the machine.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "mitigation": "Approve the exact command that was asked for, never a broader one.",
        "narrower": "",
        "depends_on": (),
    },
    "workdir": {
        "capability": "Lets this person run their allowed commands from one more folder.",
        "data": "The contents of that folder, for the commands they may already run.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "mitigation": "Pick the narrowest folder that does the job, not the folder above it.",
        "narrower": "",
        "depends_on": ("The person also needs the commands themselves to be allowed.",),
    },
    "file_read": {
        "capability": "Lets this person open files in one more folder.",
        "data": "Everything stored in that folder and below it.",
        "risks": (RISK_DATA_ACCESS,),
        "mitigation": "Check what else lives in that folder: the whole tree comes with it.",
        "narrower": "",
        "depends_on": (),
    },
    "file_write": {
        "capability": "Lets this person create, change and remove files in one more folder.",
        "data": "Everything stored in that folder and below it.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "mitigation": "Grant read access instead when looking at the files is all that was needed.",
        "narrower": "A read-only grant on the same folder is the smaller version of this.",
        "depends_on": (),
    },
    "mcp": {
        "capability": "Lets this person use one connected service, with every tool that service offers.",
        "data": "Whatever the connected service holds on the other side.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_DATA_ACCESS),
        "mitigation": "Approving turns on all of the service's tools at once, so approve the service only if the whole of it is acceptable.",
        "narrower": "",
        "depends_on": ("The service itself must already be set up on this workstation.",),
    },
    "tool": {
        "capability": "Lets this person use one more built-in tool. What that tool does is not described here: check the tool by name before approving.",
        "data": "",
        "risks": (),
        "mitigation": "Look the tool up by name first. This screen will not tell you what it does.",
        "narrower": "",
        "depends_on": (),
    },
    "toolset": {
        "capability": "Lets this person use a whole group of built-in tools at once.",
        "data": "Whatever that group of tools reaches.",
        "risks": (),
        "mitigation": "A single tool can be granted instead when only one of them was actually needed.",
        "narrower": "Granting the one tool that was blocked is the smaller version of this.",
        "depends_on": (),
    },
    "profile": {
        "capability": "Lets this person work under one more profile.",
        "data": "Everything that profile's own tool and folder settings reach.",
        "risks": (RISK_DATA_ACCESS,),
        "mitigation": "Check what the profile itself allows: the person inherits all of it.",
        "narrower": "",
        "depends_on": (),
    },
    "workspace": {
        "capability": "Lets this person work in one more workspace.",
        "data": "The files and conversations that belong to that workspace.",
        "risks": (RISK_DATA_ACCESS,),
        "mitigation": "Confirm the workspace really is theirs to work in.",
        "narrower": "",
        "depends_on": (),
    },
    "route": {
        "capability": (
            "Makes one blocked address in this workstation reachable for this person. "
            "It hands out no permission of its own: whatever that screen already "
            "required, they still need."
        ),
        "data": "Nothing extra by itself. It only removes the address from the blocked list.",
        "risks": (),
        "mitigation": "Check what this person may already do before approving: this grant only matters if they hold the permission below.",
        "narrower": "",
        "depends_on": (),
    },
    "permission": {
        "capability": (
            "Lets this person do one more kind of thing here. Unlike an address, "
            "this is the check behind the screen, so it applies wherever that "
            "kind of thing is done."
        ),
        "data": "Whatever that kind of thing reaches. The sentence below says which.",
        "risks": (),
        "mitigation": "Only the read-shaped ones can be granted this way. Anything that changes, restarts or runs something is set in the access rules instead, where the whole entry is in view.",
        "narrower": "",
        "depends_on": (),
    },
    "secret_glob": {
        "capability": "Lets this person open one exact file that the rules deliberately hold back.",
        "data": "That one file, and nothing else. Files of this kind are held back because of what they usually hold.",
        "risks": (RISK_DATA_ACCESS,),
        "mitigation": "Ask why the file is needed and confirm it with the person, out loud, before approving. Consider moving what they need into a normal file instead, and take the exception away again afterwards.",
        "narrower": "Copying the one value they need into an ordinary file avoids the exception altogether.",
        "depends_on": (),
    },
}


# ── Toolsets ────────────────────────────────────────────────────────────────
# Keyed on api.config._DEFAULT_TOOLSETS.

TOOLSET_RISKS = {
    "browser": {
        "capability": "Open web pages and act on them, as a person with a browser would.",
        "data": "Any site reachable from this machine, signed-in sessions included.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_DATA_ACCESS),
    },
    "clarify": {
        "capability": "Ask the person a follow-up question mid-task.",
        "data": "Nothing beyond the conversation itself.",
        "risks": (),
    },
    "code_execution": {
        "capability": "Write and run code on this machine.",
        "data": "Everything the machine account can reach.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
    },
    "cronjob": {
        "capability": "Set up work that runs on a schedule, without anyone present.",
        "data": "Whatever the scheduled work is set up to touch.",
        "risks": (RISK_SCHEDULING, RISK_FILE_WRITE),
    },
    "delegation": {
        "capability": "Hand parts of a task to other assistants that run on their own.",
        "data": "Whatever those assistants reach in turn.",
        "risks": (RISK_DATA_ACCESS,),
    },
    "file": {
        "capability": "Read and write files in the allowed folders.",
        "data": "Documents and notes in the folders the profile allows.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
    },
    "image_gen": {
        "capability": "Generate images.",
        "data": "The prompt text and the resulting image files.",
        "risks": (RISK_FINANCIAL,),
    },
    "memory": {
        "capability": "Remember things across conversations and read them back.",
        "data": "Saved notes about people, projects and preferences.",
        "risks": (RISK_DATA_ACCESS,),
    },
    "session_search": {
        "capability": "Search back through earlier conversations.",
        "data": "Everything said in the conversations it can reach.",
        "risks": (RISK_DATA_ACCESS,),
    },
    "skills": {
        "capability": "Look up and follow the installed skills.",
        "data": "The instructions in those skills and whatever their steps reach.",
        "risks": (),
    },
    "terminal": {
        "capability": "Run commands directly on this machine.",
        "data": "Everything the machine account can reach, files included.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
    },
    "todo": {
        "capability": "Keep a task list while working.",
        "data": "The task text only.",
        "risks": (),
    },
    "web": {
        "capability": "Search the web and fetch pages.",
        "data": "Public pages, plus whatever is put into a search box.",
        "risks": (RISK_EXTERNAL_COMMS,),
    },
    "webhook": {
        "capability": "Call outside systems and receive calls back from them.",
        "data": "Whatever is sent out and whatever comes back.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_DATA_ACCESS),
    },
}


# ── Self-service request kinds ──────────────────────────────────────────────
# Keyed on api.approvals.KINDS. "grant" is the fallback sentence for an access
# request whose kind of grant this module does not recognise; every recognised
# one is answered from GKIND_RISKS instead.

KIND_RISKS = {
    "skill": {
        "capability": "Publishes a skill somebody added: a set of written instructions the assistant follows.",
        "data": "Whatever the skill's own steps reach when it runs.",
        "risks": (),
        "mitigation": "Read the skill text before approving. Approving makes it available to everyone here.",
        "narrower": "",
        "depends_on": (),
    },
    "integration": {
        "capability": "Turns on an outside service so people here can connect their own account to it.",
        "data": "Whatever each person's own connected account holds.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_DATA_ACCESS),
        "mitigation": "Each person still connects their own account, so approving does not hand you theirs.",
        "narrower": "",
        "depends_on": (),
    },
    "mcp": {
        "capability": "Adds a connected service to this workstation.",
        "data": "Whatever the service holds on the other side.",
        "risks": (RISK_EXTERNAL_COMMS, RISK_DATA_ACCESS),
        "mitigation": "Check the address it points at. A service that needs a login is installed switched off, so somebody has to fill the login in and switch it on afterwards.",
        "narrower": "",
        "depends_on": (),
    },
    "cli": {
        "capability": "Allows one command to be run on this machine.",
        "data": "Everything that command can reach on the machine.",
        "risks": (RISK_FILE_WRITE, RISK_DATA_ACCESS),
        "mitigation": "Approve the exact command that was asked for, never a broader one.",
        "narrower": "",
        "depends_on": (),
    },
    "grant": {
        "capability": "An access request. The kind of access could not be identified here, so approve it only if you recognise what was asked for.",
        "data": "",
        "risks": (),
        "mitigation": "Ask the person what they were doing when they were stopped.",
        "narrower": "",
        "depends_on": (),
    },
}


# ── Composition ─────────────────────────────────────────────────────────────

_EMPTY = {
    "capability": "",
    "data": "",
    "tools": [],
    "external_systems": [],
    "permissions": [],
    "permission_notes": [],
    "scope": "",
    "scope_text": "",
    "duration": "",
    "expires_at": None,
    "risks": [],
    "mitigations": [],
    "alternatives": [],
    "dependencies": [],
    "policy_target": [],
    "source": "",
}

_SKILL_DESCRIPTION_MAX = 240


def _blank() -> dict:
    """A fresh copy of the stable return shape: every field always present."""
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _EMPTY.items()}


def _dedup(values) -> list:
    """Order-preserving de-duplication for the list fields."""
    seen, out = set(), []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _skill_description(name: str) -> str:
    """The skill's own frontmatter description, or '' when unavailable.

    Author-supplied text, so it is truncated here and escaped by the client.
    api.routes is imported inside the function on purpose: routes imports
    governance_api, which reaches this module.
    """
    key = str(name or "").strip().strip("/")
    if not key or ".." in key.split("/") or "\\" in key or "\x00" in key:
        return ""
    try:
        from api import routes as api_routes
        from tools.skills_tool import _parse_frontmatter

        skills_dir = api_routes._active_skills_dir()
        skill_md = skills_dir / key / "SKILL.md"
        skill_md.resolve().relative_to(skills_dir.resolve())
        # Same bounded read as api/routes.py's skill index: a frontmatter block
        # never needs more, and the queue composes one of these per row.
        frontmatter, _body = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
        text = " ".join(str(frontmatter.get("description") or "").split())
        return text[:_SKILL_DESCRIPTION_MAX]
    except Exception:
        return ""


def _route_permissions(path: str) -> list:
    """Every permission that can still apply to a route, read and write both.

    The denial spool records no HTTP method (api/grant_requests.py:132-136), so
    picking one of the two would misreport half the rows. Both are listed and
    the copy calls them "still required", never "granted".
    """
    try:
        from api.governance.catalog import route_permission

        return _dedup([route_permission(path, "GET"), route_permission(path, "POST")])
    except Exception:
        return []


def _policy_target(gkind: str, value: str) -> list:
    """The policy paths approving this request writes, under the user entry.

    Derived from api.grant_requests._GRANT_TARGETS and from what
    apply_grant_to_policy actually does: a skill grant lands on two lists, and
    an MCP grant also opens every tool on that server.
    """
    try:
        from api.grant_requests import _GRANT_TARGETS

        target = _GRANT_TARGETS.get(gkind)
    except Exception:
        target = None
    if not target:
        return []
    section, subkeys = target
    paths = [f"grants.{section}.{sub}" for sub in subkeys] or [f"grants.{section}"]
    if gkind == "mcp" and value:
        paths.append(f"grants.{section}.tools.{value} (every tool on that server)")
    return paths


def _mcp_server_of(tool_name: str) -> str:
    """The server segment of an mcp__<server>__<tool> name, or ''."""
    parts = str(tool_name or "").split("__")
    if len(parts) >= 3 and parts[0] == "mcp" and parts[1]:
        return parts[1]
    return ""


def _apply_base(out: dict, base: dict) -> None:
    out["capability"] = str(base.get("capability") or "")
    out["data"] = str(base.get("data") or "")
    out["risks"] = _dedup(base.get("risks") or ())
    out["mitigations"] = _dedup([base.get("mitigation") or ""])
    out["alternatives"] = _dedup([base.get("narrower") or ""])
    out["dependencies"] = _dedup(base.get("depends_on") or ())


def _explain_grant(entry: dict, payload: dict, skill_detail: bool) -> dict:
    gkind = str(payload.get("gkind") or "").strip().lower()
    value = str(payload.get("value") or "").strip()
    base = GKIND_RISKS.get(gkind) or KIND_RISKS["grant"]
    out = _blank()
    _apply_base(out, base)
    # apply_grant_to_policy writes under users[email].grants, so a grant is
    # always one person wide: never a role, never a group.
    out["scope"] = "user"
    out["scope_text"] = SCOPE_TEXTS["user"]
    out["duration"] = DURATION_UNTIL_REVOKED
    out["policy_target"] = _policy_target(gkind, value)
    out["source"] = "grant_targets" if gkind in GKIND_RISKS else "request_kinds"

    if gkind == "route":
        permissions = _route_permissions(value)
        out["permissions"] = permissions
        out["permission_notes"] = [
            f"{name}: {PERMISSION_RISKS[name]['capability']}"
            for name in permissions
            if name in PERMISSION_RISKS
        ]
        # The area's own risks are shown because the address becomes reachable,
        # but the capability sentence above states that the permission still
        # applies, so this is context and not a claim about the grant.
        out["risks"] = _dedup(
            list(out["risks"])
            + [r for name in permissions for r in PERMISSION_RISKS.get(name, {}).get("risks", ())]
        )
        out["alternatives"] = _dedup(
            list(out["alternatives"])
            + [PERMISSION_RISKS.get(name, {}).get("narrower", "") for name in permissions]
        )
        if permissions:
            out["source"] = "catalog"
    elif gkind == "toolset":
        toolset = TOOLSET_RISKS.get(value)
        if toolset:
            out["capability"] = f"{out['capability']} The group covers: {toolset['capability']}"
            out["data"] = str(toolset.get("data") or out["data"])
            out["risks"] = _dedup(list(out["risks"]) + list(toolset.get("risks") or ()))
            out["tools"] = [value]
            out["source"] = "toolsets"
    elif gkind == "tool":
        out["tools"] = [value] if value else []
        server = _mcp_server_of(value)
        if server:
            # A tool grant writes tools.builtins, not mcp.servers: the server
            # is named as context only, never swapped in as the grant.
            out["external_systems"] = [server]
            out["capability"] = (
                f"{out['capability']} It is a tool of the connected service "
                f"named '{server}'."
            )
    elif gkind in {"skill", "skill_manage"}:
        description = _skill_description(value) if skill_detail else ""
        if description:
            out["capability"] = f"{out['capability']} The skill describes itself as: {description}"
            out["source"] = "skill_frontmatter"
    elif gkind == "mcp":
        out["external_systems"] = [value] if value else []
    elif gkind == "permission":
        # Approving writes the permission itself, so the sentence for it is the
        # capability, not context: the "still required" framing used for a route
        # would be wrong here.
        described = PERMISSION_RISKS.get(value)
        if described:
            out["capability"] = f"{out['capability']} It allows: {described['capability']}"
            out["data"] = str(described.get("data") or out["data"])
            out["risks"] = _dedup(list(out["risks"]) + list(described.get("risks") or ()))
            out["alternatives"] = _dedup(list(out["alternatives"]) + [described.get("narrower") or ""])
            out["dependencies"] = _dedup(list(out["dependencies"]) + list(described.get("depends_on") or ()))
            out["permissions"] = [value]
            out["source"] = "catalog"
    return out


def _explain_registry(entry: dict, payload: dict, kind: str, base: dict, skill_detail: bool) -> dict:
    out = _blank()
    _apply_base(out, base)
    key = str(entry.get("key") or "").strip()
    try:
        from api import approvals

        scope = "global" if kind == approvals.KIND_SKILL else approvals.approval_scope(entry)
    except Exception:
        scope = "global"
    out["scope"] = scope
    out["scope_text"] = SCOPE_TEXTS.get(scope, SCOPE_TEXTS["global"])
    out["duration"] = DURATION_UNTIL_REVOKED
    out["source"] = "request_kinds"
    if kind == "skill":
        description = _skill_description(key) if skill_detail else ""
        if description:
            out["capability"] = f"{out['capability']} The skill describes itself as: {description}"
            out["source"] = "skill_frontmatter"
    elif kind == "mcp":
        out["external_systems"] = _dedup([payload.get("url"), key])
    elif kind == "integration":
        out["external_systems"] = _dedup([key])
    return out


def explain_entry(entry, *, skill_detail: bool = True) -> dict:
    """Compose the plain-English explanation for one approvals registry row.

    ``skill_detail`` carries the caller's skills:read entitlement. A skill's
    own SKILL.md description is skill content, protected by skills:read
    everywhere else in the app, so a reviewer without that permission gets the
    kind-level sentence and no quoted text: explaining an approval must not
    become a way around the permission that guards what it explains.

    Never raises: an unrecognisable row returns {} and the queue renders
    without a detail block rather than failing.
    """
    try:
        if not isinstance(entry, dict):
            return {}
        kind = str(entry.get("kind") or "").strip().lower()
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if kind == "grant":
            return _explain_grant(entry, payload, bool(skill_detail))
        base = KIND_RISKS.get(kind)
        if base is None:
            return {}
        return _explain_registry(entry, payload, kind, base, bool(skill_detail))
    except Exception as exc:  # pragma: no cover: the queue must render regardless
        logger.debug("approval explanation failed: %s", exc)
        return {}


def risk_digest(explanation) -> dict:
    """What the approver was shown, small enough to store on the audit row.

    Keys are chosen to miss api/governance/audit.py's redaction pattern, so the
    digest survives into the trail instead of landing there as [REDACTED].
    """
    if not isinstance(explanation, dict) or not explanation:
        return {}
    return {
        "risks": list(explanation.get("risks") or []),
        "scope": str(explanation.get("scope") or ""),
        "permissions": list(explanation.get("permissions") or []),
        "policy_target": list(explanation.get("policy_target") or []),
    }
