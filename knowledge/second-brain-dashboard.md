# Second Brain Dashboard

Updated: 2026-05-02

Conclusion:
Yuto's second brain is active and should be improved in small verified loops, not by adding a large RAG/database layer before the graph and routing are useful.

Current source of truth:
- Active memory: `USER.md`, `MEMORY.md`
- Durable knowledge: `/Users/kei/kei-jarvis/knowledge/*.md`
- Procedures: `/Users/kei/.hermes/skills/`
- Raw/recent evidence: `/Users/kei/.hermes/sessions/session_*.json`
- Generated graph index: `/Users/kei/kei-jarvis/knowledge/.graph`
- Obsidian vault bridge: `/Users/kei/Documents/Obsidian Vault/Yuto Second Brain.md`

Operating rule:
- Capture only durable, source-backed, or user-confirmed knowledge.
- Keep active memory compact.
- Prefer focused notes and links over raw dumps.
- Run graph/tests/canaries after meaningful edits.

Fast commands:
```bash
cd /Users/kei/kei-jarvis
python3 tools/second_brain.py status
python3 tools/second_brain.py search "memory routing"
python3 tools/second_brain.py path dashboard
python3 tools/second_brain.py new "Source title" --type source --why "why it matters" --evidence "path or URL" --next "next review action"
```

Use from chat:
- "ค้น second brain เรื่อง <topic>" -> search `knowledge/*.md` first, then session_search if needed.
- "เก็บเข้า second brain" -> create a focused note only after evidence/source/path is known.
- "second brain status" -> run `python3 tools/second_brain.py status`.

Today status:
- Core graph after memory/KG organization: `nodes=58 edges=227 broken=0 orphans=0`.
- Active `USER.md` pressure after pruning: `1045/1375 chars`.
- Active `MEMORY.md` pressure after pruning: `1565/2200 chars`.
- Obsidian bridge exists at `/Users/kei/Documents/Obsidian Vault/Yuto Second Brain.md`.
- Retrieval/use CLI exists at `/Users/kei/kei-jarvis/tools/second_brain.py`.

Next improvement queue:
1. Make scheduled maintenance reports visible to Kei instead of local-only.
   Success: daily audit result or failure is visible without Kei asking.
2. Use the `new` command as the ingestion inbox for user-provided sources and product/research material.
   Success: every source note records path/URL, why it matters, evidence, and next action.
3. Add a weekly graph review note only if drift appears.
   Success: broken links/orphans stay at zero without adding empty notes.
4. Add semantic/RAG indexing only after the Markdown graph proves useful in daily use.
   Success: retrieval improves answers without replacing Markdown as authority.

Canary questions:
- Where does Yuto store larger durable context? -> `knowledge/*.md`
- Where do repeatable procedures go? -> `~/.hermes/skills/`
- Where should old conversation detail be recalled from? -> `session_search`, not active memory
- Is generated graph authority? -> no, it is an index

Related: [[index]] [[memory-system]] [[yuto-graph-second-brain-plan]] [[workflows]] [[yuto-maintenance-command-center]]
