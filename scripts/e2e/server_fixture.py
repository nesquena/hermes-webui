"""Isolated SynPulse QA server with real signed sessions and no production state."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from isolation import fixture_environment, install_guard

def main():
    repo = Path(sys.argv[1]).resolve()
    qa = Path(sys.argv[2]).resolve()
    assert qa != Path.home() and qa != repo and 'e2e-state' in qa.name, 'Refuse non-QA state directory'
    qa.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = qa / 'state'
    home = qa / 'home'
    workspace = qa / 'workspace'
    for path in (state, home, workspace):
        path.mkdir(mode=0o700, exist_ok=True)
    engine = str(Path(sys.argv[3]).resolve()) if len(sys.argv) > 3 else str(repo.parent / 'hermes-agent')
    runtime_python = sys.executable
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 19086
    provider_port = int(sys.argv[5]) if len(sys.argv) > 5 else 19087
    realtime_http_port = int(sys.argv[6]) if len(sys.argv) > 6 else None
    realtime_ws_port = int(sys.argv[7]) if len(sys.argv) > 7 else None
    qa_password = secrets.token_urlsafe(30)
    isolated_env = fixture_environment(qa)
    os.environ.clear()
    os.environ.update(isolated_env)
    install_guard(qa)
    os.environ.update({
        'SHELL': '/bin/sh',
        'HERMES_HOME': str(home),
        'HERMES_BASE_HOME': str(home),
        'HERMES_CONFIG_PATH': str(home / 'config.yaml'),
        'HERMES_WEBUI_STATE_DIR': str(state),
        'HERMES_WEBUI_DEFAULT_WORKSPACE': str(workspace),
        'HERMES_WEBUI_HOST': '127.0.0.1',
        'HERMES_WEBUI_PORT': str(port),
        'HERMES_WEBUI_PASSWORD': qa_password,
        'HERMES_WEBUI_PASSWORD_IDENTITY': 'admin@example.test',
        'HERMES_WEBUI_AGENT_DIR': engine,
        # Spawned CLI/Cron/Kanban workers must import the same reviewed engine
        # as the server. Keep sitecustomize's isolation guard first.
        'PYTHONPATH': os.pathsep.join([str(repo / 'scripts' / 'e2e'), engine, str(repo)]),
        'HERMES_WEBUI_PYTHON': runtime_python,
        'HERMES_WEBUI_SKIP_ONBOARDING': '1',
        'HERMES_WEBUI_PREWARM': '0',
        'HERMES_WEBUI_DISABLE_PROFILE_SYNC': '1',
        'HERMES_WEBUI_PLUGINS_DIR': str(home / 'plugins'),
        'HERMES_WEBUI_NANGO_API_URL': f'http://127.0.0.1:{provider_port}',
        'HERMES_WEBUI_NANGO_CONNECT_URL': f'http://127.0.0.1:{provider_port}',
        'HERMES_WEBUI_NANGO_SECRET_KEY_FILE': str(qa / 'intentionally-unconfigured-nango-secret'),
        'HERMES_WEBUI_NANGO_PROVIDERS_YAML': str(qa / 'intentionally-unconfigured-nango-providers.yaml'),
        'HERMES_WEBUI_GOVERNANCE_POLICY': str(home / 'dashboard-governance.yaml'),
        'AWS_EC2_METADATA_DISABLED': 'true',
    })
    sys.path[:0] = [str(repo), engine]
    from api.app_catalog import REGISTRY
    # Synthetic directory links are never followed: browser networking remains
    # loopback-only.
    os.environ.update({
        'SP_DOMAIN': 'example.test',
        'SP_MODULE_LINKS': json.dumps({'design_studio': 'https://design.example.test',
                                      'workflows': 'https://workflows.example.test'}),
        'SP_APP_LINKS': json.dumps({key: 'https://apps.example.test/' + key + '/'
                                   for key in REGISTRY}),
    })
    # Regression for module-launched workers: a fresh child in a private cwd
    # must resolve the selected checkout, independent of editable venv metadata.
    probe = json.loads(subprocess.check_output([runtime_python, '-c',
        'import json, os, hermes_cli.kanban_db as k; import isolation; '
        'print(json.dumps({"engine_module": k.__file__, "pid": os.getpid(), '
        '"guard_installed": isolation._INSTALLED}))'], cwd=workspace, text=True))
    assert Path(probe['engine_module']).resolve() == Path(engine) / 'hermes_cli' / 'kanban_db.py'
    assert probe['guard_installed'] is True
    (qa / 'runtime-engine.json').write_text(json.dumps(probe))
    os.chdir(repo)
    import yaml

    if realtime_http_port and realtime_ws_port:
        import requests
        from urllib.parse import urlsplit
        import websockets.sync.client
        original_post = requests.post
        original_connect = websockets.sync.client.connect
        def realtime_post(url, **kwargs):
            if url.startswith('https://api.openai.com/v1/realtime/calls'):
                url = f'http://127.0.0.1:{realtime_http_port}' + urlsplit(url).path
            return original_post(url, **kwargs)
        def realtime_connect(url, **kwargs):
            if url.startswith('wss://api.openai.com/v1/realtime?'):
                url = f'ws://127.0.0.1:{realtime_ws_port}/realtime?' + urlsplit(url).query
            return original_connect(url, **kwargs)
        requests.post = realtime_post
        websockets.sync.client.connect = realtime_connect
        os.environ['SYNPULSE_REALTIME_VOICE_ENABLED'] = '1'
        os.environ['OPENAI_API_KEY'] = 'qa-realtime-local-only'

    users = ['clionce', 'clideny', 'cliauto', 'grantapprove', 'grantdeny', 'grantmanual', 'grantharddeny', 'climcp', 'continuation', 'govrequest', 'resource', 'admin', 'alice', 'bob', 'outsider', 'denied', 'voicedenied', 'autoapprove', 'autodeny', 'automanual', 'manualapprove', 'manualdeny']
    permissions = ['chat:use', 'sessions:read', 'sessions:write', 'profiles:read',
                   'files:read', 'files:write', 'cron:read', 'cron:write',
                   'skills:read', 'skills:write', 'workspaces:read', 'workspaces:write', 'memory:read', 'memory:write', 'integrations:read',
                   'integrations:connect', 'model:read', 'model:write', 'config:read']
    policy = {
        'version': 1, 'mode': 'enforce', 'default_effect': 'deny',
        'bootstrap_admins': ['admin@example.test'],
        'roles': {'member': {'grants': {
            'permissions': permissions, 'profiles': ['default', 'qa-research'],
            'routes': ['*'], 'files': {'read_roots': [str(workspace)], 'write_roots': [str(workspace)]},
            'workspaces': [str(workspace)], 'models': {'providers': ['*'], 'models': ['*']},
            'skills': {'view': ['*'], 'load': ['*']},
            'tools': {'toolsets': ['file', 'delegation', 'todo'], 'builtins': ['read_file','write_file','delegate_task','delegate_to_bot','todo','todo_list']},
        }}},
        'users': {u + '@example.test': {'roles': ['member']} for u in users if u not in {'admin', 'denied'}},
    }
    for who, mode, rules in [('autoapprove','automatic','QA_ALLOW_ONLY: approve synthetic access requests.'),('autodeny','automatic','QA_DENY_ONLY: reject synthetic access requests.'),('automanual','automatic','QA_MANUAL_ONLY: require a human review.'),('manualapprove','manual',''),('manualdeny','manual','')]:
        policy['users'][who+'@example.test'].update({'access_level':'user','access_mode':'blacklist','approval':{'mode':mode,'prompt':rules}})
    policy['users']['voicedenied@example.test']['deny'] = {'models': {'providers': ['openai']}}
    # A creator can configure bots without becoming a governance administrator.
    policy['roles']['bot_creator'] = {'grants': {
        'permissions': ['profiles:admin'], 'cli': {'commands': ['git']},
        'mcp': {'servers': ['qa-local']}}}
    policy['users']['alice@example.test']['roles'].append('bot_creator')
    # Supplemental capability tests use a separate identity and role. Existing
    # personas keep their original envelopes. Tools still need actual grants.
    policy['roles']['qa_cli_mcp'] = {'grants': {
        'permissions': ['terminal:use', 'mcp:read'],
        'tools': {'builtins': ['terminal'], 'toolsets': ['terminal']},
        'cli': {'commands': ['*'], 'workdir_roots': [str(workspace)]},
        'mcp': {'servers': ['qa-stdio'], 'tools': {'qa-stdio': ['*']}},
    }}
    policy['users']['climcp@example.test']['roles'].append('qa_cli_mcp')
    # Per-call review is a CLI policy floor, independent of the per-person
    # access-request reviewer. Keep these personas isolated from the ordinary
    # file-tool tests and from the supplement that edits its own CLI policy.
    policy['roles']['qa_cli_review'] = {'grants': {
        'permissions': ['terminal:use'],
        'tools': {'builtins': ['terminal'], 'toolsets': ['terminal']},
        'cli': {'commands': ['touch'], 'approval_commands': ['touch'],
                'workdir_roots': [str(workspace)]},
    }}
    for who in ('clionce', 'clideny', 'cliauto'):
        policy['users'][who+'@example.test'].update({
            'roles': ['member', 'qa_cli_review'], 'access_level': 'elevated',
            'access_mode': 'blacklist', 'approval': {
                'mode': 'automatic' if who == 'cliauto' else 'manual',
                'prompt': 'QA_ALLOW_ONLY: approve synthetic access requests.' if who == 'cliauto' else ''}})
    # A missing direct whitelist route generates a real grant request. The
    # member role is its upper bound; an explicit deny must remain ungrantable.
    for who, rules in (('grantapprove', 'QA_ALLOW_ONLY'), ('grantdeny', 'QA_DENY_ONLY'),
                       ('grantmanual', 'QA_MANUAL_ONLY'), ('grantharddeny', 'QA_ALLOW_ONLY')):
        policy['users'][who+'@example.test'].update({
            'access_level': 'user', 'access_mode': 'whitelist',
            'approval': {'mode': 'automatic', 'prompt': rules + ': review the synthetic access request.'},
            'grants': {'permissions': ['*'], 'profiles': ['*'], 'routes': [],
                       'skills': {'view': ['*'], 'load': ['*']}}})
    policy['users']['grantharddeny@example.test']['deny'] = {'routes': ['/api/skills']}
    policy['groups'] = {'qa-team': {'roles': ['member']}}
    policy['groups']['qa-sso-restricted'] = {'grants': {'files': {
        'denied_globs': [str(workspace / 'qa-continuation-protected-*')]}}}
    policy['users']['bob@example.test']['groups'] = ['qa-team']
    policy['installation'] = {'kind': 'client'}
    policy['organization'] = {'id': 'org_qa', 'name': 'QA organisation',
                              'admins': ['alice@example.test'], 'billing_admins': []}
    (home / 'dashboard-governance.yaml').write_text(yaml.safe_dump(policy))
    config = {'model': {'default': 'qa-deterministic', 'provider': 'custom:qa',
                        'base_url': f'http://127.0.0.1:{provider_port}/v1'},
              'custom_providers': [{'name': 'qa', 'base_url': f'http://127.0.0.1:{provider_port}/v1', 'api_key': 'qa-local-only'}],
              'toolsets': ['file','delegation','todo'], 'max_turns': 12,
              'agent': {'max_turns': 12}, 'delegation': {'max_concurrent_children': 3}}
    config['webui_passkey_enabled'] = True
    config['mcp_servers'] = {'qa-local': {'command': '/usr/bin/false', 'enabled': False}}
    config['mcp_servers']['qa-stdio'] = {
        'command': runtime_python,
        'args': [str(repo / 'scripts/e2e/provider_fixture.py'), '--mcp', str(qa)],
        'enabled': False, 'timeout': 10,
    }
    # The supplement requests terminal in its own session override. No added
    # terminal toolset is enabled for existing ordinary fixture conversations.
    dashboard = home / 'plugins' / 'qa-dashboard' / 'dashboard'
    (dashboard / 'dist').mkdir(parents=True, exist_ok=True)
    (dashboard / 'manifest.json').write_text(json.dumps({
        'name': 'qa-dashboard', 'label': 'QA Dashboard', 'version': '1.0.0',
        'description': 'Synthetic local dashboard for browser QA.',
        'tab': {'path': '/qa-dashboard', 'label': 'QA Dashboard'},
    }))
    (dashboard / 'dist' / 'index.js').write_text(
        "document.body.append(Object.assign(document.createElement('p'),{textContent:'QA_PLUGIN_PAGE'}));\n")
    skill_dir = home / 'skills' / 'qa-review'
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / 'SKILL.md').write_text('---\nname: qa-review\ndescription: Review synthetic QA evidence\n---\nSummarize only the synthetic evidence supplied.\n')
    (home / 'config.yaml').write_text(yaml.safe_dump(config))
    (workspace / 'qa-evidence.txt').write_text('Synthetic QA workspace evidence.\n')
    (workspace / 'qa-folder').mkdir(exist_ok=True)
    (state / 'settings.json').write_text(json.dumps({'show_cli_sessions': False}))
    (home / 'SOUL.md').write_text('QA_BOT_DEFAULT. You are SynPulse QA. Answer the request briefly. Do not perform external actions.\n')
    bot = home / 'profiles' / 'qa-research'
    bot.mkdir(parents=True, exist_ok=True)
    (bot / 'config.yaml').write_text(yaml.safe_dump(config))
    (bot / 'SOUL.md').write_text('QA_BOT_RESEARCH. You are the Research bot. Answer briefly.\n')
    (bot / 'profile.yaml').write_text(yaml.safe_dump({'version': 1, 'name': 'qa-research', 'ui_meta': {'hermes-bots': {'title': 'Research', 'description': 'Research bot for QA'}}}))
    from api import auth
    cookies = {u: auth.create_session({'email': u + '@example.test', 'groups': ['qa-sso-restricted'] if u == 'continuation' else [],
                                     'claims_subset': {'name': u.title()}, 'method': 'qa_seed'}) for u in users}
    from extension_fixture import setup as setup_extension_fixture
    extension_fixture = setup_extension_fixture(qa)
    from interrupt_fixture import setup as setup_interrupt_fixture
    setup_interrupt_fixture(qa)
    private = qa / 'browser-sessions.json'
    private.write_text(json.dumps({'cookies': cookies, 'cookie_name': auth.COOKIE_NAME, 'base_url': f'http://127.0.0.1:{port}', 'login_password': qa_password, 'workspace': str(workspace), 'engine': engine, 'extension_fixture': extension_fixture, 'realtime_http_base': f'http://127.0.0.1:{realtime_http_port}' if realtime_http_port else None, 'realtime_ws_base': f'ws://127.0.0.1:{realtime_ws_port}' if realtime_ws_port else None}))
    private.chmod(0o600)
    print('QA signed sessions prepared for admin, alice, bob, outsider; production state isolated.', flush=True)
    import server
    server.main()


if __name__ == "__main__":
    main()
