# Source: OnionClaw

Date checked: 2026-05-01
Source URL: https://github.com/JacobJandon/OnionClaw

Conclusion:
OnionClaw is a Tor/dark-web OSINT toolkit packaged as an OpenClaw skill and standalone Python scripts. It is strategically relevant as evidence that AI agents + Tor + autonomous OSINT pipelines are becoming accessible, but it should not be installed into Yuto core. If used, it belongs in an isolated, explicit, defensive-only sandbox with legal/ethical scope and no autonomous purchasing/posting/interaction.

Source facts:
- GitHub API showed repository `JacobJandon/OnionClaw`, created 2026-03-14, pushed 2026-04-27, Python, 19 stars, 6 forks, 4 open issues, default branch `main`, topics include `dark-web`, `tor`, `osint`, `threat-intelligence`, `mcp-server`, `openclaw-skill`.
- README states OnionClaw is an OpenClaw skill + standalone tool for Tor/.onion access, based on the SICRY engine, with scripts `check_tor.py`, `renew.py`, `check_engines.py`, `search.py`, `fetch.py`, `ask.py`, and `pipeline.py`.
- README explicitly names dangerous dual-use capabilities: dark-web crawling, marketplace monitoring, credential surveillance, deanonymisation research, criminal automation, disinformation infrastructure, and zero-day brokerage.
- `requirements.txt` lists `requests[socks]`, `beautifulsoup4`, `python-dotenv`, and `stem`; optional MCP server mode is noted in comments.
- `.env.example` supports Tor SOCKS/control ports and LLM providers `openai`, `anthropic`, `gemini`, `ollama`, and `llamacpp`; placeholder API keys are present only as examples.
- `setup.py` can edit Tor configuration, write `.env`, chmod secret files, prompt for API keys, and optionally use `sudo usermod` / `systemctl` for Tor cookie-auth setup.
- `sicry.py` contains SQLite-backed cache/watch state, TorPool support, LLM providers, STIX/MISP export helpers, and many crawl/watch code paths.
- CI workflow performs syntax/import checks only across Python 3.9-3.12; it does not prove live Tor behavior, legal safety, or end-to-end OSINT correctness.
- License signal is inconsistent: repository API reports `NOASSERTION`, `LICENSE` is Apache-2.0, while README/SKILL badges/text claim MIT.

Local availability checked:
- On this machine at check time: `requests`, `bs4`, and `dotenv` importable; `stem` missing; `tor`, `openclaw`, and `onionclaw` binaries not found.
- No install was performed.

Yuto relevance:
- Useful conceptually for threat-intel/research workflows: structured intake query -> search -> fetch -> filter -> summarize -> report.
- Relevant to the “Criminal AI” direction as a signal: AI can assist legal/criminal-defense adjacent investigation only with strict guardrails, source logging, and human expert review.
- Not suitable as a default Yuto skill because the action surface includes Tor access, hidden-service crawling, identity rotation, watch daemons, and LLM analysis of potentially illegal/sensitive content.

Risks and cautions:
- Dual-use and potentially illegal depending on jurisdiction, target, and content accessed.
- High prompt-injection risk from dark-web pages and adversarial content.
- Secret risk: `.env` may hold LLM keys; never print or transmit real keys.
- Operational risk: Tor usage may create legal/forensic exposure; identity rotation is not legal protection.
- System-change risk: `setup.py` can modify Tor config and systemd/user groups; avoid running on Kei's main environment.
- Reliability risk: dark-web search indexes and hidden services are unstable; search/fetch results are not truth without corroboration.
- Licensing ambiguity should be resolved before reuse or redistribution.

Recommended posture:
- Do not install into Yuto core.
- If Kei wants a pilot, use a disposable VM/container with no personal credentials, no production network, no autonomous interaction, no market transactions, no posting, and explicit allowed queries.
- Prefer clearnet/offline mock data first to test the OSINT pipeline shape.
- If used for legal/criminal-defense product research, frame it as defensive incident-response intelligence and lawyer-reviewed evidence organization, not autonomous dark-web operations.

Canary questions before any pilot:
- Is the use case defensive and lawful in the relevant jurisdiction?
- Are targets/queries scoped and documented?
- Are we avoiding purchasing, posting, credential use, exploitation, or contact with illicit services?
- Are LLM outputs treated as leads, not evidence or legal conclusions?
- Is the environment isolated from Kei's main machine and secrets?

Related: [[sources]] [[security]] [[yuto-growth-loop]] [[source-openkb]]
