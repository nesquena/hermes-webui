## Summary

The workspace file browser (right panel, "Files" tab) shows a directory tree with expand/collapse. To find a specific string across files — a function name, error message, or config value — the user must drill through every folder manually or drop to a terminal and run `rg`/`grep`. Neither keeps the user inside the WebUI workspace flow.

Add a ripgrep-backed search bar to the workspace file panel that searches both file **names** and file **contents** across the entire workspace. Results display inline above the file tree. Both the search section and the file tree section have independent collapse/expand toggles so the user can reclaim vertical space when not searching.

> **Architecture diagram:** https://excalidraw.com/#json=FCIJsnCqXE1ooOz8EfcRl,-Z_xycdIYPciVXHLGtO6ng

## Current state

**Workspace file browser** — directory tree only, no search:

| Aspect | Current |
|---|---|
| File listing | `GET /api/list?session_id=...&path=.` — `list_dir()` in `api/workspace.py:1234` |
| File content preview | `GET /api/read` — `read_file_content()` in `api/workspace.py:1458` |
| File name search | None |
| File content search | None |
| Frontend tree render | `renderFileTree()` in `static/ui.js:19213`; `_renderTreeItems()` in `static/ui.js:19402` |
| Workspace panel HTML | `static/index.html:1659-1711` (`.rightpanel`, tabs, `.breadcrumb-bar`, `#fileTree`) |

## Proposed layout

The file tree is **always visible** beneath the search section. Activating search does not replace or hide the tree — results render in a collapsible row inside the search section. Either section can be collapsed independently:

```
┌─────────────────────────┐
│ Files  │  Artifacts     │ ← tabs
├─────────────────────────┤
│ 🔍 Search section  [▼] │ ← collapsible header; ▼ expanded / ▶ collapsed
│ ┌─────────────────────┐ │
│ │ 🔍 Search files...  │ │ ← input (debounced 300ms)
│ │ 📄 3 results        │ │ ← inline result rows
│ └─────────────────────┘ │
├─ — — — — — — — — — — — ┤
│ 📁 FILE TREE      [▼]  │ ← collapsible header; independent from search
│   📄 README.md          │
│   📂 src/               │
│   📂 tests/             │
│   📄 pyproject.toml     │
│   📄 Dockerfile         │
└─────────────────────────┘
```

