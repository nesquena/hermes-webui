"""Hermes Web UI server entry point."""
import logging
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Ignore SIGPIPE so a dropped client only aborts that write, not the whole WebUI process.
_SIGPIPE = getattr(signal, "SIGPIPE", None)
if _SIGPIPE is not None:
    signal.signal(_SIGPIPE, signal.SIG_IGN)

# Test-mode network isolation keeps subprocess-backed tests hermetic.
if os.environ.get("HERMES_WEBUI_TEST_NETWORK_BLOCK", "").strip() in ("1", "true", "yes"):
    _REAL_CREATE_CONN = socket.create_connection
    _REAL_SOCK_CONNECT = socket.socket.connect

    import re as _re

    def _re_match_unique_local_ipv6(h):
        """Match IPv6 fc00::/7 without catching similar-looking hostnames."""
        return bool(_re.match(r"^f[cd][0-9a-f]{0,2}:", h))

    def _addr_is_local(host):
        if not isinstance(host, str):
            return False
        h = host.strip().lower()
        if not h:
            return False
        if h in ("::1", "0:0:0:0:0:0:0:1") or h.startswith("fe80:") or _re_match_unique_local_ipv6(h):
            return True
        if h == "localhost" or h.endswith(".localhost"):
            return True
        if h.endswith(".local") or h.endswith(".test") or h.endswith(".invalid"):
            return True
        if h == "example.com" or h.endswith(".example.com"):
            return True
        if h == "example.net" or h.endswith(".example.net"):
            return True
        if h == "example.org" or h.endswith(".example.org"):
            return True
        if h.endswith(".example"):
            return True
        if h and h[0].isdigit() and h.count(".") == 3:
            try:
                o1, o2, o3, o4 = [int(p) for p in h.split(".")]
            except ValueError:
                return False
            if o1 == 127:
                return True
            if o1 == 10:
                return True
            if o1 == 192 and o2 == 168:
                return True
            if o1 == 172 and 16 <= o2 <= 31:
                return True
            if o1 == 169 and o2 == 254:
                return True
            if o1 == 203 and o2 == 0 and o3 == 113:
                return True
        return False

    def _blocked_create_connection(address, *a, **kw):
        try:
            host = address[0]
        except (TypeError, IndexError):
            host = ""
        if _addr_is_local(host):
            return _REAL_CREATE_CONN(address, *a, **kw)
        raise OSError(
            f"hermes test network isolation (server.py): outbound to {address!r} blocked"
        )

    def _blocked_socket_connect(self, address):
        try:
            host = address[0]
        except (TypeError, IndexError):
            host = ""
        if _addr_is_local(host):
            return _REAL_SOCK_CONNECT(self, address)
        raise OSError(
            f"hermes test network isolation (server.py): socket.connect to {address!r} blocked"
        )

    socket.create_connection = _blocked_create_connection
    socket.socket.connect = _blocked_socket_connect


try:
    import resource
except ImportError:  # pragma: no cover - resource is Unix-only
    resource = None
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from api.auth import check_auth
from api.config import HOST, PORT, STATE_DIR, SESSION_DIR, DEFAULT_WORKSPACE
from api.helpers import (
    j,
    get_profile_cookie,
    _build_csp_report_only_policy,
    _CLIENT_DISCONNECT_ERRORS,
)
from api.profiles import set_request_profile, clear_request_profile
from api.routes import handle_delete, handle_get, handle_patch, handle_post, handle_put
from api.startup import auto_install_agent_deps, fix_credential_permissions
from api.updates import WEBUI_VERSION


