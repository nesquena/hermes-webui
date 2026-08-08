# RFC: Ripgrep-Backed Workspace File Search

**Status:** Proposal  
**Target repo:** `nesquena/hermes-webui`  
**Author:** Community

---

> **Architecture diagram:** https://excalidraw.com/#json=FCIJsnCqXE1ooOz8EfcRl,-Z_xycdIYPciVXHLGtO6ng

## Problem

The workspace file browser (right panel, "Files" tab) shows a directory tree with expand/collapse. To find a specific string across files — a function name, error message, or config value — the user must either:

1. Drill through every folder manually, or
2. Switch to a terminal and run `rg` or `grep` outside the UI.

Neither is fast, and neither keeps the user inside the Hermes WebUI workspace flow. As workspace sizes grow, the lack of file content search becomes a daily friction point.

## Proposal

Add a ripgrep-backed search bar to the workspace file browser that searches both **file names** and **file contents** across the entire workspace. Results display inline above the file tree. Both the search section and the file tree section have independent collapse/expand toggles so the user can reclaim vertical space when not searching.

## Layout

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
- **Both expanded** — default state; search on top, tree below
- **Search collapsed** — tree fills the panel (same as today's file-tree-only view)
- **Tree collapsed** — search still works; results inline in search section
- **Both collapsed** — just the tabs showing; minimal chrome

## Why ripgrep (over other approaches)

| Approach | Drawback |
|----------|----------|
| `os.walk()` + `str.find()` | Slow on large workspaces; reimplements what ripgrep already does at 5-10x the speed |
| `subprocess grep` | No ignorefile awareness (`.gitignore`, `.rgignore`) without extra plumbing |
| Python-only indexer | Adds maintenance burden, DB storage, staleness |
| **`subprocess ripgrep`** | Already installed on the system (`rg` v11.0.2+), respects `.gitignore` out of the box, JSON output mode for easy parsing, fast enough for interactive use on repos up to 100k+ files |

Ripgrep is the pragmatic zero-build-step choice that matches the project's "no build step, Python + vanilla JS" philosophy.

## Design

### Backend: New API endpoint

**`GET /api/workspace/search?session_id=<id>&q=<query>[&kind=content|filename][&hidden=false][&max_results=N]`**

- `kind=content` (default): search file contents via `rg --json -i -n --max-count <max> -- <query> <workspace_path>`
- `kind=filename`: search file names via `rg --json -i --files <workspace_path> | filter in Python`
- `hidden=false`: respects `.gitignore` and hidden files by default; `true` passes `--no-ignore --hidden`
- `max_results=50` (capped at 200)

Returns JSON:

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

**Security:** Scoped to the workspace path via the existing `safe_resolve_ws` guard and `open_anchored_fd` pattern (same as `list_dir` and `read_file_content`). 10-second subprocess timeout. Binary files auto-skipped by ripgrep.

**Backend placement:** New functions in `api/workspace.py` (adjacent to `list_dir` / `read_file_content`), wired in `api/routes.py` as a new `if parsed.path == "/api/workspace/search"` branch alongside the existing `/api/list`.

### Frontend: Collapsible search section

**HTML** (`static/index.html:1683`): A new `.workspace-search-bar` element between the `.workspace-panel-tabs` container and the `.breadcrumb-bar`. It contains:

- A header row with "🔍 Search" label and a collapse toggle (▼ / ▶)
- The search input (`<input type="search">`)
- An optional results container (`<div class="ws-search-results">`)

The collapse state is stored as a CSS class on the search bar element (`data-collapsed="true|false"`) and persisted in `localStorage` so it survives page reloads.

**JavaScript** (`static/workspace.js`):

- `_wsSearchCollapsed`, `_wsSearchQuery`, `_wsSearchResults`, `_wsSearchActive` state
- `_toggleWsSearchSection()` — toggles `data-collapsed` on the search bar element, saves to `localStorage`
- `_handleWsSearchInput()` — 300ms debounced, calls `api('/api/workspace/search?session_id=...&q=...')`, renders results into `ws-search-results`
- `_renderSearchResults()` — builds result rows (file path + snippet + line number)
- Click handler — `openFile(result.path, {line: result.line_number})` opens in preview pane scrolled to match
- Esc/clear — resets query, clears results, input remains visible

**CSS** (`static/style.css`):

```css
.workspace-search-bar { border-bottom: 1px solid var(--border2); }
.workspace-search-bar[data-collapsed="true"] .ws-search-body { display: none; }
.ws-search-header { display: flex; align-items: center; cursor: pointer; }
.ws-search-header .collapse-icon { /* ▼ / ▶ toggle */ }
.ws-search-input { width: 100%; border: none; background: transparent; }
.ws-search-results { max-height: 200px; overflow-y: auto; }
.ws-search-result-row { cursor: pointer; /* match .file-tree-item style */ }
```

No build step, no bundler, no new npm dependencies.

### States to handle

| State | Behavior |
|---|---|
| Empty query | No network call; tree visible; results container empty |
| Typing | 300ms debounce; "Searching…" indicator |
| Results | Inline list below input; tree still visible below |
| No results | "No results for '<q>'" inline message |
| Error (rg missing, timeout) | Inline error message with fallback suggestion |
| Truncation | "Showing first 200 of N results" footer |
| Search collapsed | Header row only; body + results hidden |
| Tree collapsed | Search still works; results visible |

## File changes

| File | What changes |
|---|---|
| `api/workspace.py` | Add `search_workspace_content()`, `search_workspace_filenames()` |
| `api/routes.py` | Add `/api/workspace/search` route handler; import new functions |
| `static/index.html` | Add search bar element + results container after tabs |
| `static/workspace.js` | Add search state, debounced handler, collapse toggle, result render, click-to-preview |
| `static/style.css` | `.workspace-search-bar`, `.ws-search-*` classes — minimal |

## Pitfalls

- **Large result sets:** Capped at 200; truncation footer shown
- **Binary files:** Skipped by `rg` by default — no action needed
- **Hidden / gitignored:** Respected by default; `hidden=true` flag overrides
- **UTF-8 / BOM:** Handled natively by ripgrep; decoded as UTF-8 with replacement chars
- **`rg` not installed:** Fall back to `os.walk()` + `fnmatch` for filename-only; flash message suggesting `apt install ripgrep`
- **Collapse state persistence:** On first load both sections default to expanded; state saved to `localStorage` per session

## Alternative Approaches Considered

**Separate "Search" tab:** Rejected — inline avoids tab proliferation (there are already three: Files / Artifacts / Todos). Collapsing the search section gives the same visual result as switching tabs.

**Replace tree with results:** Rejected per user feedback — the tree must remain visible beneath the search results so the user always has the directory structure as a fallback navigation surface. Both sections are independently collapsible so the user controls the split.

**Client-side search (Fuse.js, lunr):** Rejected — requires shipping a search index to the browser, which adds a build-step dependency and doesn't scale beyond a few hundred files.

**Python watchdog indexer (background process):** Rejected — overkill for an interactive search box; index staleness and file-watch complexity don't justify the benefit over a 10ms ripgrep call.

## Out of Scope

- Live/streaming search (single-shot is simpler and fast enough)
- Saved searches or search history (persistent state complexity)
- Multi-workspace search (scope is the current session's workspace)
- Index-on-write caching (ripgrep is fast enough without it)
