# Run-journal `after_seq` offset index

`read_run_events(..., after_seq=N)` can avoid reparsing an entire run journal for
ordinary client replay and inspection. This is a derived, process-local index;
the JSONL journal and its archived copy remain authoritative.

## When the index is used

The index applies only to a live `.jsonl` file with no archive present. It stores
one entry every 256 complete rows, including the entry's maximum sequence,
absolute byte offset, line number, and SHA-256 digest of the byte segment since
the previous checkpoint. A bounded LRU retains at most 64 paths. Malformed-row
text is retained only up to 1 MiB per path; the current response remains complete
when that budget is exceeded, but the path is not cached.

Authoritative recovery (`validated_recovery=True`) uses the existing locking and
full-validation path, not this index. Archived/gzip data, CR bytes, invalid
sequence values, and cases that cannot guarantee equivalence with the ordinary
full reader use that full reader instead.

## Append and rollback lifecycle

Ordinary append-only growth extends an existing checkpoint. Before reusing a
checkpoint, the reader re-hashes only its bounded source segment and compares it
with the stored digest. This verifies actual content, not merely a matching file
size or newline boundary.

A failed append can expose a complete row before its eager `fsync` fails and the
producer truncates that uncommitted suffix. A replay during that window may have
indexed the transient row. Later committed writes can regrow the same inode to
the same or a larger size. Size-only validation could then skip committed rows.
The segment digest detects that mismatch and falls back to the complete reader,
including when timestamp resolution cannot distinguish the writes.

Checkpoint validation and suffix replay use one outer read from the segment
start through EOF. The digest, checkpoint-ending newline, and returned suffix
all come from that same byte buffer. Because a buffered outer read may span
several raw reads, the reader also compares the file's identity, size, mtime,
and ctime before and after it. Any observed producer change selects the
complete-reader fallback rather than accepting a buffer spliced across producer
generations.

The supported lifecycle is ordinary append plus producer rollback of the
uncommitted suffix. Arbitrary external rewrites of an already committed prefix
are outside this guarantee. The emit-side append/rollback protocol and
authoritative recovery locking are unchanged.

## When an entry is discarded

The index falls back to the complete reader on:

- device/inode change or file shrink;
- an unchanged size with a different `ctime_ns`;
- a checkpoint segment or newline-boundary digest mismatch;
- an archive appearing, CR bytes, or unreadable/invalid sequence state;
- an old or internally inconsistent cache entry shape.

A failed lookup removes the local entry rather than publishing it again. The
fallback read supplies the response from current journal bytes; a later request
may build a fresh index. `ctime_ns` is only a cheap same-size invalidation check,
not the sole generation authority—the segment digest covers larger regrowth and
coarse timestamp cases as well.
