# CocoIndex Second Brain Sandbox

Purpose: test CocoIndex as a derived incremental index/cache for Yuto's Markdown knowledge base.

Principle:

```text
Markdown KG = source of truth
CocoIndex = derived incremental index/cache
Yuto = verifier + router
```

This pilot does not modify Markdown notes. It reads `/Users/kei/kei-jarvis/knowledge/*.md` and writes derived JSON metadata under:

```text
/Users/kei/kei-jarvis/.cocoindex-secondbrain/index/
```

Internal CocoIndex state lives at:

```text
/Users/kei/kei-jarvis/.cocoindex-secondbrain/cocoindex.db
```

## Setup

From this directory:

```bash
uv sync
```

## Run once

```bash
COCOINDEX_DB=/Users/kei/kei-jarvis/.cocoindex-secondbrain/cocoindex.db \
  uv run cocoindex update main.py
```

Or use:

```bash
./run.sh
```

## Inspect

```bash
COCOINDEX_DB=/Users/kei/kei-jarvis/.cocoindex-secondbrain/cocoindex.db \
  uv run cocoindex show main.py --tree
```

Through Yuto's second-brain CLI:

```bash
python /Users/kei/kei-jarvis/tools/second_brain.py coco status
python /Users/kei/kei-jarvis/tools/second_brain.py coco doctor
python /Users/kei/kei-jarvis/tools/second_brain.py coco search CocoIndex
python /Users/kei/kei-jarvis/tools/second_brain.py coco search "legal evidence"
python /Users/kei/kei-jarvis/tools/second_brain.py coco update
```

The `coco search` command searches derived records: path, title, headings, wikilinks, and body text. Use it to route to likely source notes quickly, then open/read the Markdown source before making claims.

The `coco doctor` command checks drift without updating:

- `missing`: Markdown notes with no derived JSON record.
- `stale`: derived JSON exists but its stored path/hash no longer matches the Markdown source.
- `orphan_derived`: derived JSON records left behind after source notes were deleted or renamed.
- `ok`: true only when source note count, derived record count, hashes, and paths all match.

## Practice loop

Use this loop until CocoIndex becomes muscle memory:

```bash
python /Users/kei/kei-jarvis/tools/second_brain.py coco update
python /Users/kei/kei-jarvis/tools/second_brain.py coco doctor
python /Users/kei/kei-jarvis/tools/second_brain.py coco search "<topic>"
python /Users/kei/kei-jarvis/tools/second_brain.py search "<topic>"
python /Users/kei/kei-jarvis/tools/second_brain.py status
```

Interpretation:

- `coco update` refreshes the derived index.
- `coco doctor` verifies source/index drift before trusting the derived cache.
- `coco search` finds candidate notes quickly from the derived index, including body text.
- `search` checks actual Markdown content directly.
- `status` verifies graph health remains OK.

## Live mode

Only use live mode when intentionally watching files:

```bash
COCOINDEX_DB=/Users/kei/kei-jarvis/.cocoindex-secondbrain/cocoindex.db \
  uv run cocoindex update main.py -L
```

## Safety

- Source Markdown files remain untouched.
- Derived JSON files are disposable and rebuildable.
- No embeddings in phase 1.
- No cloud services in phase 1.
- Do not use with legal/forensic evidence yet.
- `cocoindex drop` can delete target state; use only with explicit intent.
