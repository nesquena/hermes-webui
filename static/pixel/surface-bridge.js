// hermes pixel office -- bridge between /api/agent-activity stream and the
// office state engine. Maps surfaces (webui / telegram / discord / weixin / cli /
// cron / ...) to pixel characters. Rewritten from OpenClaw's agentBridge.ts
// because the role semantics differ ("per surface" vs "per agent" -- design.md D3).
// Implementation pending -- task 9.1 in tasks.md.
