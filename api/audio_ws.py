"""
Audio WebSocket Server — bidirectional voice streaming for the WebUI.

Runs alongside the HTTP server and provides a WebSocket endpoint that
plugs into the agent's configured TTS/STT providers directly, without
requiring a separate Fun-Audio-Chat (FAC) server.

State machine (adapted from gateway patterns):
    IDLE ──start──▶ LISTENING ──stop──▶ TRANSCRIBING ──▶ RESPONDING ──▶ SPEAKING ──▶ IDLE
                      │                   │                  │              │
                      └──audio chunks──▶   └──transcript──▶  └──text────▶  └──audio──▶
                                                                             chunks

Protocol
--------
Client → Server (text, JSON):
    {cmd: "start"}                      — begin listening, binary frames are audio chunks
    {cmd: "stop"}                       — end listening, transcribe accumulated audio
    {cmd: "text", text: "..."}          — send text for TTS (skip STT)
    {cmd: "set_voice", voice: "..."}    — switch TTS voice
    {cmd: "cancel"}                     — abort current operation
    {cmd: "ping"}                       — keepalive

Client → Server (binary):  Opus audio chunks (from MediaRecorder, webm container)

Server → Client (text, JSON):
    {type: "state", state: "..."}       — state transition
    {type: "transcript", text: "..."}   — STT result
    {type: "tts_begin", format: "..."}  — TTS output starting
    {type: "log", text: "..."}          — diagnostic message
    {type: "error", message: "..."}     — error
    {type: "done"}                      — operation complete
    {type: "pong"}                      — keepalive response

Server → Client (binary):  Opus/WAV audio chunks (TTS output)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class AudioState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    RESPONDING = "responding"
    SPEAKING = "speaking"


_STATE_TRANSITIONS: dict[AudioState, set[AudioState]] = {
    AudioState.IDLE:         {AudioState.LISTENING},
    AudioState.LISTENING:    {AudioState.IDLE, AudioState.TRANSCRIBING},
    AudioState.TRANSCRIBING: {AudioState.IDLE, AudioState.RESPONDING},
    AudioState.RESPONDING:   {AudioState.IDLE, AudioState.SPEAKING},
    AudioState.SPEAKING:     {AudioState.IDLE},
}


def _can_transition(from_state: AudioState, to_state: AudioState) -> bool:
    """Check if a state transition is valid."""
    return to_state in _STATE_TRANSITIONS.get(from_state, set())


# ---------------------------------------------------------------------------
# Audio Session — one per WebSocket connection
# ---------------------------------------------------------------------------

class AudioSession:
    """Manages the voice state machine + audio buffering for one client."""

    def __init__(self, websocket: WebSocketServerProtocol):
        self.ws = websocket
        self.state = AudioState.IDLE
        self.audio_buffer: list[bytes] = []       # raw Opus chunks
        self.voice: Optional[str] = None           # configured TTS voice
        self._cancel_event = asyncio.Event()
        self._lock = asyncio.Lock()

    # ── State management ────────────────────────────────────────────────

    async def _set_state(self, new_state: AudioState) -> None:
        """Transition state and notify the client."""
        if not _can_transition(self.state, new_state):
            logger.debug(
                "AudioWS: invalid transition %s -> %s",
                self.state.value, new_state.value,
            )
            return
        self.state = new_state
        await self._send({"type": "state", "state": new_state.value})

    async def _send(self, data: dict) -> None:
        """Send a JSON text frame to the client."""
        try:
            await self.ws.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _send_log(self, text: str) -> None:
        """Send a log message to the client."""
        await self._send({"type": "log", "text": text})

    # ── Command handlers ────────────────────────────────────────────────

    async def handle_command(self, data: dict) -> None:
        """Process a text command from the client."""
        cmd = data.get("cmd", "")

        if cmd == "start":
            await self._cmd_start()
        elif cmd == "stop":
            await self._cmd_stop()
        elif cmd == "text":
            text = data.get("text", "")
            if text.strip():
                await self._cmd_text(text)
        elif cmd == "set_voice":
            self.voice = data.get("voice", "")
            await self._send_log(f"Voice set to: {self.voice}")
        elif cmd == "cancel":
            await self._cmd_cancel()
        elif cmd == "ping":
            await self._send({"type": "pong"})
        else:
            await self._send({"type": "error", "message": f"Unknown command: {cmd}"})

    async def handle_audio(self, data: bytes) -> None:
        """Buffer an incoming audio chunk (binary frame)."""
        if self.state != AudioState.LISTENING:
            return  # drop audio if not listening
        self.audio_buffer.append(data)

    async def _cmd_start(self) -> None:
        """Begin listening — accept audio chunks."""
        if self.state != AudioState.IDLE:
            await self._send_log("Already active. Cancel first.")
            return
        self.audio_buffer.clear()
        self._cancel_event.clear()
        await self._set_state(AudioState.LISTENING)
        await self._send_log("Listening...")

    async def _cmd_stop(self) -> None:
        """Stop listening and transcribe accumulated audio."""
        if self.state != AudioState.LISTENING:
            await self._send_log("Not currently listening.")
            return

        await self._set_state(AudioState.TRANSCRIBING)
        self._cancel_event.clear()

        if not self.audio_buffer:
            await self._send_log("No audio received.")
            await self._set_state(AudioState.IDLE)
            return

        # Concatenate audio chunks and transcribe
        await self._send_log(f"Transcribing {len(self.audio_buffer)} chunk(s)...")
        try:
            transcript = await self._transcribe()
            if self._cancel_event.is_set():
                return
            await self._send({
                "type": "transcript",
                "text": transcript,
                "source": "stt",
            })
        except Exception as exc:
            logger.exception("AudioWS: transcription failed")
            await self._send({"type": "error", "message": f"STT failed: {exc}"})

        await self._set_state(AudioState.IDLE)
        await self._send({"type": "done"})

    async def _cmd_text(self, text: str) -> None:
        """Synthesize text to speech and stream back audio."""
        await self._set_state(AudioState.RESPONDING)
        self._cancel_event.clear()
        await self._send_log(f"Synthesizing: {text[:80]}...")

        try:
            await self._synthesize_and_stream(text)
        except Exception as exc:
            logger.exception("AudioWS: TTS failed")
            await self._send({"type": "error", "message": f"TTS failed: {exc}"})
        finally:
            await self._set_state(AudioState.IDLE)
            if not self._cancel_event.is_set():
                await self._send({"type": "done"})

    async def _cmd_cancel(self) -> None:
        """Cancel any in-progress operation."""
        self._cancel_event.set()
        self.audio_buffer.clear()
        await self._set_state(AudioState.IDLE)
        await self._send_log("Cancelled.")

    # ── STT ─────────────────────────────────────────────────────────────

    async def _transcribe(self) -> str:
        """Concatenate buffered Opus chunks, write to temp file, transcribe.

        Runs the potentially-blocking STT call in a thread so the asyncio
        event loop stays responsive.
        """
        if not self.audio_buffer:
            return ""

        # Write concatenated audio to a temp file
        fd, tmp_path = tempfile.mkstemp(suffix=".webm", prefix="hws_")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in self.audio_buffer:
                    f.write(chunk)

            # Run transcription in a thread (avoids blocking the WS loop)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _do_transcribe, tmp_path)

            if self._cancel_event.is_set():
                return ""

            if result.get("success"):
                return result.get("transcript", "")
            else:
                error = result.get("error", "Unknown STT error")
                raise RuntimeError(error)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ── TTS ─────────────────────────────────────────────────────────────

    async def _synthesize_and_stream(self, text: str) -> None:
        """Synthesize text to speech and stream audio back in chunks.

        Uses the configured TTS provider via text_to_speech_tool().
        The audio file is read and sent as binary frames.
        """
        if not text.strip():
            return

        # Synthesize to temp file (in a thread)
        fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="hws_tts_")
        os.close(fd)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, _do_tts_synthesize, text, tmp_path,
            )

            if self._cancel_event.is_set():
                return

            if not result.get("success"):
                error = result.get("error", "Unknown TTS error")
                raise RuntimeError(error)

            # Read the output file and stream it as binary chunks
            file_path = result.get("file_path") or tmp_path
            output_format = _detect_audio_format(file_path)

            await self._send({
                "type": "tts_begin",
                "format": output_format,
            })

            CHUNK_SIZE = 8192  # 8 KB chunks
            with open(file_path, "rb") as f:
                while True:
                    if self._cancel_event.is_set():
                        break
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    await self.ws.send(chunk)

                    # Yield to event loop between chunks so cancellation
                    # and other events can be processed promptly.
                    await asyncio.sleep(0)

            if not self._cancel_event.is_set():
                await self._send({"type": "tts_end"})

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Synchronous helpers (run in thread pool to avoid blocking the WS loop)
# ---------------------------------------------------------------------------

def _do_transcribe(file_path: str) -> dict:
    """Call the agent's transcription tool synchronously."""
    # Import lazily — agent modules may not be loaded yet
    from tools.transcription_tools import transcribe_audio
    return transcribe_audio(file_path)


