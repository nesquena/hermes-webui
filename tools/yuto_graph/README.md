# Yuto Graph Tools

Read-only graph indexer for Yuto's Markdown second brain.

Source of truth remains:

- `/Users/kei/kei-jarvis/knowledge/`
- `/Users/kei/.hermes/memories/USER.md`
- `/Users/kei/.hermes/memories/MEMORY.md`
- `/Users/kei/.hermes/skills/`

Generated outputs live under:

- `/Users/kei/kei-jarvis/knowledge/.graph/nodes.json`
- `/Users/kei/kei-jarvis/knowledge/.graph/edges.json`
- `/Users/kei/kei-jarvis/knowledge/.graph/report.md`

Run the Yuto core graph check:

```bash
cd /Users/kei/kei-jarvis
python3 -m tools.yuto_graph.build_graph \
  --root /Users/kei/kei-jarvis/knowledge \
  --memory-file /Users/kei/.hermes/memories/USER.md \
  --memory-file /Users/kei/.hermes/memories/MEMORY.md \
  --extra-root /Users/kei/.hermes/skills/software-development \
  --extra-root /Users/kei/.hermes/skills/yuto \
  --extra-root /Users/kei/.hermes/skills/yuto-maintenance-audit \
  --out /Users/kei/kei-jarvis/knowledge/.graph
```

This scope covers Yuto knowledge, active memory, and core Yuto/software-development skills that are linked from `knowledge/index.md`.

Safety:

- Does not rewrite source notes.
- Reports broken links and orphan notes instead of auto-fixing them.
- Full skill-vault diagnostics are optional; running `--extra-root /Users/kei/.hermes/skills` indexes many plugin skills and intentionally exposes plugin reference/template noise that is not part of Yuto core graph closure.
- External vault roots should be added read-only with `--extra-root` only after scope is clear.
