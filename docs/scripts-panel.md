# Scripts subtab

The **Tasks** panel has two subtabs: **Jobs** (scheduled cron jobs) and
**Scripts** (a read-only browser over the active profile's
`~/.hermes/scripts/` directory). The Jobs subtab is the default, so existing
users see no behavioural change until they click Scripts.

The slice is read-only by design: editing a script from the WebUI is out of
scope. The panel tells you which scripts exist, how big they are, and what
they claim to do; to change one, open the file on the server.

## Where the scripts come from

The directory is the **active profile's** `scripts/` folder, so switching
profiles switches the listing. It is resolved per request, not cached:

```
<active profile home>/scripts/
```

Only `.py` and `.sh` files are listed. Dotfiles are included (`.hidden.py`
is a script like any other), and the name guard is not a filename policy —
`my job.py` and `résumé.sh` are both listed and readable. Orphan scripts that
no cron job references are listed too; filtering them is follow-up work.

The description comes from the file's own leading text: for a `.py` file the
module docstring, for a `.sh` file the leading `#` comment block. Either way
the list shows the first non-empty line (up to 280 characters), so a
multi-line docstring still reads as a one-line summary in the panel.

## What the panel shows

Each row carries the script name and the description extracted from its
leading text. The panel is a listing: it has no viewer, and scripts are edited
on the server. `/api/scripts/list` also returns each script's size and
last-modified time, and `/api/scripts/raw` serves a script's content, for
clients that want them.

Hardlinked files (link count above one) are not listed or served, the same
rule as the wiki browser. The check runs again after the read, so a file whose
scripts entry changes while it is being read is refused too. The listing
reports how many were skipped in `skipped_hardlinked`. Hermes never hardlinks
scripts itself; a folder copied with `cp -al` is the usual way to get them.

A listing describes at most 1,000 scripts, in name order, and walks at most
100,000 directory entries. A listing cut short by either cap is flagged
`"truncated": true`. Past the entry cap, which scripts appear depends on the
directory's own order.

A profile with no `scripts/` directory and a profile with an empty one are
different states: the panel says which one it is rather than showing an
indistinguishable empty list.

## Endpoints

Two read-only endpoints back the panel.

### `GET /api/scripts/list`

Returns the listing for the active profile:

```json
{
  "scripts": [
    {"name": "backup.sh", "size": 812, "modified": 1759000000, "description": "Nightly backup."}
  ],
  "directory": "/home/user/.hermes/scripts",
  "exists": true,
  "unavailable": "",
  "truncated": false,
  "skipped_hardlinked": 0
}
```

`exists` is `false` when the profile has no `scripts/` directory at all.
Entries are sorted by name, case-insensitively, for a stable order.

### `GET /api/scripts/raw?name=…`

Returns one script's contents:

```json
{"name": "backup.sh", "content": "…", "size": 812, "modified": 1759000000, "truncated": false}
```

| Condition | Response |
| --- | --- |
| `name` is missing | `400` |
| no such script, or the name is not a `.py`/`.sh` file | `404` |
| the file is larger than 256 KiB | `200` with `{too_large: true, size: N}` and no body |
| the read failed | `500` with `{error: "read_failed", detail}` |

A failed read is a `500`, not a `200` with an empty body: the panel keys its
error state off the response status, so a `200` would make an unreadable
script look like an empty one.

A script over the 256 KiB cap is refused rather than streamed. There is no
inline reader for that case yet — read such a file in an editor outside the
WebUI. `size` reports the bytes actually read, so a file that grows while it
is being read cannot report a stale length next to newer text. A read that
hit the cap is flagged `truncated`; a file of exactly the cap is complete.

## Containment

Nothing outside the active profile's `scripts/` directory is ever served.

The name guard (`_is_safe_script_name`) rejects traversal and control
characters only — it is not a filename policy. Containment itself lives in
`_open_script_for_read()`, which opens the leaf **relative to a directory
descriptor** pinned with `O_DIRECTORY`, with `O_NOFOLLOW` on the leaf, and
proves via `fstat` that the opened object is a regular file inside that
directory. A symlink pointing outside the directory, a non-regular leaf
(FIFO, device, directory), a symlinked `scripts/` directory itself, and
permission errors all fail closed.

Listing follows the same rule: a candidate is described only after it is
proven to be a regular file inside the directory, so a symlink to somewhere
else is skipped rather than reported with its target's description.

Two consequences are worth knowing:

- On a platform without `O_NOFOLLOW`/`O_DIRECTORY` the listing is **refused**,
  and the refusal is reported rather than shown as an empty folder (see
  "A third state is a refusal" above).
- A FIFO in the directory does not block the listing: the leaf is opened
  `O_NONBLOCK` so the open returns instead of waiting for a writer, and it is
  then rejected by the regular-file check before anything is read.

A profile with no `scripts/` directory and a profile with an empty one are
different states: the listing carries an `exists` flag, and the panel shows
"No scripts directory yet." for the former and "No scripts in this profile
yet." for the latter.

A third state is a **refusal**: on a platform without `O_NOFOLLOW`/
`O_DIRECTORY` the scripts directory cannot be opened safely, so the listing
cannot be produced at all. That is not the same fact as "this profile has no
scripts", and the panel does not pretend it is — the listing carries an
`unavailable` field with the reason, and the panel shows "Scripts are
unavailable on this platform." instead of an empty state.

## Profile ownership

The panel always shows the active profile's scripts. A profile switch
invalidates any in-flight listing, so a reply that was already on the way
when the switch started cannot repaint another profile's rows, and the new
profile's scripts are loaded after the switch succeeds. If a switch fails,
the panel is restored rather than left blank.

## Where the code lives

| Piece | Location |
| --- | --- |
| Backend | `api/scripts_panel.py` |
| Route registration | `api/routes.py` (`/api/scripts/list`, `/api/scripts/raw`) |
| Panel markup | `static/index.html` (`#panelTasks` subtab strip, `#scriptsList`) |
| Panel behaviour | `static/panels.js` (`loadScriptsList`, `switchTasksSubtab`, `_renderScriptItem`) |
| Profile-switch invalidation | `static/sessions.js` |
| Styling | `static/style.css` (`.tasks-subtab`, `.scripts-list`, `.scripts-item`) |
| Copy | `static/i18n.js` (`cron_tab_*`, `scripts_*` keys, `en` locale) |

The subtab copy lives in the `en` locale only and rides the existing
` t() ` fallback into the other locales — see `docs/GUIDELINES.md` section 8.

## Tests

- `tests/test_issue2316_scripts_panel.py` — the listing and read contracts.
- `tests/test_issue7685_scripts_panel_profile_ownership.py` — profile ownership.
- `tests/test_issue7685_scripts_panel_round2.py` — descriptor accounting, FIFO
  handling, Windows behaviour.
- `tests/test_issue7685_scripts_panel_round3.py` — bounded reads and error
  surfacing.
- `tests/test_issue7685_scripts_panel_round4_opacity_ownership.py` — pane
  ownership during a profile switch.
- `tests/test_issue7685_scripts_panel_round5_ux.py` — the UX review fixes.
- `tests/test_issue7685_scripts_tab_jobs_wording.py` — the Jobs/Scripts
  wording.
- `tests/test_issue7685_scripts_panel_i18n_fallback.py` — the copy stays in
  `en` and every locale still resolves it.

Run them with the repository test interpreter:

```bash
./scripts/test.sh tests/test_issue2316_scripts_panel.py tests/test_issue7685_scripts_panel_profile_ownership.py
```
