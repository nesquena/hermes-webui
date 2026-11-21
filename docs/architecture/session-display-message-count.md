# Session Display Message Count Contract

- **Status:** Implemented
- **Applies to:** `GET /api/session` display coordinate resolution across `messages=0` (metadata), `messages=1&msg_limit=...` (bounded), and `messages=1` (full) response modes.
- **Regression coverage:** `tests/test_issue7050_foreign_session_count_coordinate.py`, `tests/test_display_merge_cache.py`, `tests/test_display_merge_cache_shortcut.py`, `tests/test_lineage_display_cache.py`, `tests/test_webui_lineage_display_merge.py`, `tests/test_session_lineage_full_transcript.py`, `tests/test_session_tail_payload.py`.

This document is the authority for how `GET /api/session` resolves and reports `message_count` and `last_message_at` across response modes.

## One Reconciled Display Coordinate

Historically, `GET /api/session` answered with counts computed in different domains depending on request parameters:
* `messages=0` (the frontend's 30-second external-refresh poll) took a cheap `COUNT(*)` summary from `state.db`'s raw row domain, including archived rows (`active = 0`), replayed transport rows, and tool rows that the display reader folds away;
* `messages=1` returned `len(_all_msgs)`, the merged display transcript.

For foreign/CLI sessions, the raw row domain is significantly larger than the visible transcript rendered by the WebUI. Because the external-refresh poll compared the two counts, it perpetually saw a "change" and triggered full transcript reloads every 30 seconds.

Under this contract:

1. **Foreign/CLI sessions answer in merged display coordinates:**
   For foreign/CLI sessions (`_session_display_count_is_foreign`), `messages=0` metadata requests run through `_foreign_session_display_message_summary()`. This executes the exact same sidecar + immediate-parent + `state.db` reconciliation that a message load runs (`_merged_sidecar_state_messages_for_display()`), ensuring the poll count equals the loaded count by construction.

2. **Accepted trade for metadata reconciliation:**
   Reconciling foreign/CLI metadata costs a bounded transcript merge on the metadata path (and reading the immediate parent sidecar when lineage is present). This bounded CPU cost is the accepted trade to eliminate the catastrophic 30-second reload loop. The metadata response still ships no transcript (`messages == []` and `_messages_truncated is False`), keeping network transfer minimal.

3. **Native WebUI sessions retain the cheap growth path:**
   Native WebUI sessions retain the fast, cheap `_metadata_only_message_summary()` raw growth signal because their sidecar count already matches their display count. They are not subject to the foreign transcript discrepancy and avoid paying for a merge on periodic polls.

## Lineage Reconciliation Rules

Display transcripts and counts inherit strict lineage rules:

- **Stitched snapshot ancestors:**
  Parents marked `pre_compression_snapshot` are stitched multi-hop into the sidecar lineage via `_webui_sidecar_lineage_messages_for_display()`.
- **Immediate parent display reconciliation:**
  An immediate parent continuation identified by `parent_session_id` is resolved for display via `_webui_lineage_parent_session_for_display()`. If the immediate parent contains parent-only rows not already visible as a prefix in the child sidecar, those rows are reconciled into the transcript via `_merged_webui_lineage_messages_for_display()` across all three modes: metadata, bounded, and full. Furthermore, native-image payload projection (`_project_native_image_payload_conflicts_for_display`) is fed the same parent-reconciled sidecar across all modes.
- **Isolated lineages:**
  Explicit forks (`session_source: fork` unless parent is also a fork) and child sessions (`relationship_type: child_session`), as well as active compression-recovery sessions, stay isolated from parent reconciliation. They intentionally start from a subset of their parent history.

## Memoized Bounded Merge and Provenance Invalidation

Bounded message loads (`messages=1&msg_limit=...`) memoize the reconciled transcript in `_display_merge_cache`.

To prevent serving stale counts or transcripts when parent sessions grow:
- The validity key (`_display_merge_cache_key`) covers not only the child sidecar's stat signature and `state.db` fingerprint, but also the stat signature of every lineage parent involved.
- For snapshot lineages, parent signatures are supplied from `_lineage_display_cache`.
- For non-snapshot immediate display parents that contribute rows, the parent sidecar's stat signature (`_sidecar_stat_signature`) is explicitly recorded in `parent_sigs`.
- If a parent sidecar cannot be resolved or stat'd, or if lineage provenance is required but missing (`_display_merge_requires_lineage_provenance`), the cache fails closed and refuses to cache (`cache_key is None`).
- If any parent sidecar grows or is modified on disk, its stat signature changes, immediately invalidating the cache entry on subsequent bounded reads and forcing fresh reconciliation with the updated parent rows.