def _do_tts_synthesize(text: str, output_path: str) -> dict:
    """Call the agent's TTS tool synchronously."""
    from tools.tts_tool import text_to_speech_tool
    result_str = text_to_speech_tool(text=text, output_path=output_path)
    # text_to_speech_tool returns a JSON string
    try:
        return json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        # Fallback: if it returned a file path directly
        if isinstance(result_str, str) and os.path.exists(result_str):
            return {"success": True, "file_path": result_str}
        return {"success": False, "error": f"TTS returned unexpected: {result_str[:200]}"}


def _detect_audio_format(file_path: str) -> str:
    """Detect audio format from file extension."""
    ext = Path(file_path).suffix.lower()
    fmt_map = {
        ".mp3": "mp3",
        ".wav": "wav",
        ".ogg": "ogg",
        ".opus": "opus",
        ".flac": "flac",
        ".m4a": "m4a",
    }
    return fmt_map.get(ext, "mp3")


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def audio_handler(websocket: WebSocketServerProtocol) -> None:
    """Handle a single WebSocket audio session."""
    session = AudioSession(websocket)
    await session._send_log("Audio WebSocket connected")
    await session._send({
        "type": "state",
        "state": AudioState.IDLE.value,
        "protocol_version": 1,
    })

    try:
        async for message in websocket:
            if isinstance(message, str):
                # Text frame — JSON command
                try:
                    data = json.loads(message)
                    await session.handle_command(data)
                except json.JSONDecodeError:
                    await session._send({
                        "type": "error",
                        "message": "Invalid JSON command",
                    })
            elif isinstance(message, bytes):
                # Binary frame — audio chunk
                await session.handle_audio(message)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # Cleanup — cancel any in-progress operation
        session._cancel_event.set()


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