class QuietHTTPServer(ThreadingHTTPServer):
    """Custom HTTP server that silently handles common network errors."""
    daemon_threads = True
    request_queue_size = 64
    max_request_workers = 128
    max_overflow_reject_workers = 16
    _OVERFLOW_RESPONSE = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Connection: close\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )

    def __init__(self, *args, **kwargs):
        server_address = args[0] if args else kwargs.get('server_address', None)
        if server_address and ':' in server_address[0]:
            self.address_family = socket.AF_INET6
        self.ssl_context: object | None = None
        super().__init__(*args, **kwargs)
        self._request_worker_slots = threading.BoundedSemaphore(self.max_request_workers)
        self._overflow_reject_slots = threading.BoundedSemaphore(self.max_overflow_reject_workers)
        self.accept_loop_requests_total = 0
        self.accept_loop_last_request_at = 0.0

    def server_bind(self):
        if sys.platform == 'win32':
            self.allow_reuse_address = False
            SO_EXCLUSIVEADDRUSE = getattr(socket, 'SO_EXCLUSIVEADDRUSE', -5)
            self.socket.setsockopt(socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)
            # Retry bind on Windows to handle the case where a previous
            # process (e.g. during self-update) is still releasing the port.
            # The old process calls os._exit(0) which starts tearing down
            # its socket, but with SO_EXCLUSIVEADDRUSE the OS blocks new
            # binds until the teardown completes.  Retry for up to 10 s.
            max_retries = 20
            retry_delay = 0.5
            for attempt in range(max_retries):
                try:
                    super().server_bind()
                    return
                except OSError as e:
                    if e.winerror == 10048 and attempt < max_retries - 1:  # WSAEADDRINUSE
                        time.sleep(retry_delay)
                    else:
                        raise
        else:
            super().server_bind()

    def get_request(self):
        """Accept raw sockets and defer TLS handshake work to request threads."""
        request, client_address = self.socket.accept()
        ssl_context = getattr(self, "ssl_context", None)
        if ssl_context is None:
            return request, client_address
        try:
            tls_request = ssl_context.wrap_socket(
                request,
                server_side=True,
                do_handshake_on_connect=False,
            )
        except Exception:
            request.close()
            raise
        return tls_request, client_address

    def _handle_request_noblock(self):
        """Record accept-loop progress before dispatching a request handler."""
        self.accept_loop_requests_total += 1
        self.accept_loop_last_request_at = time.time()
        return super()._handle_request_noblock()

    def _close_request_quietly(self, request) -> None:
        try:
            request.close()
        except Exception:
            pass

    def _drain_request_input_nonblocking(self, request) -> None:
        # Read through the current header block before replying so Windows
        # doesn't reset the socket when we close with unread input.
        deadline = time.monotonic() + 0.05
        buffered = bytearray()
        header_terminator = b"\r\n\r\n"
        max_bytes = 65536
        try:
            timeout = request.gettimeout()
        except Exception:
            timeout = None
        try:
            while len(buffered) < max_bytes and header_terminator not in buffered:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                try:
                    request.settimeout(wait)
                    chunk = request.recv(min(4096, max_bytes - len(buffered)))
                except (BlockingIOError, InterruptedError, TimeoutError, socket.timeout):
                    break
                except OSError:
                    break
                if not chunk:
                    break
                buffered.extend(chunk)
            try:
                request.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        finally:
            try:
                request.settimeout(timeout)
            except Exception:
                pass

    def _reject_overflow_request(self, request) -> None:
        if getattr(self, "ssl_context", None) is not None:
            self._close_request_quietly(request)
            return
        if not self._overflow_reject_slots.acquire(blocking=False):
            self._close_request_quietly(request)
            return
        try:
            threading.Thread(
                target=self._reject_overflow_request_worker,
                args=(request,),
                daemon=True,
            ).start()
        except Exception:
            self._overflow_reject_slots.release()
            self._close_request_quietly(request)

    def _reject_overflow_request_worker(self, request) -> None:
        try:
            self._drain_request_input_nonblocking(request)
            try:
                request.sendall(self._OVERFLOW_RESPONSE)
                try:
                    request.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
            except Exception:
                pass
        finally:
            self._close_request_quietly(request)
            self._overflow_reject_slots.release()

    def process_request(self, request, client_address):
        if not self._request_worker_slots.acquire(blocking=False):
            self._reject_overflow_request(request)
            return
        try:
            return super().process_request(request, client_address)
        except Exception:
            self._request_worker_slots.release()
            self._close_request_quietly(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            return super().process_request_thread(request, client_address)
        finally:
            self._request_worker_slots.release()

    def handle_error(self, request, client_address):
        """Suppress logging for common client disconnect errors."""
        exc_type, exc_value, _ = sys.exc_info()
        if exc_type in (
            ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
            TimeoutError, ssl.SSLError, ssl.SSLEOFError,
        ):
            return
        if issubclass(exc_type, OSError):
            if getattr(exc_value, 'errno', None) in (32, 54, 104, 110):  # EPIPE, ECONNRESET, ETIMEDOUT
                return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive stays on, so every response must declare framing.
    protocol_version = "HTTP/1.1"
    timeout = 30  # seconds — kills idle/incomplete connections to prevent thread exhaustion
    
    def setup(self):
        """Set socket options for each accepted connection."""
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        if hasattr(socket, 'TCP_KEEPIDLE'):  # Linux
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except OSError:
                pass
        elif hasattr(socket, 'TCP_KEEPALIVE'):  # macOS
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 10)
            except OSError:
                pass
    _ver_suffix = WEBUI_VERSION.removeprefix('v')
    server_version = ('HermesWebUI/' + _ver_suffix) if _ver_suffix != 'unknown' else 'HermesWebUI'
    _CSP_REPORT_TO = '{"group":"csp-endpoint","max_age":10886400,"endpoints":[{"url":"/api/csp-report"}]}'

    @classmethod
    def csp_report_only_policy(cls, extra_connect_src=None, extra_frame_src=None) -> str:
        return _build_csp_report_only_policy(extra_connect_src, extra_frame_src)

    def end_headers(self) -> None:
        extra_connect_src = getattr(self, "_csp_extra_connect_src", None)
        extra_frame_src = getattr(self, "_csp_extra_frame_src", None)
        self.send_header("Content-Security-Policy-Report-Only", self.csp_report_only_policy(extra_connect_src, extra_frame_src))
        self.send_header("Report-To", self._CSP_REPORT_TO)
        super().end_headers()

    def log_message(self, fmt, *args): pass  # suppress default Apache-style log

    @staticmethod
    def _safe_webui_print(message: str) -> None:
        """Emit a request log line without letting logging break responses."""
        try:
            print(message, flush=True)
        except Exception:
            pass

    def log_request(self, code: str='-', size: str='-') -> None:
        """Structured JSON logs for each request."""
        import json as _json
        duration_ms = round((time.time() - getattr(self, '_req_t0', time.time())) * 1000, 1)
        remote = '-'
        try:
            if getattr(self, 'client_address', None):
                remote = str(self.client_address[0])
        except Exception:
            remote = '-'
        forwarded_for = None
        try:
            forwarded_for = (self.headers.get('X-Forwarded-For') or '').split(',')[0].strip() or None
        except Exception:
            forwarded_for = None
        record_data = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'remote': remote,
            'method': getattr(self, 'command', None) or '-',
            'path': getattr(self, 'path', None) or '-',
            'status': int(code) if str(code).isdigit() else code,
            'ms': duration_ms,
        }
        if forwarded_for:
            record_data['forwarded_for'] = forwarded_for
        record = _json.dumps(record_data)
        self._safe_webui_print(f'[webui] {record}')

    def do_GET(self) -> None:
        self._req_t0 = time.time()
        cookie_profile = get_profile_cookie(self)
        if cookie_profile:
            set_request_profile(cookie_profile)
        try:
            parsed = urlparse(self.path)
            if not check_auth(self, parsed): return
            result = handle_get(self, parsed)
            if result is False:
                return j(self, {'error': 'not found'}, status=404)
        except _CLIENT_DISCONNECT_ERRORS:
            # Expected disconnect path; do not convert it into a misleading server 500.
            return
        except Exception:
            self._safe_webui_print(f'[webui] ERROR {self.command} {self.path}\n' + traceback.format_exc())
            try:
                j(self, {'error': 'Internal server error'}, status=500)
            except _CLIENT_DISCONNECT_ERRORS:
                pass
            except Exception:
                self._safe_webui_print(traceback.format_exc())
        finally:
            clear_request_profile()

    def _handle_write(self, route_func) -> None:
        self._req_t0 = time.time()
        cookie_profile = get_profile_cookie(self)
        if cookie_profile:
            set_request_profile(cookie_profile)
        try:
            parsed = urlparse(self.path)
            _is_csp_report_post = (
                parsed.path == "/api/csp-report" and self.command == "POST"
            )
            if not _is_csp_report_post and not check_auth(self, parsed): return
            result = route_func(self, parsed)
            if result is False:
                return j(self, {'error': 'not found'}, status=404)
        except _CLIENT_DISCONNECT_ERRORS:
            # Expected disconnect path; do not convert it into a misleading server 500.
            return
        except Exception:
            self._safe_webui_print(f'[webui] ERROR {self.command} {self.path}\n' + traceback.format_exc())
            try:
                j(self, {'error': 'Internal server error'}, status=500)
            except _CLIENT_DISCONNECT_ERRORS:
                pass
            except Exception:
                self._safe_webui_print(traceback.format_exc())
        finally:
            clear_request_profile()

    def do_POST(self) -> None:
        self._handle_write(handle_post)

    def do_PUT(self) -> None:
        self._handle_write(handle_put)

    def do_PATCH(self) -> None:
        self._handle_write(handle_patch)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self._req_t0 = time.time()
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_DELETE(self) -> None:
        self._handle_write(handle_delete)


