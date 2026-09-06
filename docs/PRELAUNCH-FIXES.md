# hermes-webui (fork) — required fixes

**Path:** `/home/alex/hermes-webui`  
**Remotes:** `nesquena/hermes-webui` + public fork `Alextechgamer/hermes-webui`  
**Branch:** `fix/session-endpoint-redact-gil-wedge` (`334eb105` 2026-09-01), clean

**Verdict:** not a money product. Redaction branch is the point. Do not market as Tillpress.

---

## Do not do

- Copy tokens into `~/.claude/.credentials.json`
- Dump session transcripts with secrets into issues
- Treat this fork as a launch SKU

---

## High

### H1 — Stay on the redact branch until upstream merge
HEAD: mask JWT / URI-userinfo / Telegram in the >16KB redact fallback. Public-release readiness = upstream’s, plus this leak fix.

**Fix:** do not mix Tillpress/Batchideo work here. Merge/PR to upstream when you want it public; this audit did not re-pentest.

## Next action

None in the money queue.
