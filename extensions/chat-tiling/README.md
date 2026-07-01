# Chat Tiling

Multi-session tiling extension for Hermes WebUI. Opens session conversations in a resizable grid — work on 2, 4, or 6 sessions simultaneously.

## Usage

Three layout buttons appear in a floating toolbar (bottom-right of the chat area):

- **||** — Split in 2 (horizontal, 2 columns)
- **⊞** — Split in 4 (corners, 2×2 grid)
- **⊟** — Split in 6 (3 columns × 2 rows)

Click any layout button to create the grid with empty tiles. Click a session from the sidebar to fill the focused tile.

- **Click a tile header** to focus it (composer switches to that session)
- **Close** (×) to remove a tile — streaming gets cancelled gracefully
- **Maximize** (↗) to make one tile full-screen in the grid
- Grid closes when all tiles are removed

## Settings

In Settings → Extensions → Chat Tiling:
- Default layout — which layout opens when clicking the toolbar button
- Auto-tile on session click — automatically open sessions in tiles when grid is visible
- Show tile count badges in sidebar