def _raise_fd_soft_limit(target: int = 4096) -> dict:
    """Best-effort raise of RLIMIT_NOFILE for persistent WebUI hosts."""
    if resource is None:
        return {"status": "unsupported"}
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    desired = int(target)
    if hard not in (-1, getattr(resource, "RLIM_INFINITY", object())):
        desired = min(desired, int(hard))
    if soft >= desired:
        return {"status": "unchanged", "soft": soft, "hard": hard}
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    except Exception as exc:
        return {"status": "error", "soft": soft, "hard": hard, "error": str(exc)}
    return {"status": "raised", "soft": desired, "hard": hard, "previous_soft": soft}


_SHUTDOWN_AUDIT_LOGGED = False
_SHUTDOWN_LOG_VALUE_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _shutdown_log_value(value, *, default: str = "unknown", max_len: int = 160) -> str:
    """Return a bounded single-line value safe for shutdown diagnostics."""
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    text = _SHUTDOWN_LOG_VALUE_RE.sub("?", text).strip()
    if not text:
        return default
    if len(text) > max_len:
        text = f"{text[:max_len]}…"
    return text


def _log_shutdown_audit(reason: str = "serve_forever_exit") -> None:
    """Log runtime context when the WebUI server is exiting."""
    global _SHUTDOWN_AUDIT_LOGGED
    if _SHUTDOWN_AUDIT_LOGGED:
        return

    active_sessions = []
    try:
        from api.models import LOCK, SESSIONS
        with LOCK:
            session_items = list(SESSIONS.items())
        for sid, session in session_items:
            stream_id = getattr(session, "active_stream_id", None)
            if stream_id:
                pending = bool(getattr(session, "pending_user_message", None))
                active_sessions.append(
                    "sid=%s stream=%s pending=%s"
                    % (
                        _shutdown_log_value(sid),
                        _shutdown_log_value(stream_id),
                        pending,
                    )
                )
    except Exception:
        logger.debug("Failed to collect active-session shutdown audit state", exc_info=True)

    _SHUTDOWN_AUDIT_LOGGED = True
    logger.info(
        "[shutdown-audit] reason=%s pid=%s thread=%s(%s) active_sessions=[%s]",
        _shutdown_log_value(reason),
        os.getpid(),
        _shutdown_log_value(threading.current_thread().name),
        threading.current_thread().ident,
        "; ".join(active_sessions) if active_sessions else "none",
    )


