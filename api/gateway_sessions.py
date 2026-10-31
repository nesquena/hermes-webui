"""Opt-in, profile-local client of the Agent's existing canonical session protocol.

The Gateway owns admission, execution and history. These objects own only sockets;
the normal WebUI gateway_run sidecar and stream registries own recovery and UI state.
No daemon is launched, source label changed, or API credential promoted to operator.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import re
import threading


_LOCAL_SOURCES = frozenset({"cli", "tui", "desktop", "gui", "local", "acp"})
_MESSAGING_SOURCES = frozenset({"telegram", "discord", "slack", "whatsapp", "signal",
                               "matrix", "mattermost", "teams", "line", "feishu"})
_CONNECTIONS = {}
_LOCK = threading.Lock()
PREFIX = "session:"


class SessionTransportError(ValueError):
    """Bounded public failure; never a raw socket URL, credential or remote traceback."""


class SessionTransportDisconnected(SessionTransportError):
    """Preserve the existing durable request so reopening recovers, never resends."""


def external_session(session):
    source = str(session.get("raw_source") or session.get("source_tag")
                 or session.get("session_source") or "").strip().lower()
    return session.get("session_source") != "fork" and source in _LOCAL_SOURCES | _MESSAGING_SOURCES


def display_history(snapshot):
    """Canonical display projections use text; WebUI transcript rows use content."""
    rows = []
    for row in snapshot.get("display_messages", snapshot.get("messages", [])):
        if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}:
            continue
        item = {**row, "content": row.get("text", row.get("content", ""))}
        # Retain the Gateway's row_id and timestamp. Do not invent a WebUI id:
        # the existing state.db reader merges these rows by their native identity.
        rows.append(item)
    return rows


def config_for_session(session):
    """Use the owning profile on workers without request-local profile context."""
    from api.config import get_config_for_profile_home
    from api.profiles import get_hermes_home_for_profile
    return get_config_for_profile_home(get_hermes_home_for_profile(session.get("profile")))


def configured(session, config=None):
    if not external_session(session):
        return False
    if config is None:
        config = config_for_session(session)
    return config.get("webui_gateway_session_transport") in {"local", "unix"}


def model_change_requested(session, body):
    # Messaging rows can have no stored model even after a real Gateway turn.
    # The browser then includes its displayed profile default on every send.
    # That implicit display value is not an override: native prompt.submit sends
    # only session_id, input_id and text; the Gateway selects its own model.
    stored = getattr(session, "model", None)
    if stored in {None, "", "unknown"}:
        return bool(body.get("explicit_model_pick"))
    if body.get("model") and body["model"] != stored:
        return True
    provider = getattr(session, "model_provider", None)
    return bool(provider and body.get("model_provider") and body["model_provider"] != provider)


@asynccontextmanager
async def connect_local_socket(home):
    # Use the Agent's authenticated discovery/bootstrap client. Passing the home
    # explicitly matters: WebUI worker threads have no request-profile context.
    from hermes_cli.gateway_client import _session_ticket, gateway_ws_target
    from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint
    from websockets.asyncio.client import connect

    found = await asyncio.to_thread(discover_gateway_endpoint, home)
    if not attachable(found) or found.endpoint is None:
        raise SessionTransportError("The profile's local Gateway is not available.")
    endpoint = found.endpoint
    if Path(endpoint.profile_id).resolve() != Path(home).resolve():
        raise SessionTransportError("Gateway profile mismatch.")
    ticket = await asyncio.to_thread(_session_ticket, Path(home), endpoint)
    url, protocols = gateway_ws_target(endpoint, ticket)
    async with connect(url, subprotocols=protocols, proxy=None, open_timeout=10,
                       max_size=8 * 1024 * 1024) as ws:
        if ws.subprotocol != "hermes-gateway-v1":
            raise SessionTransportError("The Gateway does not support canonical sessions.")
        yield ws


@asynccontextmanager
async def connect_local(home):
    from hermes_cli.gateway_client import GatewayClient
    async with connect_local_socket(home) as ws:
        async with GatewayClient(ws) as client:
            yield client


def control(run_id, action, **params):
    """Dispatch only to the socket that owns this exact WebUI stream."""
    with _LOCK:
        relay = _CONNECTIONS.get(run_id)
    if relay is None:
        raise SessionTransportError("The Gateway session connection is unavailable; retry after reconnecting.")
    future = asyncio.run_coroutine_threadsafe(relay.control(action, **params), relay.loop)
    try:
        return future.result(timeout=12)
    except Exception:
        future.cancel()
        raise SessionTransportError("Gateway control was not acknowledged. Refresh the conversation before retrying.") from None


def respond_clarify(session_id, clarify_id, answer):
    """None for local prompts; a remote prompt can never fall back to local settlement."""
    if not str(clarify_id).startswith("gw-session:"):
        return None
    with _LOCK:
        owner = next((r for r in _CONNECTIONS.values()
                      if r.session_id == session_id and clarify_id in r.clarifications), None)
    if owner is None:
        return False
    try:
        result = control(owner.run_id, "clarify", clarify_id=clarify_id, answer=answer)
    except ValueError:
        return False
    if not isinstance(result, dict) or result.get("status") not in ("ok", "resolved"):
        return False
    from api.clarify import resolve_clarify_by_id
    # Gateway owns the acknowledgement. Its settlement event can retire this
    # presentation mirror before the RPC returns; that is still success.
    resolve_clarify_by_id(session_id, clarify_id, answer)
    return True


def _verified_messaging_owner(home, display_id, owner_id, *, recovering=False):
    """Only the owning database's compression lineage may alias a display ID.

    A fresh turn must start from its live tip. A retained admission may keep its
    display segment after another compression; its canonical owner never changes.
    No sidecar hint grants this equivalence, and this read never opens a writable DB.
    """
    db = None
    try:
        from hermes_state import SessionDB
        path = Path(home) / "state.db"
        if not path.is_file():
            return False
        db = SessionDB(path, read_only=True)
        lineage = db.get_compression_lineage(display_id)
        if not lineage or lineage[0] != owner_id or display_id not in lineage:
            return False
        tip = db.get_compression_tip(owner_id)
        if tip != lineage[-1] or (not recovering and tip != display_id):
            return False
        rows = [db.get_session(sid) for sid in lineage]
        if any(not row or row.get("source") not in _MESSAGING_SOURCES for row in rows):
            return False
        route = rows[0].get("session_key")
        if not route or any(row.get("session_key") != route for row in rows):
            return False
        if any(row.get("profile_name") != rows[0].get("profile_name") for row in rows):
            return False
        last = rows[-1]
        if not recovering and (last.get("end_reason") not in (None, "", "idle_timeout")
                or (last.get("ended_at") is not None and last.get("end_reason") != "idle_timeout")):
            return False
        return db.get_compression_lineage(display_id) == lineage
    except Exception:
        return False
    finally:
        if db is not None:
            db.close()


class SessionRelay:
    def __init__(self, session_id, stream_id, home, request, emit, cancel, record, connector=None, *, recovering=False):
        self.session_id, self.stream_id, self.home = session_id, stream_id, home
        self.run_id = PREFIX + stream_id
        self.request, self.emit, self.cancel, self.record = request, emit, cancel, record
        self.canonical_session_id = request.get("session_id", session_id)
        self.client = None
        self.receipt = None
        self.loop = None
        self.epoch, self.sequence = None, -1
        self.approvals = {}
        self.clarifications = {}
        self.text = ""
        # A saved request may have been admitted before its acknowledgement was
        # persisted. Cancellation or discovery failure cannot declare it unsent.
        self.attempted = recovering
        self.snapshot = None
        self.connector = connector or (lambda: connect_local(self.home))

    async def status(self):
        self.receipt = await self.client.rpc("prompt.receipt", session_id=self.canonical_session_id,
                                            admission_id=self.receipt["admission_id"], include_result=True)
        return self.receipt

    async def control(self, action, **params):
        if self.client is None or self.receipt is None:
            raise SessionTransportError("Gateway session is reconnecting.")
        receipt = await self.status()
        generation = receipt.get("execution_generation")
        if action == "stop":
            if receipt["status"] == "queued":
                await self.client.rpc("prompt.cancel", session_id=self.canonical_session_id,
                                      admission_id=receipt["admission_id"])
            elif receipt["status"] == "started":
                await self.client.rpc("session.interrupt", session_id=self.canonical_session_id,
                                      execution_generation=generation)
            elif receipt["status"] != "terminal":
                raise SessionTransportError("Gateway turn outcome is unknown.")
            return True
        if action == "approval":
            prompt_id = params["approval_id"]
            expected = self.approvals.get(prompt_id)
            if expected is None or expected != generation or receipt["status"] != "started":
                raise SessionTransportError("This approval is no longer pending.")
            return await self.client.rpc("approval.respond", session_id=self.canonical_session_id,
                                         execution_generation=expected, prompt_id=prompt_id,
                                         choice=params["choice"])
        if action == "clarify":
            target = self.clarifications.get(params["clarify_id"])
            if target is None or receipt["status"] != "started":
                raise SessionTransportError("This clarification is no longer pending.")
            method, fields = target
            if "execution_generation" in fields and fields["execution_generation"] != generation:
                raise SessionTransportError("This clarification is no longer pending.")
            scope = {"session_id": self.canonical_session_id} if method == "clarify.respond" else {}
            return await self.client.rpc(method, **scope, **fields, answer=params["answer"])
        if action == "steer" and receipt["status"] == "started":
            return await self.client.rpc("session.steer", session_id=self.canonical_session_id,
                                         execution_generation=generation, text=params["text"])
        raise SessionTransportError("This Gateway control is unavailable for the current turn.")

    def clarify(self, payload, *, request_id=None):
        from api.clarify import submit_pending
        questions = payload.get("questions", []) if request_id else [payload]
        for question in questions:
            qid = question.get("qid") if request_id else payload.get("prompt_id")
            if not qid or qid in (payload.get("answers") or {}):
                continue
            local_id = f"gw-session:{self.stream_id}:{request_id or ''}:{qid}"
            if local_id in self.clarifications:
                continue
            target = (("clarify.lock", {"request_id": request_id, "question_id": qid}) if request_id else
                      ("clarify.respond", {"prompt_id": qid, "execution_generation": payload["execution_generation"]}))
            self.clarifications[local_id] = target
            submit_pending(self.session_id, {"clarify_id": local_id, "question": question.get("question", ""),
                "choices_offered": question.get("choices") or [], "multi_select": bool(question.get("multi_select"))})

    def withdraw_clarify(self, request_id):
        from api.clarify import resolve_clarify_by_id
        if not request_id:
            return
        for local_id, (_, fields) in list(self.clarifications.items()):
            if request_id in {fields.get("request_id"), fields.get("prompt_id")}:
                resolve_clarify_by_id(self.session_id, local_id, "")
                del self.clarifications[local_id]

    def approval(self, payload, generation):
        from api.gateway_chat import _gateway_runs_approval_event, _settle_gateway_run_approval
        prompt_id = payload.get("prompt_id")
        if not prompt_id or self.approvals.get(prompt_id) == generation:
            return
        self.approvals[prompt_id] = generation
        data = _gateway_runs_approval_event({**payload, "approval_id": prompt_id})
        if data is None:
            data = {"approval_id": prompt_id, "description": "Gateway tool approval", "choices": ["once", "deny"]}
        data.update(run_id=self.run_id, _gateway_agent_identity_v1=True)
        # Reuse the existing session-YOLO handoff and exact-owner mirror. This
        # runs off the socket loop because approval settlement can RPC back to it.
        settled, head, count = _settle_gateway_run_approval(self.session_id, data, "", "")
        if not settled:
            self.emit("approval", {**(head or data), "pending_count": count})

    async def receive_event(self, frame):
        if frame.get("type") == "approval.request":
            await asyncio.to_thread(self.event, frame)
        else:
            self.event(frame)

    def event(self, frame):
        from api.gateway_chat import _append_gateway_stream_text, _note_live_gateway_event
        if frame.get("session_id") != self.canonical_session_id:
            return
        seq, epoch = frame.get("seq"), frame.get("replay_epoch")
        if isinstance(seq, int):
            if epoch == self.epoch and seq <= self.sequence:
                return
            self.epoch, self.sequence = epoch, seq
        if frame.get("type") == "request.cancel":
            self.withdraw_clarify((frame.get("payload") or {}).get("id"))
            return
        if frame.get("admission_id") != (self.receipt or {}).get("admission_id"):
            return
        kind, payload = frame.get("type"), frame.get("payload") or {}
        if kind == "message.delta":
            text = payload.get("text") or payload.get("delta") or ""
            if isinstance(text, str) and text:
                self.text = _append_gateway_stream_text(self.stream_id, self.text, text)
                self.emit("token", {"text": text})
        elif kind in {"reasoning.delta", "thinking.delta"}:
            data = {"text": payload.get("text") or payload.get("delta") or ""}
            _note_live_gateway_event(self.stream_id, "reasoning", data)
            self.emit("reasoning", data)
        elif kind in {"tool.start", "tool.complete"}:
            name = "tool" if kind == "tool.start" else "tool_complete"
            data = {"name": payload.get("name") or payload.get("tool"),
                    "tid": payload.get("tool_call_id") or payload.get("id"),
                    "args": payload.get("args") or {},
                    "is_error": bool(payload.get("is_error", payload.get("error", False)))}
            _note_live_gateway_event(self.stream_id, name, data)
            self.emit(name, data)
        elif kind == "approval.request":
            self.approval(payload, frame.get("execution_generation"))
        elif kind == "clarify.request":
            self.clarify({**payload, "execution_generation": frame.get("execution_generation")})
        elif kind == "clarify.settled":
            self.withdraw_clarify(payload.get("prompt_id"))
        elif kind == "approval.settled":
            from api.route_approvals import retire_gateway_pending_mirror
            prompt_id = payload.get("prompt_id")
            self.approvals.pop(prompt_id, None)
            retire_gateway_pending_mirror(self.session_id, run_id=self.run_id, approval_id=prompt_id)

    async def run(self):
        from api.gateway_chat import _adopt_gateway_stream_text, _publish_gateway_run_id
        from hermes_cli.gateway_client import GatewayRPCError
        self.loop = asyncio.get_running_loop()
        failures = 0
        while True:
            attempted_before_connect = self.attempted
            terminal_outcome = False
            try:
                async with self.connector() as client:
                    self.client = client
                    snapshot = await client.rpc("session.resume", session_id=self.canonical_session_id, display_projection=True)
                    self.snapshot = snapshot
                    owner_id = snapshot["session_id"]
                    if self.attempted and owner_id != self.canonical_session_id:
                        raise SessionTransportError("Gateway session owner changed; refusing to replay this turn.")
                    if owner_id != self.session_id and not await asyncio.to_thread(
                        _verified_messaging_owner, self.home, self.session_id, owner_id, recovering=self.attempted,
                    ):
                        raise SessionTransportError("This conversation has moved to a continuation. Open that conversation first.")
                    self.canonical_session_id = owner_id
                    self.request = {**self.request, "session_id": owner_id}
                    if self.cancel.is_set() and not self.attempted:
                        return None, {}
                    # Resolve identity first, then journal the exact canonical request
                    # before prompt.submit. Never replay under a different owner.
                    self.record(self.run_id if self.receipt else "", self.request)
                    # Retrying the same input_id returns the durable original admission.
                    self.attempted = True
                    self.receipt = await client.rpc("prompt.submit", **self.request)
                    self.record(self.run_id, self.request)
                    with _LOCK:
                        _CONNECTIONS[self.run_id] = self
                    _publish_gateway_run_id(self.stream_id, self.run_id)
                    if self.epoch is not None:
                        replay = await client.rpc("session.events.since", session_id=self.canonical_session_id,
                                                  replay_epoch=self.epoch, last_sequence=self.sequence)
                        for frame in replay.get("events", []):
                            await self.receive_event(frame)
                    for prompt in snapshot.get("prompts", []):
                        if self.receipt.get("status") != "started" or prompt.get("execution_generation") != self.receipt.get("execution_generation"):
                            continue
                        if prompt.get("kind") == "clarify":
                            self.clarify(prompt)
                        else:
                            await asyncio.to_thread(self.approval, prompt, prompt.get("execution_generation"))
                    for request in snapshot.get("open_requests", []):
                        if self.receipt.get("status") == "started" and request.get("method") == "clarify":
                            self.clarify(request.get("params") or {}, request_id=request["id"])
                    while True:
                        receipt = await self.status()
                        if receipt["status"] == "terminal":
                            terminal_outcome = True
                            if receipt.get("outcome") in {"cancelled", "interrupted"}:
                                self.emit("cancel", {"message": "Cancelled by user"})
                                return None, {}
                            result = receipt.get("result") or {}
                            if receipt.get("outcome") != "completed" or result.get("failed") or result.get("error"):
                                raise SessionTransportError("Gateway turn failed. Open the conversation in its original client for details.")
                            text = result.get("final_response")
                            if not isinstance(text, str):
                                raise SessionTransportError("Gateway did not return a durable completion receipt.")
                            self.snapshot = await client.rpc("session.resume", session_id=self.canonical_session_id, display_projection=True)
                            return _adopt_gateway_stream_text(self.stream_id, text), receipt.get("usage") or {}
                        if receipt["status"] == "unknown":
                            raise SessionTransportDisconnected("Gateway turn outcome is unknown. Do not resend the message.")
                        if self.cancel.is_set():
                            await self.control("stop")
                        # A short receipt poll recovers dropped terminal frames. Between
                        # polls, consume the actual streaming events without blocking RPCs.
                        deadline = self.loop.time() + 0.5
                        while self.loop.time() < deadline:
                            try:
                                frame = await asyncio.wait_for(client.events.get(), deadline - self.loop.time())
                            except asyncio.TimeoutError:
                                break
                            if isinstance(frame, Exception):
                                raise frame
                            if frame.get("method") == "event":
                                await self.receive_event(frame.get("params") or {})
                            elif frame.get("method") == "clarify" and (frame.get("params") or {}).get("session_id") == self.canonical_session_id:
                                # A queued admission must not answer another client's
                                # active turn. Refresh before projecting a server request.
                                if (await self.status()).get("status") == "started":
                                    self.clarify(frame["params"], request_id=frame["id"])
            except GatewayRPCError as exc:
                if terminal_outcome or (not attempted_before_connect and self.receipt is None):
                    code = str(exc)
                    suffix = f": {code}" if re.fullmatch(r"[a-z_]{1,80}", code) else "."
                    raise SessionTransportError("Gateway refused the session operation" + suffix) from None
                # A refusal while reconnecting does not settle an earlier
                # uncertain submission. Keep its input ID and worker ownership.
            except SessionTransportError:
                if terminal_outcome or not self.attempted:
                    raise
            except Exception:
                pass
            finally:
                self.client = None
                from api.clarify import resolve_clarify_by_id
                for local_id in self.clarifications:
                    resolve_clarify_by_id(self.session_id, local_id, "")
                self.clarifications.clear()
                with _LOCK:
                    if _CONNECTIONS.get(self.run_id) is self:
                        del _CONNECTIONS[self.run_id]
            # Retain the existing WebUI stream/worker while admission is unknown.
            # Teardown would let stale-state repair erase the durable request and
            # admit another turn. Reopening can attach to this same live worker;
            # process restart uses the same persisted input ID through reattach.
            failures += 1
            from api.config import update_active_run
            update_active_run(self.stream_id, phase="gateway-reconnecting")
            await asyncio.sleep(min(failures * 0.5, 5))


def run_session(session, stream_id, text, *, emit, cancel, record, saved=None):
    from api.profiles import get_hermes_home_for_profile, _is_root_profile
    home = str(Path(get_hermes_home_for_profile(session.profile)).resolve())
    if saved and saved.get("profile_home") != home:
        raise SessionTransportError("Gateway profile changed; refusing to replay this turn.")
    config = config_for_session({"profile": session.profile})
    mode = config.get("webui_gateway_session_transport")
    binding = {"mode": mode}
    connector = None
    if mode == "unix":
        from api.gateway_session_bridge import connect_bridge
        socket_path = config.get("webui_gateway_session_socket")
        if not isinstance(socket_path, str) or not Path(socket_path).is_absolute():
            raise SessionTransportError("Configure an absolute private Gateway session socket path.")
        profile = "default" if not session.profile or _is_root_profile(session.profile) else session.profile
        binding.update(socket=socket_path, profile=profile)
        connector = lambda: connect_bridge(socket_path, profile)
    elif mode != "local":
        raise SessionTransportError("Gateway session transport is not configured.")
    if saved and saved.get("session_connection", {"mode": "local"}) != binding:
        raise SessionTransportError("Gateway connection changed; refusing to replay this turn.")
    request = (saved or {}).get("session_request") or {
        "session_id": session.session_id, "input_id": "webui:" + stream_id, "text": text,
    }
    def persist(run_id, request):
        record(run_id, request, transport="session", session_request=request, profile_home=home,
               session_connection=binding)
    relay = SessionRelay(session.session_id, stream_id, home, request, emit, cancel, persist, connector,
                         recovering=bool(saved))
    text, usage = asyncio.run(relay.run())
    return text, usage, relay.snapshot
