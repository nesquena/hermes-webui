# Session-owned artifact projection

Artifact evidence outside the loaded transcript is derived from an authoritative
session snapshot and owned by the current session object, not accumulated in a
bare-session-id page cache. Profile, regeneration revision, and load generation
must match before the projection is rendered. A cold bounded transcript load
requests full history for this derivation; replacement history rebuilds it and
failed enrichment leaves the transcript usable without stale derived evidence.
At stream recovery, the bounded transcript settles and the pane becomes idle
before the full-history artifact request starts. The eventual result updates only
the still-current snapshot, so slow or failed enrichment cannot hold queued input
or install stale artifacts after a pane switch.

The artifact browser harness covers cold bounded loading, done/error/cancel,
owner mismatch rejection, and history replacement. Completion and terminal
harnesses page repeatedly with a progress assertion instead of assuming one
30-row older-history request reveals all 3,302 rows.

Limitations: full-history enrichment costs an additional request and can reach
the server response ceiling. A capped response is rejected, not mislabeled as
complete. A dedicated server-owned artifact projection and real profile-switch
race coverage remain follow-up work; this is not an unbounded-history guarantee.
