# Second Brain Dashboard

Updated: 2026-05-04

Conclusion:
Yuto's second brain is active and should optimize for fast, location-aware recall: know what exists, where it lives, and retrieve the right source quickly without bloating active memory. Improve in small verified loops, not by adding a large RAG/database layer before the graph and routing are useful.

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
python3 tools/second_brain.py recent --query "AI-Books Book Expert Factory" --limit 5
python3 tools/second_brain.py search "memory routing"
python3 tools/second_brain.py memory candidates --min-chars 140
python3 tools/second_brain.py palace search "latest recall"
python3 tools/second_brain.py palace doctor
python3 tools/second_brain.py path dashboard
python3 tools/second_brain.py new "Source title" --type source --why "why it matters" --evidence "path or URL" --next "next review action"
```

Use from chat:
- "ค้น second brain เรื่อง <topic>" -> search `knowledge/*.md` first, then session_search if needed.
- "เก็บเข้า second brain" -> create a focused note only after evidence/source/path is known.
- "second brain status" -> run `python3 tools/second_brain.py status`.

Fast recall protocol:
1. If Kei asks where something is, search known paths first; answer with exact path(s), not a lecture.
2. If Kei references recent/latest work ("ล่าสุด", "เมื่อกี๊", "คุยกันล่าสุด"), inspect raw sessions by mtime first with `python3 tools/second_brain.py recent --query "<topic>" --limit 5`; do not rely on `session_search` summaries as first source.
3. If Kei references older prior work, use `session_search` after checking likely knowledge/source paths.
4. If the item is durable and reusable, route it to `knowledge/` or a skill; keep active memory as pointers only.
5. If the claim depends on current files/tools/models, verify live before answering.

Today status:
- Current second-brain status on 2026-05-04: `nodes=68 edges=247 broken=0 orphans=6`.
- Active memory pressure after adding Kei's fast-recall preference: `USER.md` about 95% full; keep future additions compact or route to `knowledge/rules.md`.
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
