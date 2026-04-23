# Chae-Min Knowledge Base

This directory is the living knowledge base for Chae-Min. Keep `MEMORY.md`
small; put larger context here and link to it from memory only when needed.

Use Obsidian-compatible Markdown. Wikilinks are the default soft knowledge graph
format: connect related notes with `[[note-name]]`.

## Core Notes

- [[projects]]: active and historical projects
- [[workflows]]: recurring workflows and runbooks before they
  become skills
- [[decisions]]: decisions, reasons, dates, and consequences
- [[sources]]: trusted sources, research trails, and verification
  notes
- [[research]]: research planning, source priority, evidence handling, and
  synthesis protocol
- [[security]]: security assumptions, guardrails, reviews, and
  hardening notes
- [[momentum]]: energy patterns, restart points, and ways to help
  Kei move when stuck or energized
- [[maintenance]]: Codex maintenance, persona drift control, authority files,
  and maintenance cadence
- [[chamin]]: Chae-Min self-memory, behavior lessons, limitations, and repair
  patterns

## Chae-Min Skills

- `/Users/kei/.hermes/skills/chamin-research-brief/SKILL.md`
- `/Users/kei/.hermes/skills/chamin-maintenance-audit/SKILL.md`

## Promotion Rules

- Stable preference -> `USER.md`
- Always-needed active fact -> `MEMORY.md`
- Larger context -> `knowledge/*.md`
- Repeatable procedure -> `~/.hermes/skills/`
- Old conversation detail -> `session_search`

## Entry Format

Use short entries with evidence when possible:

```md
## YYYY-MM-DD - Topic

Conclusion:
Evidence:
Related: [[project-or-topic]] [[decision-or-source]]
Next:
```

## Graph Rules

- Use stable kebab-case note aliases inside wikilinks.
- Add `Related:` links for meaningful relationships.
- Do not create empty notes only to fill the graph.
- When a note becomes a repeatable how-to, promote it to a skill and link the
  skill from [[workflows]].
