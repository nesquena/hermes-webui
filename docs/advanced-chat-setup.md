# Advanced chat setup

Two optional features for self-hosted Hermes WebUI deployments. **Most users need neither** — the defaults (in-process chat, no prefill) work out of the box.

## Session recall prefill

WebUI can attach ephemeral prefill messages to new browser-originated
agent turns. This is useful when a deployment already has a local recall or
router script for Joplin, Obsidian, Notion, llm-wiki, or another third-party
notes source and wants browser chat to know where durable context lives.

Prefer a compact router-style prefill (for example, "Joplin has the durable
project context; use the available notes/search tools before answering
detail-dependent questions") instead of dumping the full note corpus into every
new browser session. The prefill should point the agent toward retrieval; the
notes/search tools should provide the specific facts on demand.

Static JSON remains supported through `prefill_messages_file` or
`HERMES_PREFILL_MESSAGES_FILE`. For dynamic recall, opt in explicitly with a
WebUI-specific script hook:

```yaml
webui_prefill_messages_script:
  - python3
  - /path/to/notes_recall.py
webui_prefill_messages_script_timeout: 5
```

or:

```bash
HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT="python3 /path/to/notes_recall.py" \
HERMES_WEBUI_PREFILL_MESSAGES_SCRIPT_TIMEOUT=5 \
./ctl.sh restart
```

The script may print either an OpenAI-style JSON message list, a JSON object with
a `messages` list, or plain text; plain text is wrapped as one `user` prefill
message so dynamic recall text becomes ordinary context instead of an extra
system instruction. If the hook must provide system-level guidance, emit JSON
messages with an explicit `role: "system"` entry instead. Script output is capped
at 256 KiB before parsing. Parsed prefill context is then bounded by
`webui_prefill_context_max_chars` or `HERMES_WEBUI_PREFILL_CONTEXT_MAX_CHARS`
(default: 12,000 characters; set to `0` to disable). When a dynamic script
exceeds the budget and a compact static prefill file is configured, WebUI falls
back to that file. If no compact fallback is available, WebUI injects a short
retrieval instruction instead of sending the oversized note/body payload with
every new browser turn. The browser only receives a compact status event
(`source`, `label`, message count, compaction metadata, and redacted errors),
never the prefill message bodies.

## Session title generation

Hermes WebUI derives a provisional session title from the first user message
and, after the first response, may call an LLM to generate a better title
(and periodically refresh it for long sessions).

For structured messages containing text and native images, title generation
uses the user text without flattening or modifying the stored message. Title
comparison and title-model inputs remove the internal `[Workspace::v1: ...]`
prefix and one terminal `[Attached files: ...]` or
`[Attached files for this steer: ...]` suffix separated by a blank line.
For structured content, this cleanup applies to the first text part that
provides title content. Literal legacy `[Workspace: ...]` text and later text
parts remain unchanged. Initial generation, explicit regeneration, and adaptive
refresh use this title-specific cleanup.

Background generation requires user text and a substantive assistant response.
It recognizes the sanitized provisional title as well as the existing raw
placeholder, so internal metadata does not make an image-containing turn look
manually titled. Image-only or metadata-only content does not provide title
text. Existing manual-title protection and the title-generation setting still
apply; this cleanup does not rewrite the transcript or native image parts.

Automatic title-generation LLM calls honor the active Hermes profile's
`auxiliary.title_generation.enabled` setting (default: `true`):

```yaml
auxiliary:
  title_generation:
    enabled: false
```

When disabled:

- the provisional first-message title stays in place and is never replaced
  or overwritten by an automatic LLM call or local fallback;
- the periodic adaptive refresh is skipped;
- the explicit "regenerate title" action returns a
  `title_generation_disabled` response instead of calling a title model.

The WebUI's `auto_title_refresh_every` setting remains a separate control for
periodic refreshes of already-generated titles; it does not re-enable
automatic generation when the auxiliary flag is off.

### Pinning the title language

`auxiliary.title_generation.language` pins the language generated titles are
written in, whatever language the conversation itself is in:

```yaml
auxiliary:
  title_generation:
    language: Japanese
```

A nonblank value is read once per generation attempt and drives both halves of
that attempt. The prompt instruction becomes `Write the title in <language>.`
in place of the default "match the language of the user question" rule, and the
post-generation drift check is retargeted to agree with it. Both title routes
honour the pin: the auxiliary-client route and the active-agent route.

Retargeting the validator is the point. The drift check exists to reject a
title whose language wandered away from the conversation (issue #3293), and on
a pinned install that same check would reject the pinned title the prompt had
just asked for. How a generated title is validated therefore depends on the
pin:

- **A pin the script map recognises** (`Japanese`, `Russian`, `Amharic`,
  `Bengali`, `Brazilian Portuguese`, `pt-BR`) is checked against that
  language's script. The map covers the Latin, Cyrillic, CJK, Arabic, Hebrew,
  Greek, Devanagari, Thai, Georgian, Armenian and Ethiopic scripts, and the
  major Indic and South-East Asian ones.
  A title substantially outside it is still rejected, so an English pin
  rejects a CJK title and a Japanese pin rejects a Cyrillic one. A CJK pin
  keeps borrowed Latin terms (`Python`, `WeChat Pay`) as long as the title
  also holds at least two CJK characters, the same exemption the
  conversation-based check applies. "Substantially outside" means more than
  a third of the title's letters, summed across every other script. Styled
  alphabets such as mathematical bold, circled, enclosed or fullwidth letters
  count as the plain letters they decompose to; Roman numerals and circled
  digits are not letters and are left out of the count.
- **A pin the script map cannot resolve**, and **no pin**, both keep the
  original behaviour: the title is checked against the language of the
  conversation's opening message. A pin outside the map therefore still
  changes the prompt, and a title that follows it into a script the
  conversation does not use is rejected as drift. Pin a language the map
  knows to get cross-script titles.

A language written in two scripts in majority use accepts either by default:
`Punjabi` accepts Gurmukhi and Shahmukhi (Arabic script), and `Mongolian`
accepts Cyrillic and the traditional Mongolian script. `Serbian`, `Bosnian` and
`Uzbek` have no default, because Cyrillic and Latin are both in wide use, so a
bare pin naming one of them keeps the conversation check.

A script qualifier narrows a recognised language to one script. It can be an
ISO 15924 code in a BCP 47 tag (`pa-Arab`, `pa-Guru`, `mn-Mong`, `kk-Latn`,
`sr-Latn`, `zh-Hant`) or an English script name beside the language
(`Punjabi (Arabic)`, `Malay (Jawi)`, `Mongolian (Traditional)`,
`Serbian (Cyrillic)`). A qualifier is also how a minority script opts in: bare
`Kazakh` accepts Cyrillic only, and `kk-Latn` accepts Latin.

Anything ambiguous fails closed to the conversation check instead of widening
what is accepted:

- a qualifier on a language the map does not know (`Klingon-Latn`, `xx-Latn`,
  `Klingon (Arabic)`);
- a language named only inside brackets after a word the map does not know
  (`Klingon (English)`, `Русский (Russian)`);
- a pin longer than 256 characters once surrounding whitespace is trimmed,
  or 64 after normalisation;
- two different qualifiers (`English-Latn-Cyrl`, `pa-Arab-Guru`), including
  two scripts validated the same way (`ja-Hira-Kana`,
  `Chinese (Simplified, Traditional)`), or `Latin` beside
  another script (`Cyrillic Latin`); equivalent ones collapse
  (`pa-Arab-Aran`, `Japanese (Kanji, Hani)`);
- a script code in a tag that the table does not know (`ja-Zyyy`);
- two languages (`English French`);
- a two-letter code outside a BCP 47 tag (`pt (Brazil)`, `No preference`),
  because such codes collide with region codes and ordinary words. Write the
  language name or a tag instead: `Portuguese (Brazil)` or `pt-BR`.

Language lookup is diacritic-insensitive. In a BCP 47 tag the language is the
first subtag. A POSIX locale's encoding and modifier are ignored
(`en_US.UTF-8`), except that every script named in the modifier qualifies
(`be_BY@latin`, `sr_RS@latin`; `pa_IN@arabic-gurmukhi` conflicts), and so does
a piece of the encoding that is exactly a script code (`en-Latn.Cyrl`
conflicts). Otherwise a
language name counts anywhere outside brackets, so `Francais`, `Français`,
`Traditional Chinese` and `Brazilian Portuguese` resolve. The same rule
applies to languages whose names are also script names (Arabic, Greek, Thai,
Latin and others), so `Egyptian Arabic` and `Modern Greek` resolve, and so do
`Klingon Arabic` and `Klingon-Latin`: the unknown word is read as a modifier of
the named language. `Klingon (Arabic)` stays unresolved, because a bracketed
word only qualifies. A word in brackets only
qualifies: `Tamil (Arabic)` is Tamil in Arabic script, and `mn (Mongolian)` is
Mongolian because the code and the name agree. A language's own name for itself
is mostly not recognised, and a bracketed English name beside it does not
rescue it: `Русский (Russian)` and `Klingon (English)` are unresolved. Write the
English name or a tag instead: `Russian` or `ru`.

A pin longer than 256 characters once surrounding whitespace is trimmed, or
64 characters after
normalisation (lowercasing, diacritic folding, punctuation to spaces), is not
parsed; it is unresolved, so the conversation check applies, and the title
prompt leaves it out.

The pin affects session titles only. It does not change the language the
assistant replies in, and it has no effect when
`auxiliary.title_generation.enabled` is `false`, since no LLM title is
generated at all in that case.

## Gateway-backed browser chat

By default, browser chat runs through WebUI's in-process legacy runtime. Advanced
self-hosted deployments can opt into routing new browser turns through a running
Hermes Gateway API server while preserving the existing WebUI `/api/chat/start`
and `/api/chat/stream` browser contract:

```bash
HERMES_WEBUI_CHAT_BACKEND=gateway \
HERMES_WEBUI_GATEWAY_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_GATEWAY_API_KEY=... \
./ctl.sh restart
```

Gateway-backed approval prompts need one more explicit opt-in because they use the Gateway runs API path:

```bash
HERMES_WEBUI_CHAT_BACKEND=gateway \
HERMES_WEBUI_GATEWAY_BASE_URL=http://127.0.0.1:8642 \
HERMES_WEBUI_GATEWAY_API_KEY=... \
HERMES_WEBUI_GATEWAY_USE_RUNS_API=true \
./ctl.sh restart
```

Use this when the connected gateway advertises approval support and you want tool approval cards to appear in WebUI. Without `HERMES_WEBUI_GATEWAY_USE_RUNS_API=true`, gateway chat stays on the legacy chat-completions transport and approval-capable commands can remain pending in the agent without a WebUI approval card.

Each turn goes to the Gateway of the session's own profile. A named profile that sets its own `HERMES_WEBUI_GATEWAY_BASE_URL` (in its `.env`) or `webui_gateway_base_url` (in its `config.yaml`) uses that URL. Otherwise WebUI sends the turn to the shared listener under `/p/<profile>` and authenticates with that profile's `API_SERVER_KEY`, which is how a multiplexing Gateway (`gateway.multiplex_profiles: true`) selects the profile. The root profile and isolated-profile deployments use the base URL unchanged. A Gateway that does not serve the profile answers 404 and a missing profile key answers 401, so a named-profile turn is never silently run as the root profile. That error names the fix: enable `gateway.multiplex_profiles` on the Gateway, or set `HERMES_WEBUI_GATEWAY_BASE_URL` in the profile's `.env`. This differs from earlier releases, which ran such a turn as the root profile. A run admitted by an earlier release is still reattached after an upgrade on the unprefixed URL with the key it was started with.

On the runs API path the turn is executed by the Gateway, so restarting WebUI does not stop it. WebUI stores the Gateway `run_id` on the pending turn (and submits it with an `Idempotency-Key` so the Gateway keeps a durable run record). On startup, WebUI reattaches to every such run by polling `GET /v1/runs/{run_id}` until it settles, then writes the real final answer into the session instead of a "Response interrupted" marker. Stop still cancels a reattached run, and a pending approval is shown again. Token-by-token output from before the restart is not replayed; the reattached turn shows only the final answer. If the Gateway no longer knows the run (for example, it restarted too and the run was interrupted), the turn ends with an error message instead. The legacy chat-completions transport cannot reattach: its turn ends when the WebUI process that holds the HTTP stream exits.

When YOLO is enabled for a gateway-backed browser session, WebUI approves every
approval already parked for that session: Runs API prompts are relayed by their
exact `run_id` and mirror token, and local/no-run waiters are all released. It
then automatically answers later Runs API approval requests while the WebUI
session flag remains active. The flag is committed only after every currently
parked remote relay succeeds; a later prompt that races that unconfirmed drain
remains visible instead of being speculatively auto-approved. The handoff is
also shared with local approval admission: a local waiter arriving after
the current drain snapshot waits for the same session handoff and is released
immediately if YOLO has committed, rather than being parked behind an enabled
session. This is client-managed compatibility behavior: the current Runs API has
no session-YOLO toggle, so a request briefly reaches the approval boundary before WebUI answers
it, and Agent-owned policy such as unrestricted computer-use mode is unchanged.
Native API session YOLO is tracked in [Hermes Agent PR #61946](https://github.com/NousResearch/hermes-agent/pull/61946).

`HERMES_WEBUI_CHAT_BACKEND` is intentionally strict: only `gateway`,
`api_server`, or `api-server` enable the bridge. Generic truthy values such as
`1` or `true` are ignored so existing deployments do not change execution
ownership accidentally. If `HERMES_WEBUI_GATEWAY_API_KEY` is omitted, WebUI falls
back to `API_SERVER_KEY` when present. When Gateway returns HTTP 401, WebUI
reports a `gateway_auth_error` that points at this WebUI↔Gateway key mismatch
rather than showing the Gateway's generic provider-style "Invalid API key" body.
`/api/health/agent` also includes a redacted `gateway_chat` block so operators can
see whether gateway mode, base URL, and API-key presence are configured without
exposing the key value. That `gateway_chat` field is an operator diagnostic
payload only; it is not currently rendered as a user-facing health banner in the
browser UI.

The bridge is best used by operators who already run Hermes Gateway/API Server
locally and want browser-originated chat to use the same runtime/tool path as
messaging surfaces. Attachments, cancellation, approvals, and clarify prompts
still follow WebUI's current compatibility path and may not match every messaging
surface until the runtime-adapter migration is complete.