_server: Optional[asyncio.AbstractServer] = None
# websockets.serve returns websockets.server.Server — a superset of AbstractServer
_ws_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_connections: Set[WebSocketServerProtocol] = set()


async def _on_connect(websocket: WebSocketServerProtocol) -> None:
    """Register connection and delegate to handler."""
    _connections.add(websocket)
    try:
        await audio_handler(websocket)
    finally:
        _connections.discard(websocket)


def start_audio_ws(host: str, port: int) -> None:
    """Start the audio WebSocket server in a background daemon thread.

    Called from server.py main().
    """
    global _server, _ws_thread, _loop

    async def _serve():
        global _server
        _server = await websockets.serve(
            _on_connect,
            host,
            port,
            ping_interval=30,
            ping_timeout=10,
            max_size=2**24,  # 16 MB max message (audio chunks)
        )
        logger.info("Audio WebSocket listening on ws://%s:%d", host, port)
        await _server.wait_closed()

    def _run():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(_serve())
        except Exception:
            logger.exception("Audio WebSocket server failed")

    _ws_thread = threading.Thread(target=_run, daemon=True, name="audio-ws")
    _ws_thread.start()


def stop_audio_ws() -> None:
    """Shut down the audio WebSocket server gracefully."""
    global _server, _loop, _ws_thread

    if _server:
        # Close all active connections
        for conn in list(_connections):
            loop = getattr(conn, "_loop", _loop)
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    conn.close(1011, "Server shutting down"), loop
                )
        _connections.clear()

    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(lambda: _server.close() if _server else None)

    _ws_thread = None
    _server = None
    _loop = None
