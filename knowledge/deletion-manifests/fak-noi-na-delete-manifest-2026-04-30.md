# Deletion Manifest: ฝากหน่อยนะ / fak-noi-na

Created: 2026-04-30
Purpose: metadata-only manifest before removing project/product baggage from active Yuto surfaces.

No file contents or secret values were copied into this manifest.

## Deleted paths

| Path | Existed before deletion | Type | File count | Dir count | Bytes | .env-like file count | Status | Notes |
|---|---:|---|---:|---:|---:|---:|---|---|
| `/Users/kei/kei-jarvis/projects/fak-noi-na` | yes | directory | 36999 | 2835 | 2367082998 | 1 | deleted after Kei confirmation | Main project directory. Top-level entries observed before deletion: `.DS_Store`, `README.md`, `app`, `examples`, `mvp-spec.md`, `product-brief.md`, `research`. |
| `/Users/kei/kei-jarvis/knowledge/product-fak-noy-na.md` | yes | file | 1 | 0 | 961 | 0 | deleted after Kei confirmation | Focused product context note formerly linked from knowledge index and active memory. |

## Active-surface cleanup

- Removed the plain-text product-fak-noy-na link from `/Users/kei/kei-jarvis/knowledge/index.md`.
- Removed active memory pointer in `/Users/kei/.hermes/memories/MEMORY.md` that pointed to `knowledge/product-fak-noy-na.md`.
- Kept historical evidence in `knowledge/yuto-rlm-task-log.jsonl`; historical logs are not active project context.

## Runtime / scheduler checks before deletion

- `cronjob(action="list")` found one Yuto daily maintenance audit job only; no job referencing fak-noi-na was visible.
- Process search for `fak-noi-na`, `fak-noy-na`, and Thai project name returned no matching running processes.

## Post-deletion verification targets

- Deleted paths should be missing.
- Yuto graph should rebuild with `broken=0` and `orphans=0`.
- Active memory and active knowledge should not contain active product pointers, excluding this deletion manifest and historical RLM logs.
