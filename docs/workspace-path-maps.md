# Workspace path maps (multi-container profiles)

How a Hermes profile declares that one physical workspace has two names: the
path the **agent** sees and the path the **WebUI process** sees.

---

## 1. The problem

In a multi-container deployment the agent and the WebUI are two processes that
share a directory but mount it at different paths:

| Side | Path |
|---|---|
| agent container | `/workspace` |
| WebUI container | `/workspaces/alice` |

`session.workspace` stores the **agent-visible** name, because that is what the
gateway has to receive verbatim. But the WebUI process reads the filesystem
itself — for the file manager, and for the local Git controls (status, diffs,
commit messages). For those local reads it needs the **WebUI-local** name.

`webui.workspace_path_maps` is the translation table between the two.

## 2. Declaring the map

The key lives in the **profile's own** `config.yaml`
(`<profile_home>/config.yaml`):

```yaml
webui:
  workspace_path_maps:
    - agent: /workspace          # the root the agent container sees
      webui: /workspaces/alice   # the same physical directory, WebUI side
    - agent: /srv/data
      webui: /data
```

Each entry has two keys:

- `agent` — the POSIX path prefix the agent sees. Sub-paths translate with
  their relative tail (`/workspace/src/app.py` →
  `/workspaces/alice/src/app.py`).
- `webui` — the real host path of the same directory.

Entries are matched **longest agent prefix first**, so overlapping entries
resolve deterministically.

The map is read from the profile's own config **only**. It is never derived
from a sibling profile and never from a process-global allow-root set, so a
profile can only ever name roots it declared itself.

## 3. What the map does and does not change

- A profile with **no** declared map keeps the pre-existing direct-path
  contract byte-for-byte. Single-container deployments are unaffected.
- A profile **with** a map translates the agent root for local reads, and then
  **re-validates** the translated root through the ordinary trust rules (see
  below). A map entry can never widen trust beyond what the profile already
  allows.
- A mapped profile that names a path **outside** every mapped prefix is *not*
  passed through just because the WebUI process happens to be able to see it:
  it is still resolved through the trust rules, and it fails closed when it is
  not trusted.
- `session.workspace` itself is **never rewritten**. The gateway keeps
  receiving the agent-visible path; only local file and Git operations use the
  translated root.
- Remote terminal profiles (SSH / Docker backends) bypass translation
  entirely — a target-side path has no WebUI-local form.

## 4. Trust rules that still apply

A translated path is not trusted because it was declared in the map. The
translated root has to pass the same checks as any other workspace:

- it must exist and be a directory;
- it must not be a system root (`/etc`, `/usr`, `/var`, `/bin`, `/sbin`, `/boot`,
  `/proc`, `/sys`, `/dev`, `/root`);
- it must be under the user's home directory, or already in the profile's saved
  workspace list, or under the boot-time default workspace.

A `webui` value of `/` or of a blocked system root is rejected when the map is
read, so such an entry is simply not part of the table.

Because the trust rules still run, a **CLI session** (a session that only
exists in the state database, e.g. one started from a terminal) whose workspace
is outside the home and no longer in the saved workspace list is refused: the
Git badge shows no repository rather than inspecting a directory the operator
has removed.

## 5. Where it applies

The map is consulted by the local, host-side readers:

- the file-manager endpoints (list, read, write, upload);
- the workspace Git endpoints — status, branches, diffs, and the
  commit-message suggestions (both the staged and the selected-files variant).

Server-side execution is unaffected: the agent keeps working in the
agent-visible path.

## 6. Diagnostics

If a local view looks empty for a mapped profile:

1. Check that the map is in the **profile's own** `config.yaml` under
   `webui.workspace_path_maps`, and that the profile is the one the session
   belongs to.
2. Check that the `webui` path exists **on the WebUI host** and is under the
   home, in the saved workspace list, or under the default workspace.
3. Check that the session's workspace starts with one of the `agent` prefixes.

`docs/workspace-git.md` describes what the Git controls can read and write,
and the trust model of the destructive actions.
