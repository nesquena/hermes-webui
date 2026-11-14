# Realtime voice in SynPulse

Realtime voice provides a native spoken conversation while the regular SynPulse
engine works in separate visible chats. Short conversational answers come from
the realtime model. Requested actions and longer work use the authenticated chat
engine, including its tools, per-user governance, manual/automatic approval, bot
scope and actual `delegate_task` execution when available. The user can keep
speaking and ask additional questions while multiple jobs run.

Enable it with `SYNPULSE_REALTIME_VOICE_ENABLED=1` and a server-side
`OPENAI_API_KEY` with Realtime API entitlement and billing. It is disabled by
default. Install the application requirements, including `websockets>=15,<16`.
The speech connection uses `gpt-realtime-2.1`, `marin` and
`gpt-4o-mini-transcribe`. This is separate OpenAI API usage, not a Codex
subscription. Engine work keeps the parent chat's selected model/provider.
Provider credentials and raw diagnostics never reach the browser.

The browser needs HTTPS, WebRTC and microphone permission. Startup is muted;
Hold to talk or Unmute explicitly enables capture. Interrupt speech clears
playback without cancelling engine work. End voice, leaving the page, changing
conversation, transport failure or the 15-minute limit close the call. In-app
panel navigation and opening a dispatched task in a separate tab preserve voice. Existing dispatched chats remain
available and continue under normal engine governance; stop them with the normal
chat Stop control. A failed voice service leaves written chat usable.

## Conversation and work

The browser transports native audio and displays captions. It does not turn each
transcript into an ordinary chat submission, execute model function calls, or
narrate every completed chat. Written drafts remain untouched.

A trusted server sideband exposes only these model tools:

- `dispatch_work({request, mode})`, where mode is `chat`, `background` or
  `subagents`. Each accepted request starts a separate visible ordinary chat.
  Subagents mode asks the engine to use the available `delegate_task` tool for
  independent subtasks; it does not promise a delegate merely because the chat
  started. Actual tool/journal results establish execution.
- `get_work_status({session_id?})` returns only work created by this voice call.

The server returns correlated `function_call_output` items immediately after
work starts. Completion requires the authoritative engine run journal and no
pending delegation or undelivered delegated result. An idle parent while its
asynchronous children run remains a running task; unverified delegation state
is reported as an error to inspect, never as completed work. It monitors the child chats and reports completion or a pending
approval while the voice conversation remains open. Spoken result notifications
wait until the user is not speaking, no response is generating, and the previous
audio has stopped playing. Harmless provider errors for cancellation or clearing
when no response/audio is active do not end the call. A pending user turn is allowed to receive its native
response first. Duplicate provider call IDs execute once per connection.

Manual approval appears alongside the child chat in the voice panel. It uses the
existing authenticated approval endpoint and stable one-shot approval ID. The
voice model cannot approve actions on the user's behalf or expand a permission
envelope. AI approval, if configured for that user by an administrator, happens
inside the existing governed engine. Unknown/denied voice tool requests return a
failure while the conversation continues; revocation of the voice identity or
parent access closes the call.

## API and state ownership

- `POST /api/voice/realtime/call` accepts `{session_id, sdp}` and returns
  `{sdp, model, voice_id}` only after its trusted sideband connects.
- `GET /api/voice/realtime/status?voice_id=...` returns
  `{voice_id, session_id, state, error, transcript, tasks}`. Transcript entries
  contain `{id, role, text}`. Tasks contain `{task_id, session_id, mode, status,
  title, result, error, approval?}`. Task status is `starting`, `running`,
  `waiting_approval`, `done` or `error`. Closed/expired calls leave the live
  registry immediately; subsequent status requests return 404.
- `POST /api/voice/realtime/end` accepts `{voice_id}` and closes that call only.

The browser owns its microphone tracks, peer connection and display state.
The server owns the sideband, exact-call-ID dedupe, worker pool and task monitor.
Normal engine session persistence owns the actual work and tool journal. Voice
transcripts use a separate private journal under the application state directory
`voice/<actor-hash>/<voice-id>.json`; they are not merged into an active engine
turn's message array. No raw audio, SDP, authentication cookie or provider token
is saved in this journal. Files are created with mode 0600, directories 0700.
Each voice journal retains at most 200 transcript entries, 16 tasks and bounded
text. Each actor retains the most recent 100 journal files. Live connections are
limited to two per actor and 64 per process, with four negotiation attempts per
minute per actor. Closed calls release their worker/socket resources and
forget their captured authentication headers; a server
restart ends voice calls but does not delete normal child chats. Journals are
persisted for diagnosis; a previous voice connection cannot be resumed after a
restart through the live status endpoint.

## Authorization and deployment

All three routes require the existing authenticated identity and `chat:use`.
Mutation routes retain normal CSRF checks. Enforced governance checks both the
realtime model and transcription model/provider. Explicit denies and absent
managed whitelist grants stay effective. Report-only governance remains an
observation mode, as in the rest of the application.

Before negotiation, after negotiation, before dispatch, before returning results
and periodically during a call, the server revalidates the captured identity,
current policy and parent chat access. Child results and pending approvals also
require current child access. Revoked child data is cleared from returned task
snapshots. Provider error text is never returned to users.