def _abort_if_already_serving(host: str, port: int) -> None:
    """Refuse to start if a live HTTP server is already responding on this port."""
    probe_host = '127.0.0.1' if host in ('0.0.0.0', '', '::') else host
    try:
        with socket.create_connection((probe_host, port), timeout=2) as s:
            s.sendall(b'GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n')
            s.settimeout(2)
            data = s.recv(512)
            if data:
                print(
                    f'[!!] FATAL: Another server is already responding on'
                    f' {probe_host}:{port}. Stop the existing instance first.',
                    flush=True,
                )
                sys.exit(1)
    except (ConnectionRefusedError, ConnectionResetError, OSError, socket.timeout):
        pass


def main() -> None:
    from api.config import print_startup_config, verify_hermes_imports, _HERMES_FOUND

    print_startup_config()

    fd_limit = _raise_fd_soft_limit()
    if fd_limit.get("status") == "raised":
        print(
            f"[ok] Raised file descriptor soft limit "
            f"{fd_limit.get('previous_soft')} -> {fd_limit.get('soft')}",
            flush=True,
        )
    elif fd_limit.get("status") == "error":
        print(f"[!!] WARNING: Could not raise file descriptor limit: {fd_limit.get('error')}", flush=True)

    fix_credential_permissions()

    try:
        from api.models import _active_state_db_path
        from api.session_recovery import recover_all_sessions_on_startup
        result = recover_all_sessions_on_startup(
            SESSION_DIR,
            rebuild_index=True,
            state_db_path=_active_state_db_path(),
        )
        if result.get("restored"):
            print(f"[recovery] Restored {result['restored']}/{result['scanned']} sessions from .bak (see #1558).", flush=True)
    except Exception as exc:
        # Recovery is best-effort; never block server startup.
        print(f"[recovery] startup recovery failed: {exc}", flush=True)

    within_container = False
    try:
        with open('/.within_container', 'r') as f:
            within_container = True
    except FileNotFoundError:
        pass

    if within_container:
        print('[ok] Running within container.', flush=True)

    from api.auth import is_auth_enabled
    if HOST not in ('127.0.0.1', '::1', 'localhost') and not is_auth_enabled():
        print(f'[!!] WARNING: Binding to {HOST} with NO PASSWORD SET.', flush=True)
        print(f'     Anyone on the network can access your filesystem and agent.', flush=True)
        print(f'     Set a password via Settings or HERMES_WEBUI_PASSWORD env var.', flush=True)
        print(f'     To suppress: bind to 127.0.0.1 or set a password.', flush=True)
        if within_container:
            print(f'     Note: You are running within a container, must bind to 0.0.0.0 (IPv4) or :: (IPv6) to publish the port.', flush=True)
    elif not is_auth_enabled():
        print(f'  [tip] No password set. Any process on this machine can read sessions', flush=True)
        print(f'        and memory via the local API. Set HERMES_WEBUI_PASSWORD to', flush=True)
        print(f'        enable authentication.', flush=True)

    ok, missing, errors = verify_hermes_imports()
    if not ok and _HERMES_FOUND:
        print(f'[!!] Warning: Hermes agent found but missing modules: {missing}', flush=True)
        for mod, err in errors.items():
            print(f'     {mod}: {err}', flush=True)
        print('     Attempting to install missing dependencies from agent requirements.txt...', flush=True)
        auto_install_agent_deps()
        ok, missing, errors = verify_hermes_imports()
        if not ok:
            print(f'[!!] Still missing after install attempt: {missing}', flush=True)
            for mod, err in errors.items():
                print(f'     {mod}: {err}', flush=True)
            print('     Agent features may not work correctly.', flush=True)
        else:
            print('[ok] Agent dependencies installed successfully.', flush=True)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)

    try:
        from api.gateway_watcher import start_watcher

        def _start_watcher_safe():
            try:
                start_watcher()
            except Exception as e:
                print(f'[!!] WARNING: Gateway watcher failed to start: {e}', flush=True)

        t = threading.Thread(target=_start_watcher_safe, daemon=True)
        t.start()
        t.join(timeout=5)
        if t.is_alive():
            print('[tip] Gateway watcher still initializing (non-blocking)', flush=True)
    except Exception as e:
        print(f'[!!] WARNING: Gateway watcher failed to start: {e}', flush=True)

    try:
        from api.background_process import start_drain_thread
        if start_drain_thread():
            print('[ok] bg_task_complete drain thread started', flush=True)
    except Exception as e:
        print(f'[!!] WARNING: bg_task_complete drain failed to start: {e}', flush=True)

    try:
        from api.background_process import start_session_channel_reaper
        if start_session_channel_reaper():
            print('[ok] SessionChannel reaper thread started', flush=True)
    except Exception as e:
        print(f'[!!] WARNING: SessionChannel reaper failed to start: {e}', flush=True)

    try:
        from api.plugins import load_plugins
        load_plugins()
    except Exception as e:
        print(f'[!!] WARNING: Plugin loading failed: {e}', flush=True)

    _abort_if_already_serving(HOST, PORT)
    httpd = QuietHTTPServer((HOST, PORT), Handler)

    from api.config import TLS_ENABLED, TLS_CERT, TLS_KEY
    scheme = 'https' if TLS_ENABLED else 'http'
    if TLS_ENABLED:
        try:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(TLS_CERT, TLS_KEY)
            httpd.ssl_context = ctx
            print(f'  TLS enabled: cert={TLS_CERT}, key={TLS_KEY}', flush=True)
        except Exception as e:
            print(f'[!!] WARNING: TLS setup failed ({e}), falling back to HTTP', flush=True)
            scheme = 'http'

    print(f'  Hermes Web UI listening on {scheme}://{HOST}:{PORT}', flush=True)
    if HOST in ('127.0.0.1', '::1') or within_container:
        print(f'  Remote access: ssh -N -L {PORT}:127.0.0.1:{PORT} <user>@<your-server>', flush=True)
    print(f'  Then open:     {scheme}://localhost:{PORT}', flush=True)
    print('', flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        _log_shutdown_audit()
        try:
            from api.gateway_watcher import stop_watcher
            stop_watcher()
        except Exception:
            logger.debug("Failed to stop gateway watcher during shutdown")
        try:
            from api.session_lifecycle import drain_all_on_shutdown
            drain_all_on_shutdown()
        except Exception:
            logger.debug("Failed to drain lifecycle on shutdown", exc_info=True)
        try:
            from api.background_process import stop_drain_thread
            stop_drain_thread()
        except Exception:
            logger.debug("Failed to stop bg_task_complete drain thread during shutdown", exc_info=True)
        try:
            from api.background_process import stop_session_channel_reaper
            stop_session_channel_reaper()
        except Exception:
            logger.debug("Failed to stop SessionChannel reaper during shutdown", exc_info=True)

if __name__ == '__main__':
    main()
