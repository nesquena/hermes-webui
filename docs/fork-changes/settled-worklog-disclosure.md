# Preserve settled worklog choices

Classification: upstream-candidate; owner: fork maintainers; not filed upstream.
Base: 16720a769d0c5ae3e873cce621d2b20fc3283dd7. Reconstruct before contribution.

Compact settled anchor scenes store intent under session-scoped stream identity,
while the existing index-based worklog key still locates the current message.
Explicit open/closed survives rerender and older-history loading. Saved closed
beats expanded-by-default and error defaults; the transient forced-open settlement
frame still wins, then in-place collapse restores intent. Cached deferred rows
resolve through the lookup key, falling back to legacy disclosure keys.

Run `tests/browser_worklog_disclosure.py` in Chromium/WebKit: live intent, settled
rerender, history shift, saved closed/default precedence, forced settlement and
in-place collapse, cloned HTML row recovery, and session isolation. Base fails;
fixed code passes. Neighboring error-visibility, live-to-final, lazy-worklog and
cleanup tests retain their behavioral contracts with updated key assertions.

Old index-keyed choices are not migrated because a shifted index may belong to
another turn. Live intent seeds settled state. No-stream scenes retain legacy
index fallback and cannot promise preservation across index changes. Multiple
scenes in one stream share outer disclosure intent. Nested-detail capture keys
also include these attributes; existing captured maps are not migrated, and
nested-detail preservation across index shifts is outside this outer-group fix.

No new subsystem, settings or storage format. Revert this commit to roll back;
old index choices may reappear. No physical iOS scroll or battery claim. Retire
when upstream provides equivalent identity/precedence and deferred-row recovery.