**Collapse semantics:**
- **Both expanded** — default; search on top, tree below
- **Search collapsed** — tree fills the panel (same as today's file-tree-only view)
- **Tree collapsed** — search still works; results visible in search section
- **Both collapsed** — just the tabs; minimal chrome

The collapse state is persisted in `localStorage` so it survives page reloads.

## Why ripgrep

| Approach | Drawback |
|---|---|
| `os.walk()` + `str.find()` | Slow on large workspaces; reimplements what ripgrep already does at 5-10x speed |
| `subprocess grep` | No `.gitignore` awareness without extra plumbing |
| Python indexer with background DB | Adds maintenance burden, index staleness, build-step complexity |
| **Ripgrep (`rg`)** | Already on the system (`rg` v11.0.2), respects `.gitignore` by default, JSON output mode, <100ms on repos up to 100k+ files, zero build-step dependency |

Ripgrep matches the project's "no build step, Python + vanilla JS" philosophy.

## Proposed backend

### New endpoint: `GET /api/workspace/search`

| Param | Type | Default | Description |
|---|---|---|---|
| `session_id` | string | required | Workspace to search |
| `q` | string | required | Search query |
| `kind` | `content` \| `filename` | `content` | Search file contents or file names |
| `hidden` | bool | `false` | Include hidden files / dot-directories |
| `max_results` | int | `50` | Max results (capped at `200`) |

**Content search** runs `rg --json -i -n --max-count <max> -- <query> <workspace_path>`. Returns:

```json
{
  "results": [
    {
      "path": "src/main.py",
      "line_number": 42,
      "line_content": "def parse_config(path):",
      "match_column": 4
    }
  ],
  "total": 3,
  "timed_out": false,
  "kind": "content"
}
```

**Filename search** runs `rg --json -i --files <workspace_path>` and filters in Python.

Respects `.gitignore` by default. Scoped to workspace via `safe_resolve_ws` (same as `list_dir` and `read_file_content`). 10-second subprocess timeout. Binary files are auto-skipped by ripgrep.

**Backend placement:** New functions in `api/workspace.py` (adjacent to `list_dir` / `read_file_content`), wired in `api/routes.py` as a new `if parsed.path == "/api/workspace/search"` branch alongside the existing `/api/list` at `routes.py:13082`.

### Security

No new threat surface. The workspace path is resolved through `safe_resolve_ws` (anchored openat guard, same as `list_dir`). Ripgrep runs within that boundary.

## Proposed frontend

### HTML (`static/index.html`)

A search bar element between the `.workspace-panel-tabs` container and the `.breadcrumb-bar`, consisting of:

- A header row with "🔍 Search" label and a collapse toggle (▼ / ▶)
- The search input (`<input type="search" id="workspaceSearchInput">`)
- A results container (`<div id="wsSearchResults" class="ws-search-results">`)

### JavaScript (`static/workspace.js`)

| State variable | Purpose |
|---|---|
| `_wsSearchCollapsed` | Collapse toggle state (saved to localStorage key `hermes-webui-search-collapsed`) |
| `_wsSearchQuery` | Current query string |
| `_wsSearchResults` | Array of result objects from API |
| `_wsSearchActive` | Boolean, true when results are being displayed |

**Key functions:**

- `_toggleWsSearchSection()` — toggles `data-collapsed` on search bar element, saves to localStorage
- `_handleWsSearchInput()` — 300ms debounced, calls `api('/api/workspace/search?session_id=...&q=...&kind=content')`, renders results
- `_renderSearchResults(results)` — builds result rows (file path + snippet + line number) inside the results container
- `_clearWsSearch()` — clears query and results, restores tree state

**Clicking a result** calls `openFile(result.path, {line: result.line_number})` which opens the file in the preview pane scrolled to the matching line.

### CSS (`static/style.css`)

Minimal, no new dependencies:

```css
.workspace-search-bar { border-bottom: 1px solid var(--border2); }
.workspace-search-bar[data-collapsed="true"] .ws-search-body { display: none; }
.ws-search-header { display: flex; align-items: center; cursor: pointer; padding: 4px 8px; }
.ws-search-header .collapse-icon { margin-left: auto; }
.ws-search-input { width: 100%; border: none; background: transparent; padding: 6px 8px; }
.ws-search-results { max-height: 200px; overflow-y: auto; }
.ws-search-result-row { cursor: pointer; padding: 3px 8px; font-size: 12px; }
.ws-search-result-row:hover { background: var(--hover-bg); }
```

### States

| State | Behavior |
|---|---|
| Empty query | No network call; tree visible; results container empty |
| Typing | 300ms debounce; "Searching…" indicator |
| Results | Inline list below input; tree still visible beneath |
| No results | "No results for '<q>'" inline message |
| Error (rg missing, timeout) | Inline error message, fallback suggestion |
| Truncation | "Showing first 200 of N results" footer |
| Search collapsed | Header row only; body + results hidden |
| Tree collapsed | Search still works; results visible |
| Both collapsed | Minimal chrome; tabs only |

## Pitfalls / edge cases (already handled by design)

| Concern | Mitigation |
|---|---|
| Large result sets | Capped at `max_results=200`, truncation footer shown |
| Binary files | Ripgrep skips them by default |
| Hidden files / `.gitignore` | Respected by default; `hidden=true` flag enables `--no-ignore --hidden` |
| UTF-8 / BOM | Ripgrep handles natively; response uses UTF-8 with replacement chars |
| `rg` not installed | Fall back to `os.walk()` + `fnmatch` for filename-only search; flash message suggesting `apt install ripgrep` |
| Path traversal | Same `safe_resolve_ws` guard as `list_dir` / `read_file_content` |
| Timeout on huge workspace | 10-second subprocess timeout guards against pathological cases |
| Collapse state lost on reload | Persisted to `localStorage` key `hermes-webui-search-collapsed` |

## Files to modify

| File | What changes |
|---|---|
| `api/workspace.py` | Add `search_workspace_content()`, `search_workspace_filenames()` |
| `api/routes.py` | Add `/api/workspace/search` route handler; import new search functions |
| `static/index.html` | Add search bar element + results container after tabs |
| `static/workspace.js` | Add search state, debounced handler, collapse toggle, result render, click-to-preview |
| `static/style.css` | `.workspace-search-bar`, `.ws-search-*` classes — minimal |

## Out of scope (for this request)

- Live/streaming search (single-shot is faster to implement and sufficient)
- Saved searches or search history (adds persistent state complexity)
- Client-side indexing (Fuse.js, lunr — adds a build dependency)
- Multi-workspace search (scope is the current session's workspace)
- Replace tree on search (tree stays visible beneath results; both sections independently collapsible)

## AI Assistance

This issue was drafted with structural codebase analysis via `graphify` query and `search_files` over the `nesquena/hermes-webui` repo. An accompanying RFC with full implementation sketch is at `docs/rfcs/ripgrep-workspace-search.md`.
