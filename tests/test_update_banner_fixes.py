"""Tests for update banner fixes — #813 (conflict recovery) and #814 (restart after update).

Covers:
  - conflict error now includes 'conflict: True' flag and actionable git command (#813)
  - successful update returns 'restart_scheduled: True' (#814)
  - _schedule_restart() spawns a daemon thread, does not block (#814)
  - apply_force_update() returns ok on clean reset path (#813)
  - /api/updates/force route exists in routes.py (#813)
  - UI: _showUpdateError and forceUpdate functions exist in ui.js (#813)
  - UI: updateError element and btnForceUpdate element exist in index.html (#813)
  - UI: success toast says 'Restarting' not 'Reloading' (#814)
  - UI: reload timeout bumped to 2500 ms to allow server restart (#814)
"""

import pathlib
import re
import threading
import time
import sys
import os
import io
import json
import types

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


# ── api/updates.py ────────────────────────────────────────────────────────────

class TestUpdateChecker:
    def test_build_compare_url_requires_all_pieces(self):
        import api.updates as upd

        assert upd._build_compare_url(
            'https://github.com/nesquena/hermes-webui', 'abc1234', 'def5678'
        ) == 'https://github.com/nesquena/hermes-webui/compare/abc1234...def5678'
        assert upd._build_compare_url(None, 'abc1234', 'def5678') is None
        assert upd._build_compare_url('https://github.com/nesquena/hermes-webui', None, 'def5678') is None
        assert upd._build_compare_url('https://github.com/nesquena/hermes-webui', 'abc1234', None) is None

    def test_build_compare_url_rejects_unsafe_remote_urls(self):
        import api.updates as upd

        assert upd._build_compare_url('javascript:alert(1)', 'abc1234', 'def5678') is None
        assert upd._build_compare_url('file:///tmp/hermes-webui', 'abc1234', 'def5678') is None
        assert upd._build_compare_url('https:github.com/nesquena/hermes-webui', 'abc1234', 'def5678') is None
        assert upd._build_compare_url('https://github.com/nesquena/hermes-webui', 'abc1234', 'def5678')

    def test_check_repo_includes_compare_url_from_normalized_remote_and_merge_base(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[:2] == ['rev-list', '--count']:
                return '2', True
            if args[0] == 'merge-base':
                return 'abcdef1234567890', True
            if args[:3] == ['rev-parse', '--short', 'abcdef1234567890']:
                return 'abcdef1', True
            if args[:3] == ['rev-parse', '--short', 'origin/master']:
                return 'def5678', True
            if args[:2] == ['remote', 'get-url']:
                return 'git@github.com:NousResearch/hermes-agent.git', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        result = upd._check_repo(tmp_path, 'agent')

        assert result['repo_url'] == 'https://github.com/NousResearch/hermes-agent'
        assert result['current_sha'] == 'abcdef1'
        assert result['latest_sha'] == 'def5678'
        assert result['compare_url'] == 'https://github.com/NousResearch/hermes-agent/compare/abcdef1...def5678'

    def test_check_repo_omits_compare_url_when_merge_base_missing(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[:2] == ['rev-list', '--count']:
                return '2', True
            if args[0] == 'merge-base':
                return 'fatal: no merge base', False
            if args[:3] == ['rev-parse', '--short', 'origin/master']:
                return 'def5678', True
            if args[:2] == ['remote', 'get-url']:
                return 'https://github.com/nesquena/hermes-webui.git', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        result = upd._check_repo(tmp_path, 'webui')

        assert result['current_sha'] is None
        assert result['latest_sha'] == 'def5678'
        assert result['compare_url'] is None

    def test_repo_url_strips_only_dot_git_suffix(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[:2] == ['rev-list', '--count']:
                return '0', True
            if args[0] == 'merge-base':
                return 'abcdef1234567890', True
            if args[:2] == ['rev-parse', '--short']:
                return 'abcdef1', True
            if args[:2] == ['remote', 'get-url']:
                return 'https://github.com/nesquena/hermes-webui.git', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        result = upd._check_repo(tmp_path, 'webui')

        assert result['repo_url'] == 'https://github.com/nesquena/hermes-webui'

    def test_repo_url_converts_ssh_and_strips_only_dot_git_suffix(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/main', True
            if args[:2] == ['rev-list', '--count']:
                return '0', True
            if args[0] == 'merge-base':
                return 'abcdef1234567890', True
            if args[:2] == ['rev-parse', '--short']:
                return 'abcdef1', True
            if args[:2] == ['remote', 'get-url']:
                return 'git@github.com:NousResearch/hermes-agent.git', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        result = upd._check_repo(tmp_path, 'agent')

        assert result['repo_url'] == 'https://github.com/NousResearch/hermes-agent'

    def test_repo_url_strips_dot_git_before_trailing_slashes(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[:2] == ['rev-list', '--count']:
                return '2', True
            if args[0] == 'merge-base':
                return 'abcdef1234567890', True
            if args[:2] == ['rev-parse', '--short']:
                return 'abcdef1', True
            if args[:2] == ['remote', 'get-url']:
                return 'https://github.com/nesquena/hermes-webui.git/', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        result = upd._check_repo(tmp_path, 'webui')

        assert result['repo_url'] == 'https://github.com/nesquena/hermes-webui'


class TestConflictError:
    """#813 — conflict error must include flag + recovery command."""

    def test_conflict_returns_conflict_flag(self, tmp_path, monkeypatch):
        import api.updates as upd

        # Fake a repo with conflict markers in git status output
        (tmp_path / '.git').mkdir()
        conflict_status = 'UU some/file.py'

        calls = []
        def fake_run(args, cwd, timeout=10):
            calls.append(args)
            if args[:2] == ['status', '--porcelain']:
                return conflict_status, True
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)

        result = upd.apply_update('webui')
        assert result['ok'] is False
        assert result.get('conflict') is True, "conflict flag must be True"
        assert 'checkout' in result['message'] or 'pull' in result['message'], (
            "conflict message must include recovery command"
        )
        assert 'merge conflict' in result['message'].lower()

    def test_conflict_message_includes_git_command(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[:2] == ['status', '--porcelain']:
                return 'AA conflict.txt', True
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)

        result = upd.apply_update('agent')
        # Message must be actionable — should mention git checkout or pull
        msg = result['message']
        assert 'git' in msg.lower(), f"message should mention git: {msg}"


class TestScheduleRestart:
    """#814 — _schedule_restart must exist and be non-blocking."""

    def test_schedule_restart_exists(self):
        from api.updates import _schedule_restart
        assert callable(_schedule_restart)

    def test_schedule_restart_is_nonblocking(self, monkeypatch):
        """_schedule_restart() must return immediately (spawns daemon thread)."""
        import api.updates as upd

        execv_called = []

        def fake_execv(exe, args):
            execv_called.append((exe, args))

        # Monkeypatch os.execv inside the module's thread closure
        import os as _os
        original_execv = _os.execv

        monkeypatch.setattr(_os, 'execv', fake_execv)

        start = time.monotonic()
        upd._schedule_restart(delay=0.05)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"_schedule_restart must return immediately, took {elapsed:.2f}s"
        # Give the thread time to call execv
        time.sleep(0.2)
        assert execv_called, "_schedule_restart must eventually call os.execv"


class TestApplyUpdateRestartSafety:
    """Self-update must not re-exec while chat streams are active."""

    def test_apply_update_refuses_when_stream_active(self, tmp_path, monkeypatch):
        import queue
        import api.updates as upd
        from api.config import STREAMS, STREAMS_LOCK

        (tmp_path / '.git').mkdir()
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        called = []
        monkeypatch.setattr(upd, '_run_git', lambda *a, **k: (called.append(a) or ('', True)))
        monkeypatch.setattr(upd, '_schedule_restart', lambda delay=2.0: (_ for _ in ()).throw(AssertionError('must not restart')))

        with STREAMS_LOCK:
            old = dict(STREAMS)
            STREAMS.clear()
            STREAMS['stream_active'] = queue.Queue()
        try:
            result = upd.apply_update('webui')
        finally:
            with STREAMS_LOCK:
                STREAMS.clear()
                STREAMS.update(old)

        assert result['ok'] is False
        assert result.get('active_streams') == 1
        assert result.get('restart_blocked') is True
        assert 'active chat stream' in result['message']
        assert called == []

    def test_force_update_refuses_when_stream_active(self, tmp_path, monkeypatch):
        import queue
        import api.updates as upd
        from api.config import STREAMS, STREAMS_LOCK

        (tmp_path / '.git').mkdir()
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        monkeypatch.setattr(upd, '_run_git', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not run git')))
        monkeypatch.setattr(upd, '_schedule_restart', lambda delay=2.0: (_ for _ in ()).throw(AssertionError('must not restart')))

        with STREAMS_LOCK:
            old = dict(STREAMS)
            STREAMS.clear()
            STREAMS['stream_active'] = queue.Queue()
        try:
            result = upd.apply_force_update('agent')
        finally:
            with STREAMS_LOCK:
                STREAMS.clear()
                STREAMS.update(old)

        assert result['ok'] is False
        assert result.get('active_streams') == 1
        assert result.get('restart_blocked') is True
        assert 'active chat stream' in result['message']


class TestSuccessfulUpdateReturnsRestartScheduled:
    """#814 — successful apply_update must return restart_scheduled: True."""

    def test_apply_update_returns_restart_scheduled(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['status', '--porcelain']:
                return '', True   # clean tree
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[0] == 'pull':
                return 'Already up to date.', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        # Don't actually restart
        monkeypatch.setattr(upd, '_schedule_restart', lambda delay=2.0: None)

        result = upd.apply_update('webui')
        assert result['ok'] is True
        assert result.get('restart_scheduled') is True, (
            "successful update must set restart_scheduled: True"
        )


class TestApplyForceUpdate:
    """#813 — apply_force_update must reset hard and return ok."""

    def test_apply_force_update_ok(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()
        ran = []

        def fake_run(args, cwd, timeout=10):
            ran.append(args)
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[0] == 'checkout':
                return '', True
            if args[0] == 'reset':
                return '', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        monkeypatch.setattr(upd, '_schedule_restart', lambda delay=2.0: None)

        result = upd.apply_force_update('webui')
        assert result['ok'] is True
        assert result.get('restart_scheduled') is True

        git_cmds = [r[0] for r in ran]
        assert 'reset' in git_cmds, "force update must call git reset --hard"
        assert 'checkout' in git_cmds, "force update must call git checkout . to clear conflicts"

    def test_apply_force_update_rejects_unknown_target(self, tmp_path, monkeypatch):
        import api.updates as upd
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        result = upd.apply_force_update('invalid')
        assert result['ok'] is False


# ── api/routes.py ─────────────────────────────────────────────────────────────

class TestForceUpdateRoute:
    """#813 — /api/updates/force route must exist in routes.py."""

    def test_force_route_exists(self):
        src = read('api/routes.py')
        assert '"/api/updates/force"' in src, (
            "routes.py must handle POST /api/updates/force"
        )
        assert 'apply_force_update' in src, (
            "routes.py must import and call apply_force_update"
        )


class TestUpdateSummaryRouteModelSelection:
    """Update summaries should use a known text auxiliary model before main model fallback."""

    def test_summary_route_prefers_documented_compression_auxiliary_model(self):
        src = read('api/routes.py')

        assert 'get_text_auxiliary_client' in src
        assert '"compression"' in src
        assert '"update_summary"' not in src
        assert 'main_runtime=main_runtime' in src
        assert 'update summary auxiliary model failed; falling back to main model' in src
        assert 'from run_agent import AIAgent' in src

    def test_summary_route_auxiliary_model_uses_active_profile_env(self, monkeypatch, tmp_path):
        import api.config as cfg
        import api.profiles as profiles
        import api.routes as routes
        import api.updates as updates

        class FakeHandler:
            def __init__(self, payload):
                raw = json.dumps(payload).encode('utf-8')
                self.headers = {'Content-Length': str(len(raw))}
                self.rfile = io.BytesIO(raw)
                self.wfile = io.BytesIO()
                self.status = None

            def send_response(self, status):
                self.status = status

            def send_header(self, _key, _value):
                pass

            def end_headers(self):
                pass

            def response_payload(self):
                return json.loads(self.wfile.getvalue().decode('utf-8'))

        captured = {}
        profile_home = tmp_path / 'profiles' / 'work'
        fake_skill_module = types.ModuleType('tools.skills_tool')
        setattr(fake_skill_module, 'HERMES_HOME', 'default-home')
        setattr(fake_skill_module, 'SKILLS_DIR', 'default-home/skills')
        monkeypatch.setitem(sys.modules, 'tools.skills_tool', fake_skill_module)

        monkeypatch.setattr(profiles, 'get_hermes_home_for_profile', lambda profile: profile_home)
        monkeypatch.setattr(
            profiles,
            'get_profile_runtime_env',
            lambda home: {'HERMES_TEST_PROFILE_ENV': 'work-runtime'},
        )
        monkeypatch.setattr(cfg, 'get_effective_default_model', lambda: 'openai/test-main')

        def fake_resolve_model_provider(model):
            thread_env = getattr(cfg._thread_ctx, 'env', {})
            captured['model_resolution_env'] = {
                'HERMES_HOME': os.environ.get('HERMES_HOME'),
                'HERMES_TEST_PROFILE_ENV': os.environ.get('HERMES_TEST_PROFILE_ENV'),
                'THREAD_HERMES_HOME': thread_env.get('HERMES_HOME'),
                'THREAD_HERMES_TEST_PROFILE_ENV': thread_env.get('HERMES_TEST_PROFILE_ENV'),
            }
            return model, 'openai', 'https://example.test/v1'

        monkeypatch.setattr(cfg, 'resolve_model_provider', fake_resolve_model_provider)
        monkeypatch.setattr(cfg, 'resolve_custom_provider_connection', lambda provider: (None, None))

        fake_runtime_provider = types.ModuleType('hermes_cli.runtime_provider')
        fake_runtime_provider.resolve_runtime_provider = lambda requested=None: {
            'api_key': 'fake-key',
            'provider': requested or 'openai',
            'base_url': 'https://example.test/v1',
        }
        fake_hermes_cli = types.ModuleType('hermes_cli')
        fake_hermes_cli.__path__ = []
        fake_hermes_cli.runtime_provider = fake_runtime_provider
        monkeypatch.setitem(sys.modules, 'hermes_cli', fake_hermes_cli)
        monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', fake_runtime_provider)

        class FakeAuxClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(model, messages):
                        captured['aux_create'] = {'model': model, 'messages': messages}
                        return types.SimpleNamespace(
                            choices=[
                                types.SimpleNamespace(
                                    message=types.SimpleNamespace(
                                        content='Notice: Profile-routed update summaries work.'
                                    )
                                )
                            ]
                        )

        def fake_get_text_auxiliary_client(task, main_runtime=None):
            thread_env = getattr(cfg._thread_ctx, 'env', {})
            captured['aux_env'] = {
                'HERMES_HOME': os.environ.get('HERMES_HOME'),
                'HERMES_TEST_PROFILE_ENV': os.environ.get('HERMES_TEST_PROFILE_ENV'),
                'THREAD_HERMES_HOME': thread_env.get('HERMES_HOME'),
                'THREAD_HERMES_TEST_PROFILE_ENV': thread_env.get('HERMES_TEST_PROFILE_ENV'),
                'SKILL_MODULE_HOME': getattr(fake_skill_module, 'HERMES_HOME'),
                'SKILL_MODULE_DIR': getattr(fake_skill_module, 'SKILLS_DIR'),
            }
            captured['aux_task'] = task
            captured['main_runtime'] = dict(main_runtime or {})
            return FakeAuxClient(), 'profile-compression-model'

        fake_auxiliary_client = types.ModuleType('agent.auxiliary_client')
        fake_auxiliary_client.get_text_auxiliary_client = fake_get_text_auxiliary_client
        fake_agent = types.ModuleType('agent')
        fake_agent.__path__ = []
        fake_agent.auxiliary_client = fake_auxiliary_client
        monkeypatch.setitem(sys.modules, 'agent', fake_agent)
        monkeypatch.setitem(sys.modules, 'agent.auxiliary_client', fake_auxiliary_client)

        with updates._cache_lock:
            updates._summary_cache.clear()

        monkeypatch.setenv('HERMES_HOME', 'default-home')
        monkeypatch.setenv('HERMES_TEST_PROFILE_ENV', 'default-runtime')

        body = {
            'target': 'webui',
            'updates': {
                'webui': {
                    'behind': 1,
                    'current_sha': 'profile-env-before',
                    'latest_sha': f'profile-env-after-{time.time_ns()}',
                    'compare_url': 'https://example.test/compare',
                },
            },
        }
        handler = FakeHandler(body)

        profiles.set_request_profile('work')
        try:
            routes.handle_post(handler, types.SimpleNamespace(path='/api/updates/summary'))
        finally:
            profiles.clear_request_profile()

        assert handler.status == 200
        payload = handler.response_payload()
        assert payload['generated_by'] == 'llm'
        assert captured['aux_task'] == 'compression'
        assert captured['model_resolution_env'] == {
            'HERMES_HOME': str(profile_home),
            'HERMES_TEST_PROFILE_ENV': 'default-runtime',
            'THREAD_HERMES_HOME': str(profile_home),
            'THREAD_HERMES_TEST_PROFILE_ENV': 'work-runtime',
        }
        assert captured['aux_env'] == {
            'HERMES_HOME': str(profile_home),
            'HERMES_TEST_PROFILE_ENV': 'default-runtime',
            'THREAD_HERMES_HOME': str(profile_home),
            'THREAD_HERMES_TEST_PROFILE_ENV': 'work-runtime',
            'SKILL_MODULE_HOME': profile_home,
            'SKILL_MODULE_DIR': profile_home / 'skills',
        }
        assert captured['aux_create']['model'] == 'profile-compression-model'
        assert getattr(fake_skill_module, 'HERMES_HOME') == 'default-home'
        assert getattr(fake_skill_module, 'SKILLS_DIR') == 'default-home/skills'
        assert os.environ.get('HERMES_HOME') == 'default-home'
        assert os.environ.get('HERMES_TEST_PROFILE_ENV') == 'default-runtime'


class TestUiJsUpdateBanner:
    """#813 + #814 — UI must show persistent error, force button, and correct toast."""

    def test_show_update_error_function_exists(self):
        src = read('static/ui.js')
        assert 'function _showUpdateError' in src, (
            "_showUpdateError() must be defined in ui.js"
        )

    def test_force_update_function_exists(self):
        src = read('static/ui.js')
        assert 'function forceUpdate' in src or 'async function forceUpdate' in src, (
            "forceUpdate() must be defined in ui.js"
        )

    def test_force_update_uses_confirm_dialog_not_native(self):
        """forceUpdate() must use showConfirmDialog(), not the banned native confirm()."""
        src = read('static/ui.js')
        m = re.search(r'function forceUpdate\b.*?\n\}', src, re.DOTALL)
        assert m, "forceUpdate() not found"
        fn = m.group(0)
        assert 'showConfirmDialog' in fn, (
            "forceUpdate() must use showConfirmDialog() not the native confirm() "
            "(native confirm is banned by test_sprint33)"
        )
        assert 'confirm(' not in fn.replace('showConfirmDialog(', ''), (
            "forceUpdate() must not use native confirm()"
        )

    def test_force_update_calls_api_updates_force(self):
        src = read('static/ui.js')
        m = re.search(r'function forceUpdate\b.*?\n\}', src, re.DOTALL)
        assert m, "forceUpdate() not found"
        fn = m.group(0)
        assert '/api/updates/force' in fn, (
            "forceUpdate() must POST to /api/updates/force"
        )

    def test_success_toast_says_restarting(self):
        src = read('static/ui.js')
        m = re.search(r'function applyUpdates\b.*?\n\}', src, re.DOTALL)
        assert m, "applyUpdates() not found"
        fn = m.group(0)
        assert 'restarting' in fn.lower(), (
            "success toast must mention 'restarting' (server self-restarts after update)"
        )
        assert 'Reloading' not in fn, (
            "success toast must not say 'Reloading' — server restarts, page reloads after"
        )

    def test_reload_uses_health_poll_not_blind_timeout(self):
        """applyUpdates must use _waitForServerThenReload() instead of a blind setTimeout.

        A fixed setTimeout race-loses against slow hardware or reverse proxies
        that return 502 immediately when the upstream socket is down.
        The polling approach retries until /health responds OK.
        """
        src = read('static/ui.js')
        m = re.search(r'function applyUpdates\b.*?\n\}', src, re.DOTALL)
        assert m, "applyUpdates() not found"
        fn = m.group(0)
        assert '_waitForServerThenReload' in fn, (
            "applyUpdates() must call _waitForServerThenReload() instead of a blind "
            "setTimeout reload — blind timeouts race-lose against slow restarts and "
            "reverse proxies that 502 immediately on restart."
        )
        assert 'setTimeout(()=>location.reload' not in fn, (
            "applyUpdates() must not use a fixed setTimeout reload — use _waitForServerThenReload()."
        )

    def test_wait_for_server_then_reload_is_defined(self):
        """_waitForServerThenReload() must actually exist — the original PR
        referenced it from applyUpdates()/forceUpdate() without defining it,
        which would have thrown ReferenceError on 'Update Now'."""
        src = read('static/ui.js')
        assert re.search(r'(async\s+)?function\s+_waitForServerThenReload\b', src), (
            "_waitForServerThenReload() is called but not defined — this breaks "
            "the Update Now flow entirely (ReferenceError at runtime)."
        )

    def test_wait_for_server_polls_health(self):
        """_waitForServerThenReload() must fetch health to determine readiness."""
        src = read('static/ui.js')
        m = re.search(r'function\s+_waitForServerThenReload\b.*?\n\}', src, re.DOTALL)
        assert m, "_waitForServerThenReload() not found"
        fn = m.group(0)
        assert "new URL('health'" in fn, (
            "_waitForServerThenReload must poll the mount-relative health endpoint "
            "to detect server readiness"
        )
        assert 'location.reload' in fn, (
            "_waitForServerThenReload must call location.reload() once the server is ready"
        )

    def test_refresh_session_handles_restart_mode(self):
        """When _restartingForUpdate flag is set, refreshSession() must do a
        full page reload rather than hit /api/session (which will 502 while
        the server is down)."""
        src = read('static/ui.js')
        m = re.search(r'async function refreshSession\b.*?\n\}', src, re.DOTALL)
        assert m, "refreshSession() not found"
        fn = m.group(0)
        assert '_restartingForUpdate' in fn and 'location.reload' in fn, (
            "refreshSession() must check the restart flag and bypass /api/session "
            "when the server is mid-restart."
        )

    def test_conflict_response_shows_force_button(self):
        src = read('static/ui.js')
        m = re.search(r'function _showUpdateError\b.*?\n\}', src, re.DOTALL)
        assert m, "_showUpdateError() not found"
        fn = m.group(0)
        assert 'conflict' in fn or 'diverged' in fn, (
            "_showUpdateError must check res.conflict / res.diverged to show force button"
        )
        assert 'btnForceUpdate' in fn or 'forceBtn' in fn, (
            "_showUpdateError must reference the force update button"
        )

    def test_error_displayed_persistently_not_just_toast(self):
        src = read('static/ui.js')
        m = re.search(r'function _showUpdateError\b.*?\n\}', src, re.DOTALL)
        assert m
        fn = m.group(0)
        assert 'updateError' in fn, (
            "_showUpdateError must write to the #updateError element for persistent display"
        )


class TestUpdateBannerUx:
    def test_update_banner_includes_repo_branch_labels(self):
        src = read('static/ui.js')
        assert 'function _formatUpdateTargetStatus' in src
        assert 'info.branch' in src
        assert "_formatUpdateTargetStatus('WebUI',data.webui)" in src
        assert "_formatUpdateTargetStatus('Agent',data.agent)" in src

    def test_settings_update_check_uses_same_repo_branch_formatter(self):
        src = read('static/panels.js')
        m = re.search(r'async function checkUpdatesNow\b.*?\n\}', src, re.DOTALL)
        assert m, "checkUpdatesNow() not found"
        fn = m.group(0)
        assert '_formatUpdateTargetStatus' in fn
        assert "formatUpdatePart('WebUI',data.webui)" in fn
        assert "formatUpdatePart('Agent',data.agent)" in fn


# ── static/index.html ─────────────────────────────────────────────────────────

class TestIndexHtmlBanner:
    """#813 — update banner HTML must include error element and force button."""

    def test_update_error_element_exists(self):
        src = read('static/index.html')
        assert 'id="updateError"' in src, (
            "index.html must have #updateError element for persistent error display"
        )

    def test_force_update_button_exists(self):
        src = read('static/index.html')
        assert 'id="btnForceUpdate"' in src, (
            "index.html must have #btnForceUpdate button (hidden by default)"
        )

    def test_force_update_button_hidden_by_default(self):
        src = read('static/index.html')
        m = re.search(r'id="btnForceUpdate"[^>]*>', src)
        assert m, "#btnForceUpdate not found"
        tag = m.group(0)
        assert 'display:none' in tag, (
            "#btnForceUpdate must be hidden by default (display:none)"
        )


# ── Regression: sequential webui+agent update — restart coordination ──────────

class TestSequentialUpdateRestartCoordination:
    """Regression guard for the two-target race: when both webui and agent
    have updates, the client POSTs them sequentially (webui → agent). The
    first update's success schedules a restart timer; without coordination
    that timer fires while the second update's git-pull is still running,
    killing it mid-stream and leaving the second repo partial.

    Fix: `_schedule_restart` must acquire `_apply_lock` before calling
    `os.execv`, so a pending second update always completes first.
    """

    def test_schedule_restart_waits_for_apply_lock(self, monkeypatch):
        """The restart thread must wait for any in-flight update before
        calling execv. Exercised by holding _apply_lock from another thread
        and verifying execv is delayed until the lock is released."""
        import api.updates as upd
        import threading as _th
        import time as _t

        execv_called = _th.Event()
        execv_time = []

        def fake_execv(exe, args):
            execv_time.append(_t.monotonic())
            execv_called.set()

        monkeypatch.setattr(os, 'execv', fake_execv)

        # Hold _apply_lock from another thread (simulating an in-flight
        # second update) for 0.4 s.
        release_time = []
        lock_held = _th.Event()

        def holder():
            with upd._apply_lock:
                lock_held.set()
                _t.sleep(0.4)
                release_time.append(_t.monotonic())

        holder_thread = _th.Thread(target=holder, daemon=True)
        holder_thread.start()
        lock_held.wait(timeout=2)

        # Schedule a restart with a short delay. The lock is held;
        # the restart thread should block on it.
        upd._schedule_restart(delay=0.05)
        _t.sleep(0.15)
        assert not execv_called.is_set(), (
            "execv called while _apply_lock was still held by another "
            "thread — restart must wait for in-flight updates to finish"
        )

        # Let the holder release.
        holder_thread.join(timeout=2)
        assert release_time, "holder didn't release the lock"

        # execv should fire shortly after the lock release.
        assert execv_called.wait(timeout=2), (
            "execv never fired after _apply_lock was released"
        )
        assert execv_time[0] >= release_time[0], (
            f"execv fired before lock was released "
            f"(execv={execv_time[0]}, release={release_time[0]})"
        )

    def test_schedule_restart_still_fires_when_no_update_in_flight(self, monkeypatch):
        """Sanity: with nothing holding the lock, restart still fires promptly."""
        import api.updates as upd
        import time as _t

        execv_called = []
        def fake_execv(exe, args):
            execv_called.append(True)
        monkeypatch.setattr(os, 'execv', fake_execv)

        upd._schedule_restart(delay=0.05)
        _t.sleep(0.25)
        assert execv_called, (
            "restart must still fire when _apply_lock is free"
        )



class TestUpdateCompareSource:
    def test_simulated_update_check_payload_includes_both_safe_compare_urls(self):
        src = read('api/routes.py')
        assert '"repo_url": "https://github.com/nesquena/hermes-webui"' in src
        assert '"compare_url": "https://github.com/nesquena/hermes-webui/compare/abc1234...def5678"' in src
        assert '"repo_url": "https://github.com/NousResearch/hermes-agent"' in src
        assert '"compare_url": "https://github.com/NousResearch/hermes-agent/compare/aaa0001...bbb0002"' in src

    def test_update_banner_html_uses_multi_target_links_container(self):
        src = read('static/index.html')
        assert 'id="updateWhatsNewLinks"' in src
        assert 'id="updateWhatsNew"' not in src

    def test_update_banner_frontend_uses_data_driven_compare_helpers(self):
        src = read('static/ui.js')
        assert 'function _isSafeUpdateCompareUrl(url)' in src
        assert 'function _updateCompareUrl(info)' in src
        assert 'function _updateWhatsNewTargets(data)' in src
        assert 'function _renderUpdateWhatsNewLinks(data)' in src
        assert "$('updateWhatsNewLinks')" in src
        assert "compare_url" in src
        assert "repo_url+'/compare/'+currentSha+'...'+latestSha" in src
        assert "_isSafeUpdateCompareUrl(compareUrl)?compareUrl:null" in src
        assert "_renderUpdateWhatsNewLinks(data);" in src
        assert "data.webui.repo_url" not in src
        assert "$('updateWhatsNew')" not in src

    def test_update_banner_clears_stale_links_when_no_updates_remain(self):
        src = read('static/ui.js')
        start = src.find('function _showUpdateBanner(data)')
        assert start != -1, "_showUpdateBanner not found"
        fn = src[start:src.find('function dismissUpdate()', start)]
        empty_idx = fn.find('if(!parts.length)')
        assert empty_idx != -1, "_showUpdateBanner must handle empty update payloads"
        empty_block = fn[empty_idx:fn.find('return;', empty_idx) + len('return;')]
        assert '_renderUpdateWhatsNewLinks(data);' in empty_block
        assert "classList.remove('visible')" in empty_block

    def test_manual_up_to_date_check_clears_update_banner(self):
        src = read('static/panels.js')
        up_to_date_idx = src.find("settings_up_to_date")
        assert up_to_date_idx != -1, "manual update up-to-date branch not found"
        block = src[up_to_date_idx:up_to_date_idx + 300]
        assert "_showUpdateBanner(data)" in block


class TestWhatsNewSummaryToggle:
    def test_settings_default_and_persistence_allow_whats_new_summary_toggle(self):
        src = read('api/config.py')
        assert '"whats_new_summary_enabled": False' in src
        bool_keys_start = src.find('_SETTINGS_BOOL_KEYS')
        assert bool_keys_start != -1
        bool_keys = src[bool_keys_start:src.find('}', bool_keys_start)]
        assert '"whats_new_summary_enabled"' in bool_keys

    def test_settings_panel_places_summary_toggle_next_to_update_check(self):
        src = read('static/index.html')
        check_idx = src.find('id="settingsCheckUpdates"')
        summary_idx = src.find('id="settingsWhatsNewSummary"')
        assert check_idx != -1, "settingsCheckUpdates checkbox missing"
        assert summary_idx != -1, "settingsWhatsNewSummary checkbox missing"
        assert check_idx < summary_idx, "summary toggle should sit after the update-check toggle"
        nearby = src[summary_idx:summary_idx + 900]
        assert 'settings_label_whats_new_summary' in nearby
        assert 'settings_desc_whats_new_summary' in nearby

    def test_settings_js_loads_saves_and_boots_summary_toggle(self):
        panels = read('static/panels.js')
        boot = read('static/boot.js')
        assert "$('settingsWhatsNewSummary')" in panels
        assert 'payload.whats_new_summary_enabled' in panels
        assert 'settings.whats_new_summary_enabled' in panels
        assert 'body.whats_new_summary_enabled' in panels
        assert 'window._whatsNewSummaryEnabled' in boot
        assert 'whats_new_summary_enabled' in boot

    def test_update_banner_summary_flow_keeps_diff_links_after_summary(self):
        src = read('static/ui.js')
        assert 'function _renderUpdateSummaryPanel' in src
        assert 'async function showWhatsNewSummary' in src
        assert "api('/api/updates/summary'" in src
        assert 'updateSummaryDiffLinks' in src
        assert 'Regular diff comparison' in src
        assert 'updateSummarySections' in src
        assert 'Generate WebUI update summary' in src
        assert 'Generate Agent update summary' in src
        assert 'View generated WebUI update summary' in src
        assert 'View generated Agent update summary' in src
        assert 'Re-generate WebUI update summary' in src
        assert 'Re-generate Agent update summary' in src
        assert 'window._whatsNewGeneratedSummaries' in src
        assert 'sessionStorage' in src
        assert 'hermes-whats-new-generated-summaries' in src
        assert 'function _loadStoredUpdateSummaries' in src
        assert 'function _persistGeneratedSummaries' in src
        assert 'function _pruneGeneratedSummaries' in src
        assert 'function _updateSummarySignature' in src
        assert 'function _updateSummaryButtonLabel' in src
        assert 'showWhatsNewSummary(target.key)' in src
        assert 'target?{[target]:data[target]}:data' in src
        assert 'target:target||null' in src
        assert '_renderUpdateWhatsNewLinks(data,{mode' in src
        assert 'window._whatsNewSummaryEnabled' in src

    def test_summary_endpoint_and_prompt_are_human_readable_not_technical(self):
        routes = read('api/routes.py')
        updates = read('api/updates.py')
        assert '"/api/updates/summary"' in routes
        assert 'summarize_update_payload' in routes
        assert 'def summarize_update_payload' in updates
        assert 'human-readable' in updates
        assert 'avoid technical jargon' in updates
        assert 'regular diff comparison' in updates
        assert 'Return only prefixed bullets' in updates
        assert 'def _format_update_summary_sections' in updates

    def test_update_summary_formats_llm_text_into_stable_sections(self):
        from api.updates import summarize_update_payload

        payload = {
            'webui': {'behind': 2, 'current_sha': 'abc', 'latest_sha': 'def', 'compare_url': 'https://example.test/webui'},
            'agent': {'behind': 1, 'current_sha': 'aaa', 'latest_sha': 'bbb', 'compare_url': 'https://example.test/agent'},
        }
        result = summarize_update_payload(
            payload,
            llm_callback=lambda _system, _prompt: 'The settings panel is easier to understand. Update prompts are clearer.',
            use_cache=False,
        )
        assert result['summary_sections'][0]['title'] == "What you'll notice"
        assert result['summary_sections'][1]['title'] == 'Worth knowing'
        assert result['summary_sections'][0]['items']
        assert result['summary_sections'][1]['items']
        assert 'regular diff comparison' not in ' '.join(result['summary_sections'][1]['items']).lower()
        assert 'What you\'ll notice' in result['summary']
        assert 'Worth knowing' in result['summary']
        assert '- The settings panel is easier to understand.' in result['summary']

    def test_update_summary_deduplicates_notice_items_from_worth_knowing(self):
        from api.updates import summarize_update_payload

        payload = {
            'webui': {'behind': 2, 'current_sha': 'abc', 'latest_sha': 'def', 'compare_url': 'https://example.test/webui'},
        }
        result = summarize_update_payload(
            payload,
            llm_callback=lambda _system, _prompt: 'The settings panel is easier to understand. Update prompts are clearer.',
            use_cache=False,
        )
        notice_items = result['summary_sections'][0]['items']
        worth_section = next((section for section in result['summary_sections'] if section['title'] == 'Worth knowing'), None)

        assert notice_items == [
            'The settings panel is easier to understand.',
            'Update prompts are clearer.',
        ]
        assert worth_section is None
        assert 'Worth knowing' not in result['summary']
        assert 'This summary covers WebUI' not in result['summary']

    def test_update_summary_deduplicates_repeated_agent_summary_bullets(self):
        from api.updates import summarize_update_payload

        duplicate_menu_item = (
            'The `hermes tools` menus should open noticeably faster, especially when checking available tools or auth state.'
        )
        duplicate_quality_item = (
            'These updates are small quality-of-life improvements focused on smoother messaging and less waiting in the CLI.'
        )
        result = summarize_update_payload(
            {
                'agent': {
                    'behind': 2,
                    'current_sha': 'abc',
                    'latest_sha': 'def',
                    'compare_url': 'https://example.test/agent',
                },
            },
            llm_callback=lambda _system, _prompt: '\n'.join(
                [
                    'Slack thread commands now also work with `!cmd`, giving you an easier fallback when slash commands are awkward or unavailable.',
                    duplicate_menu_item,
                    duplicate_quality_item,
                    duplicate_menu_item,
                    duplicate_quality_item,
                ]
            ),
            use_cache=False,
        )
        sections = {section['title']: section['items'] for section in result['summary_sections']}

        assert duplicate_menu_item in sections["What you'll notice"]
        assert duplicate_quality_item in sections["What you'll notice"]
        assert 'Worth knowing' not in sections
        assert result['summary'].count(duplicate_menu_item) == 1
        assert result['summary'].count(duplicate_quality_item) == 1

    def test_update_summary_keeps_all_categorized_notice_and_worth_bullets(self):
        from api.updates import summarize_update_payload

        result = summarize_update_payload(
            {'webui': {'behind': 8, 'current_sha': 'abc', 'latest_sha': 'def', 'compare_url': 'https://example.test/webui'}},
            llm_callback=lambda _system, _prompt: '\n'.join(
                [
                    'Notice: The settings panel loads faster.',
                    'Notice: Update prompts are easier to read.',
                    'Notice: Chat status is clearer during reconnects.',
                    'Notice: Tool results stay grouped by source.',
                    'Notice: Mobile controls remain visible.',
                    'Worth knowing: Some labels were renamed to match the new flow.',
                    'Worth knowing: The full diff is still available from the update banner.',
                ]
            ),
            use_cache=False,
        )
        sections = {section['title']: section['items'] for section in result['summary_sections']}

        assert sections["What you'll notice"] == [
            'The settings panel loads faster.',
            'Update prompts are easier to read.',
            'Chat status is clearer during reconnects.',
            'Tool results stay grouped by source.',
            'Mobile controls remain visible.',
        ]
        assert sections['Worth knowing'] == [
            'Some labels were renamed to match the new flow.',
            'The full diff is still available from the update banner.',
        ]

    def test_update_summary_keeps_unknown_prefixed_bullets_as_notice(self):
        from api.updates import summarize_update_payload

        result = summarize_update_payload(
            {'webui': {'behind': 3, 'current_sha': 'abc', 'latest_sha': 'def', 'compare_url': 'https://example.test/webui'}},
            llm_callback=lambda _system, _prompt: '\n'.join(
                [
                    'Notice: The settings panel loads faster.',
                    'Caveat: Restart once after applying the update.',
                    'Action required: Reopen the update banner if the summary was already cached.',
                    'Worth knowing: The full diff is still available from the update banner.',
                ]
            ),
            use_cache=False,
        )
        sections = {section['title']: section['items'] for section in result['summary_sections']}

        assert sections["What you'll notice"] == [
            'The settings panel loads faster.',
            'Caveat: Restart once after applying the update.',
            'Action required: Reopen the update banner if the summary was already cached.',
        ]
        assert sections['Worth knowing'] == [
            'The full diff is still available from the update banner.',
        ]

    def test_update_summary_panel_is_scrollable_for_long_summaries(self):
        style = read('static/style.css')

        assert '#updateSummaryPanel{max-height:min(34vh,260px);overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:var(--accent) transparent;}' in style

    def test_update_summary_many_updates_caps_commit_input_and_discloses_scope(self, monkeypatch):
        import api.updates as upd

        subjects = [f'Commit subject {idx}' for idx in range(1, 25)]
        monkeypatch.setattr(
            upd,
            '_commit_subjects_for_update_with_limit',
            lambda _info, *, limit=24: (subjects[:limit], True),
        )
        prompts = []

        def fake_llm(_system, prompt):
            prompts.append(prompt)
            return '\n'.join([
                'Notice: Several user-facing fixes are ready.',
                'Notice: Settings and update messaging should be easier to understand.',
                'Notice: The update flow should feel safer and clearer.',
                'Notice: Mobile update controls should stay reachable.',
                'Worth knowing: Some lower-level cleanup supports the visible update changes.',
            ])

        result = upd.summarize_update_payload(
            {
                'webui': {
                    'behind': 57,
                    'current_sha': 'abc',
                    'latest_sha': 'def',
                    'compare_url': 'https://example.test/webui',
                }
            },
            target='webui',
            llm_callback=fake_llm,
            use_cache=False,
        )

        assert len(subjects) == 24
        assert prompts
        assert 'Showing latest 24 of 57 commit subjects; summarize trends, not every commit.' in prompts[0]
        assert 'Commit subject 24' in prompts[0]
        assert 'Commit subject 25' not in prompts[0]
        sections = {section['title']: section['items'] for section in result['summary_sections']}
        assert sections["What you'll notice"] == [
            'Several user-facing fixes are ready.',
            'Settings and update messaging should be easier to understand.',
            'The update flow should feel safer and clearer.',
            'Mobile update controls should stay reachable.',
        ]
        assert sections['Worth knowing'] == [
            'Some lower-level cleanup supports the visible update changes.',
            'WebUI has 57 updates; this summary uses the latest 24 commit subjects, with the full comparison still available in the diff link.',
        ]
        assert result['targets'][0]['commits_truncated'] is True

    def test_update_summary_cache_reuses_same_update_summary(self):
        import api.updates as upd

        upd._summary_cache.clear()
        calls = []
        payload = {
            'webui': {'behind': 2, 'current_sha': 'abc', 'latest_sha': 'def', 'compare_url': 'https://example.test/webui'},
        }

        def fake_llm(_system, _prompt):
            calls.append(True)
            return f'- Stable cached summary #{len(calls)}'

        first = upd.summarize_update_payload(payload, llm_callback=fake_llm)
        second = upd.summarize_update_payload(payload, llm_callback=fake_llm)
        changed = upd.summarize_update_payload(
            {'webui': {'behind': 3, 'current_sha': 'abc', 'latest_sha': 'xyz', 'compare_url': 'https://example.test/webui2'}},
            llm_callback=fake_llm,
        )
        assert len(calls) == 2
        assert second['summary'] == first['summary']
        assert second['cached'] is True
        assert changed['summary'] != first['summary']

    def test_update_summary_cache_is_bounded_lru(self):
        import api.updates as upd

        upd._summary_cache.clear()
        calls = []

        def payload(n):
            return {
                'webui': {
                    'behind': n + 1,
                    'current_sha': f'old-{n}',
                    'latest_sha': f'new-{n}',
                    'compare_url': f'https://example.test/webui/{n}',
                },
            }

        def fake_llm(_system, prompt):
            calls.append(prompt)
            return f'- Generated summary #{len(calls)}'

        try:
            for i in range(upd._SUMMARY_CACHE_MAX):
                upd.summarize_update_payload(payload(i), llm_callback=fake_llm)

            assert len(upd._summary_cache) == upd._SUMMARY_CACHE_MAX
            assert len(calls) == upd._SUMMARY_CACHE_MAX

            first_again = upd.summarize_update_payload(payload(0), llm_callback=fake_llm)
            assert first_again['cached'] is True
            assert len(calls) == upd._SUMMARY_CACHE_MAX

            upd.summarize_update_payload(payload(upd._SUMMARY_CACHE_MAX), llm_callback=fake_llm)
            assert len(upd._summary_cache) == upd._SUMMARY_CACHE_MAX

            still_cached = upd.summarize_update_payload(payload(0), llm_callback=fake_llm)
            assert still_cached['cached'] is True
            assert len(calls) == upd._SUMMARY_CACHE_MAX + 1

            evicted = upd.summarize_update_payload(payload(1), llm_callback=fake_llm)
            assert evicted['cached'] is False
            assert len(calls) == upd._SUMMARY_CACHE_MAX + 2
        finally:
            upd._summary_cache.clear()

    def test_update_summary_can_be_generated_per_target_and_cached_separately(self):
        import api.updates as upd

        upd._summary_cache.clear()
        calls = []
        payload = {
            'webui': {'behind': 2, 'current_sha': 'web-a', 'latest_sha': 'web-b', 'compare_url': 'https://example.test/webui'},
            'agent': {'behind': 1, 'current_sha': 'agent-a', 'latest_sha': 'agent-b', 'compare_url': 'https://example.test/agent'},
        }

        def fake_llm(_system, prompt):
            calls.append(prompt)
            if 'Agent:' in prompt:
                return '- Agent startup is clearer.'
            return '- WebUI settings are easier to use.'

        webui = upd.summarize_update_payload(payload, target='webui', llm_callback=fake_llm)
        agent = upd.summarize_update_payload(payload, target='agent', llm_callback=fake_llm)
        webui_again = upd.summarize_update_payload(payload, target='webui', llm_callback=fake_llm)

        assert len(calls) == 2
        assert webui['target'] == 'webui'
        assert agent['target'] == 'agent'
        assert [t['name'] for t in webui['targets']] == ['webui']
        assert [t['name'] for t in agent['targets']] == ['agent']
        assert 'WebUI settings are easier to use.' in webui['summary']
        assert 'Agent startup is clearer.' in agent['summary']
        assert webui_again['cached'] is True
        assert webui_again['summary'] == webui['summary']


# ── Regression: force button reset on retry ──────────────────────────────────

class TestForceButtonResetOnRetry:
    """#813 UX: if a prior update attempt showed the force button (conflict),
    the next call to applyUpdates() must reset it — otherwise a subsequent
    non-conflict error (e.g. network) leaves the stale force button visible
    pointing at the wrong target."""

    def test_apply_updates_resets_force_button_at_start(self):
        src = read('static/ui.js')
        m = re.search(r'async function applyUpdates\b.*?\n\}', src, re.DOTALL)
        assert m, "applyUpdates() not found"
        fn = m.group(0)
        # The reset must appear BEFORE the main update loop, so it runs on
        # every retry — not only on first invocation.
        setup, _, rest = fn.partition('const targets=')
        assert 'btnForceUpdate' in setup, (
            "applyUpdates must reset btnForceUpdate visibility before "
            "starting the update loop (stale conflict state otherwise "
            "persists across retries)"
        )
        assert "display='none'" in setup or "display = 'none'" in setup, (
            "applyUpdates setup must hide btnForceUpdate via display:none"
        )


# ── #785: Manual 'Check for Updates' button ───────────────────────────────────

class TestCheckForUpdatesButton:
    """#785: Ensure the 'Check for Updates' button is wired up correctly."""

    def test_checkUpdatesNow_defined_in_panels(self):
        """checkUpdatesNow() function must exist in panels.js."""
        src = read('static/panels.js')
        assert 'function checkUpdatesNow' in src or 'async function checkUpdatesNow' in src, (
            "checkUpdatesNow() not found in panels.js"
        )

    def test_btnCheckUpdatesNow_in_html(self):
        """Button element with id='btnCheckUpdatesNow' must exist in index.html."""
        src = read('static/index.html')
        assert 'id="btnCheckUpdatesNow"' in src, (
            "btnCheckUpdatesNow element not found in index.html"
        )

    def test_checkUpdatesBlock_css_exists(self):
        """CSS rules for #checkUpdatesBlock and .btn-tiny must exist in style.css."""
        src = read('static/style.css')
        assert '#checkUpdatesBlock' in src, (
            "#checkUpdatesBlock CSS selector not found in style.css"
        )
        assert '.btn-tiny' in src, (
            ".btn-tiny CSS selector not found in style.css"
        )

    def test_check_now_i18n_key_exists(self):
        """settings_check_now i18n key must exist in all locale blocks."""
        src = read('static/i18n.js')
        count = src.count('settings_check_now')
        assert count >= 5, (
            f"settings_check_now found in only {count} locale blocks (expected ≥5: en, ru, es, zh, zh-Hant)"
        )
