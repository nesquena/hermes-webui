// hermes pixel office -- office state (characters, seats, dynamics).
// Ported (by rewrite) from OpenClaw-bot-review lib/pixel-office/engine/officeState.ts (MIT).
// NOTE: setAgentTool() is intentionally NOT ported -- hermes webui has no
// current_tool data source (see design.md D9). Interface is:
//   addAgent / removeAgent / setAgentState(id, 'working'|'waiting'|'idle')
//   showClockBubble(id, tooltip)
// Implementation pending -- task 8.2 in tasks.md.
