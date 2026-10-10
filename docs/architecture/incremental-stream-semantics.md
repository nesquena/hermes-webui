# Incremental live-stream semantics

`attachLiveStream()` owns one incremental semantic state, alongside its raw
`assistantText` accumulator. The raw accumulator remains the reconnect source
(`INFLIGHT.lastAssistantText`); Markdown/smd and fade state remain presentation
only. This does not change transport identity, terminal ownership, scene-cache
identity or canonical asynchronous cancellation settlement.

## Normal growth

The token producer passes the received delta to `_scheduleSemanticProse(delta)`.
Its first action consumes that delta. The non-resetting 32 ms timer coalesces
**publication**, not parsing, including for long replies. Token/fade requests defer
to this producer drain while publication is pending. Semantic boundaries bypass
the timer and drain synchronously. Inflight messages, current prose and
presentation readers use the
same semantic snapshots. Neither a timer nor a paint callback invokes the
full-history parser during trusted append-only growth.

The shared thinking-helper layer supplies `_createIncrementalSemanticState()`:

- the existing `_thinkPairs` vocabulary, including both alternate reasoning tags;
- bounded opener/closer lookahead across chunks;
- code-fence, inline-backtick and indented-code context;
- plain and DSML-prefixed XML tool-block exclusion;
- accumulated visible prose and reasoning, with completed reasoning deduplication;
- two bounded reasoning views for full-run persistence and current live display;
- a visible-content offset for the current post-tool segment.

The two views cache derived merges, not independent semantic observations. Each
view consumes completed reasoning blocks once. Alternating persistence/display
readers must not invalidate each other's cache on every prose token. Separate
`reasoning` events feed their authoritative delta and before/after accumulator
identities into both cached views. Trimming and paragraph recognition advance
from that delta; completed paragraphs are indexed once, while the growing tail
is not repeatedly hashed. Normal channel growth retains the cached inline suffix.
An actual deduplication transition can rebuild that suffix, not the full raw
assistant history. New/replaced channel bases rebuild a view at the explicit
reset/recovery boundary; snapshots do not guess append continuity from prefixes.
The batch-recovery reasoning view uses this same delta-fed machinery.

## Boundaries and recovery

Owned non-token SSE handlers synchronously drain pending semantic publication
before handling their boundary. Tool start/completion seals current prose before
the tool observation; segment reset records a semantic-content offset rather
than parsing a raw suffix on each tick. Interim appends are trusted deltas too.

`_parseStreamState()` is the explicit batch recovery oracle. Rewind, untrusted
replacement/growth, reconnect and uncertain semantic boundaries may resynchronize
from raw text. Trusted split delimiters remain buffered across nonterminal
boundaries; terminal/detach exits settle incomplete delimiters through the oracle.
A resync rebuilds incremental state and segment offsets, including any leading
normalization. The same unchanged pending boundary is not rescanned by duplicate
listeners.
The recovery oracle supplies extracted reasoning parts to two bounded cached
views, preserving separate live-channel and durable all-run reasoning. Nested
XML prefixes and retroactive changes to earlier indented content fail closed
as uncertainty. The first visible line's indentation is recorded incrementally,
including after leading blank lines; later XML normalization must not silently
reclassify an already-published literal thinking tag. Partial code fences do not
announce reasoning transitions.

DSML permits arbitrarily long whitespace prefixes. Lookahead is capped at 256
code units (the detecting character can bring the observed maximum to 257).
An oversized uncertain prefix stops speculative incremental interpretation;
subsequent tokens retain raw text without repeatedly rescanning. An explicit
boundary supplies the batch fallback. This is a fail-closed recovery path, not
a normal token-processing mode.

Terminal/detach drains run before the existing completion/handoff sequence.
Disposal releases the incremental state, fallback snapshot and raw alias; late
callbacks retain the existing generation guards. Internal reconnect rehydrates a
disposed parser even when the old transport failed before the first token; a zero
raw cursor does not mean parser state survived disposal.

## Long-token presentation cost

The semantic fix does not change the reveal cursor or give fade callbacks any
semantic ownership. Long text emitted in a single fade frame (128 or more code
units) shares one inline opacity span: those words previously received identical
animation timing but retained a node per word. Small emissions, code/media,
reduced-motion and silent rewind prefixes keep their existing paths. Rewind
muting splits a grouped span at the previously visible word boundary, preserving
animation of genuinely new tail words. Existing live nodes remain stable until
settlement; ordinary animation cleanup does not remove or replace them.

## Verification

`tests/test_issue7478_spaced_semantic.py` advances a virtual clock by 40 ms per
spaced token, and also covers burst/alternating delivery, split think/XML/DSML
markers, prose/tool/prose order, real inflight synchronization, terminal drain,
reconnect, replacement, uncertainty and reasoning-cache work. Work counters use
UTF-16 code units, matching JavaScript string indexing; they are not heap sizes.
`test_issue7478_cancel.py` additionally drives the real `_wireSSE` token/cancel
listeners with spaced delivery and deferred canonical settlement. Application
error and disposal regressions cover pending delimiter and state-release paths.

The source-extraction test harnesses execute only in an authorized local test
sandbox. A maintainer threat-policy NO-RUN classification is not a test failure
or a product defect, and local results do not imply maintainer approval.

## Legacy segment ownership during incremental paints

A reasoning-only scene can paint before the first prose token arrives. New
compatibility assistant segments must be hidden at creation when that live scene
already owns presentation (Compact Worklog or Transparent Stream). They remain
available to the stream's Markdown/fade and metadata paths; the scene renders the
visible prose. Final-answer-only mode keeps its compatibility body visible.
This constant-work handoff avoids restoring a full legacy-node scan on every
incremental paint. Terminal settlement still renders the canonical final/error
answer through its existing path.

The lifecycle gate's `LIFECYCLE_REASONING_FIRST=1` waits for the reasoning scene
before releasing prose/tools. This pins the previously timing-sensitive ordering
without probabilistic sleeps or retries. It runs for both normal and terminal
error scenarios; `test_issue7478_live_prose_ownership.py` additionally checks
visibility at insertion, repeated segments, and the no-scene/hidden-activity modes.