The server bridge sends fixed loopback HTTP requests to the actual server's
bound port with the original cookie, CSRF token and origin provenance. When the
listener uses native TLS, the bridge uses HTTPS and the configured server
certificate as its trust anchor, with certificate verification required and an
exact SHA256 leaf-certificate pin. The pin authenticates the listener even when
its public DNS certificate does not name the loopback IP; verification is never
disabled. Reverse-proxy HTTPS with a plain local listener uses local HTTP. The
bridge uses `requests.Session(trust_env=False)`, does not follow redirects, and never accepts
a model-supplied URL, headers, actor, workspace, profile, project or bot roster.
The actual HTTP request dispatcher establishes current owner/profile context and
runs normal route, resource, project and session checks. The bridge selects the actual listener transport, including native TLS. Shared project tasks use
`/api/projects/chat`; other tasks use `/api/session/new`, followed by
`/api/chat/start`. Work-start timeouts preserve the child chat and are never
silently retried, because execution may already have begun.

The provider sideband uses the official call ID from the SDP response Location
header and `wss://api.openai.com/v1/realtime?call_id=...`. The provider API key is
kept on that server connection. No raw application tools or generic execution
endpoint are exposed to the speech model. Audio goes directly to OpenAI while
the microphone is enabled. Only speech and task summaries/results supplied to
the voice conversation are sent; the application does not copy the full parent
chat history or workspace into the realtime session.

Rollback is disabling `SYNPULSE_REALTIME_VOICE_ENABLED`; the normal chat engine
and existing persisted work remain available. Disabling or revoking voice access
also closes existing calls on their next validation.

Asynchronous delegation resumes with the initiating authenticated identity,
through a private server-owned authority reference carried by the delegation
ledger. It never borrows the conversation owner's permissions. Every resumed
turn checks current local policy and membership and retains the initiating
job's hard permission ceiling, including its original role, explicit denies,
models, files, bot scope, environment grants and usage limits. Later promotion
cannot enlarge that job. A session workspace change prevents the old job from
resuming execution in a different workspace. Eligible action approval uses the administrator's
current manual/automatic mode and prompt; it does not repeat a historical
approval workflow. The captured external SSO group claims are not refreshed
from the identity provider during autonomous continuation.

Live workspace membership and managed-bot access are checked before each tool and each primary model
request, including delegated work. The primary model permission envelope is
the turn's bound policy plus retained original ceilings; these request checks
do not reload external model policy mid-turn. Tool action review does reload
the authoritative policy before execution. File candidates and the original
workspace must also remain accessible. These checks are application
authorization and do not make an unrestricted host shell an OS sandbox.

Authority records are created only when an actual async job captures its
reference. They remain private under `continuation-authority` alongside the
durable job lifecycle so delayed jobs can resume after a restart; unknown or
missing authority fails closed. The store is bounded at 10,000 records and
256,000 bytes per record. At capacity, new authority captures fail closed while
existing valid references remain usable. There is no automatic expiry or cleanup.
Records should be removed only after every
referencing job and queued delivery has completed or been explicitly discarded.
Ending voice does not delete that authority or cancel the underlying work.

## Verification

Run `./scripts/test.sh tests/test_realtime_voice.py
 tests/test_realtime_voice_runtime.py tests/test_realtime_voice_tls.py
 tests/test_realtime_voice_frontend.py`
with a supported Python selected as described in TESTING.md. Synthetic provider
events test native function calls, exact-call-ID dedupe, concurrent dispatch,
idle playback gating, current authorization, child result redaction, private
transcript persistence and continued conversation after ordinary tool denials.
Real native-TLS listener tests use a public-name certificate (without a loopback
IP name) and prove both successful certificate-pinned delivery and rejection of
a different leaf certificate before sending authentication or work content.
The frontend E2E runner exercises the actual authenticated HTTP bridge and engine
against local deterministic providers, including file effects and approval flows.
These fixtures do not establish production provider billing/model entitlement,
real microphone quality or actual WebRTC acoustics. A real user-gesture browser
call is still required to verify those properties before production enablement.

Official protocol references:
- https://developers.openai.com/api/docs/guides/realtime-webrtc
- https://developers.openai.com/api/docs/guides/realtime-server-controls
- https://developers.openai.com/api/docs/guides/realtime-conversations
- https://developers.openai.com/api/docs/models/gpt-realtime-2.1


### Conversation pane

The native realtime call appears in a dedicated pane above the existing typed
composer. Minimize returns to the transcript without changing a continuously
unmuted microphone; End closes the call. The microphone still starts muted.
Hold-to-talk releases on pointer/keyboard release, focus loss or minimizing.
Escape within the pane minimizes it, and ending from its controls returns focus
to the voice launcher. The typed draft and session controls remain mounted.

Recent captions and background tasks use the existing bounded controller state.
Each task shows its title and server status, with results and the child-chat link
behind a native disclosure. Pending approvals retain the existing exact-ID
buttons. The task count remains visible when minimized. This is presentation
only: the server sideband, permissions and approval endpoint remain authoritative.
The interface supports light/dark themes, narrow layouts and reduced motion.
