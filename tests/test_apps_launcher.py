"""No request-controlled links, dormant apps, workflow editor leakage or unsafe origins."""
import json
from types import SimpleNamespace
import pytest
from api import apps_launcher, modules
from tests import test_org_api_read as org_fixture
from tests.test_org_api_read import _Handler, PLATFORM, NOBODY

# Register shared pytest fixtures without shadowing imported names in tests.
home = org_fixture.home
write_policy = org_fixture.write_policy
caller = org_fixture.caller


@pytest.mark.parametrize('url',[
'http://design.example.test','javascript:alert(1)','https://example.test.evil.test',
'https://evilexample.test','https://n8n.example.test','https://opendesign.example.test',
'https://user:pass@design.example.test','https://design.example.test:444',
'https://design.example.test?token=secret','https://design.example.test/#fragment',
'https://design.example.test\\@evil.test','https://design.example.test\n',
'https://node.tail.test.evil.test:9443','https://evil.node.tail.test:9443',None,{},
])
def test_unsafe_links(url):
    assert apps_launcher.safe_link(url,{'SP_DOMAIN':'example.test','SP_MESH_FQDN':'node.tail.test'}) is None


def test_domain_requires_trusted_config():
    assert apps_launcher.safe_link('https://design.example.test',{}) is None
    assert apps_launcher.safe_link('https://design.example.test',{'SP_DOMAIN':'example.test'})
    assert apps_launcher.safe_link('https://node.tail.test:9443/app',{'SP_MESH_FQDN':'node.tail.test'})


def get(caller,email):
    caller.email=email;h=_Handler();apps_launcher.handle_get(h,SimpleNamespace(path='/api/apps',query='url=https://evil.test'));return h


@pytest.mark.parametrize('editor',[False,True])
@pytest.mark.parametrize('admin',[False,True])
def test_tiles_scope_and_active_only(write_policy,caller,monkeypatch,editor,admin):
    monkeypatch.setenv('SP_DOMAIN','example.test');monkeypatch.setenv('SP_ENABLED_MODULES','')
    monkeypatch.setenv('SP_WORKFLOWS_CLIENT_EDITOR','1' if editor else '0')
    monkeypatch.setenv('SP_MODULE_LINKS',json.dumps({'design_studio':'https://design.example.test','workflows':'https://workflows.example.test','notebook':'https://notebook.example.test','office_apps':'https://office.example.test'}))
    modules.clear_cache();h=get(caller,PLATFORM[0] if admin else NOBODY[0]);assert h.status==200
    assert [a['module'] for a in h.body['apps']]==(['design_studio','workflows'] if admin or editor else ['design_studio'])
    monkeypatch.setenv('SP_ENABLED_MODULES','notebook');modules.clear_cache()
    assert 'notebook' in [a['module'] for a in get(caller,NOBODY[0]).body['apps']]


@pytest.mark.parametrize('links',['bad','[]','null','{}'])
def test_empty_and_malformed_links(write_policy,caller,monkeypatch,links):
    monkeypatch.setenv('SP_MODULE_LINKS',links)
    assert get(caller,NOBODY[0]).body=={'apps':[]}


def test_anonymous_auth_disabled(write_policy,caller):
    assert get(caller,None).status==403
    caller.method='auth_disabled'
    assert get(caller,PLATFORM[0]).status==403


CRAFT_IDS = ('photocraft', 'filmcraft', 'effectcraft', 'vectorcraft',
             'lightcraft', 'designcraft', 'pdfcraft', 'soundcraft')


@pytest.fixture(autouse=True)
def clean_app_links(monkeypatch):
    monkeypatch.delenv('SP_APP_LINKS', raising=False)
    monkeypatch.delenv('SP_MODULE_LINKS', raising=False)
    monkeypatch.delenv('SP_ENABLED_MODULES', raising=False)
    monkeypatch.delenv('SP_WORKFLOWS_CLIENT_EDITOR', raising=False)
    monkeypatch.delenv('SP_SHARED_APPS', raising=False)
    monkeypatch.delenv('SP_SHARED_APP_HOSTS', raising=False)
    modules.clear_cache()


def test_one_shared_app_filters_departments_and_never_activates_local_modules(write_policy, caller, monkeypatch):
    monkeypatch.setenv('SP_ENABLED_MODULES', '')
    monkeypatch.setenv('SP_SHARED_APP_HOSTS', '["shared.acme.example"]')
    spec = {'org_id': 'org_acme', 'scope': 'departments', 'departments': ['sales'], 'url': 'https://shared.acme.example/office'}
    monkeypatch.setenv('SP_SHARED_APPS', json.dumps({'docuseal': spec}))
    sales = get(caller, 'rep.sales@acme.example').body['apps']
    assert len(sales) == 1 and sales[0]['id'] == 'docuseal'
    assert sales[0]['placement'] == 'shared' and sales[0]['shared_scope'] == 'departments'
    assert sales[0]['shared_departments'] == ['Sales']
    assert 'e_signing' not in modules.current_state().active
    assert get(caller, 'clerk.finance@acme.example').body == {'apps': []}
    assert get(caller, 'stranger@elsewhere.example').body == {'apps': []}
    monkeypatch.setenv('SP_SHARED_APPS', json.dumps({'docuseal': {**spec, 'scope': 'organisation', 'departments': []}}))
    assert len(get(caller, 'clerk.finance@acme.example').body['apps']) == 1
    assert get(caller, 'stranger@elsewhere.example').body == {'apps': []}


@pytest.mark.parametrize('change', [
    {'org_id': 'other'}, {'scope': 'unknown'}, {'departments': ['unknown']},
    {'url': 'https://shared.acme.example.evil.test'}, {'url': 'https://shared.acme.example:9443'},
    {'url': 'https://user:secret@shared.acme.example'}, {'url': 'https://shared.acme.example?token=secret'},
])
def test_shared_app_configuration_fails_closed(write_policy, caller, monkeypatch, change):
    monkeypatch.setenv('SP_SHARED_APP_HOSTS', '["shared.acme.example"]')
    spec = {'org_id': 'org_acme', 'scope': 'departments', 'departments': ['sales'], 'url': 'https://shared.acme.example/office'}
    monkeypatch.setenv('SP_SHARED_APPS', json.dumps({'docuseal': {**spec, **change}}))
    assert get(caller, 'owner@acme.example').body == {'apps': []}


def test_complete_craft_studio_registry(write_policy, caller, monkeypatch):
    monkeypatch.setenv('SP_MESH_FQDN', 'node.tail.test')
    ids = ('opendesign', 'craft_studio') + CRAFT_IDS
    monkeypatch.setenv('SP_APP_LINKS', json.dumps({
        app_id: f'https://node.tail.test:9443/{app_id}' for app_id in ids
    }))
    rows = get(caller, NOBODY[0]).body['apps']
    assert {row['id'] for row in rows} == set(ids)
    assert all(row['module'] == 'design_studio' and row['category'] ==
               'creative'
               and row['access'] == 'member' and row['status'] == 'configured'
               and set(row['name']) == {'nl', 'en'}
               and set(row['description']) == {'nl', 'en'} for row in rows)


@pytest.mark.parametrize('email', [NOBODY[0], 'owner@acme.example', 'finance@acme.example'])
def test_admin_apps_are_not_exposed_to_other_scopes(write_policy, caller, monkeypatch, email):
    ids = ('n8n', 'omniroute', 'nango', 'ragflow', 'langfuse')
    monkeypatch.setenv('SP_MESH_FQDN', 'node.tail.test')
    monkeypatch.setenv('SP_APP_LINKS', json.dumps({app_id: None for app_id in ids}))
    assert get(caller, email).body == {'apps': []}
    rows = get(caller, PLATFORM[0]).body['apps']
    assert {row['id'] for row in rows} == set(ids)
    assert all(row['url'] is None and row['status'] == 'unconfigured'
               and row['access'] == 'platform' for row in rows)


def test_named_workflow_editor_opt_in(write_policy, caller, monkeypatch):
    monkeypatch.setenv('SP_APP_LINKS', '{"n8n":null,"omniroute":null}')
    monkeypatch.setenv('SP_WORKFLOWS_CLIENT_EDITOR', '1')
    rows = get(caller, NOBODY[0]).body['apps']
    assert [(row['id'], row['access']) for row in rows] == [('n8n', 'member')]


def test_module_gate_for_members_admin_directory_does_not_activate(write_policy, caller, monkeypatch):
    monkeypatch.setenv('SP_DOMAIN', 'example.test')
    monkeypatch.setenv('SP_APP_LINKS', '{"docuseal":"https://sign.example.test"}')
    monkeypatch.setenv('SP_MODULE_LINKS', '{"e_signing":"https://legacy.example.test"}')
    assert get(caller, NOBODY[0]).body == {'apps': []}
    rows = get(caller, PLATFORM[0]).body['apps']
    assert [row['id'] for row in rows] == ['docuseal']
    assert not modules.module_active('e_signing')
    blocked = _Handler()
    assert modules.gate(blocked, SimpleNamespace(path='/api/signing/documents'))
    assert blocked.status == 403
    monkeypatch.setenv('SP_ENABLED_MODULES', 'e_signing')
    modules.clear_cache()
    assert [row['id'] for row in get(caller, NOBODY[0]).body['apps']] == ['docuseal']


@pytest.mark.parametrize('raw', ['bad', '[]', 'null', 'false', '12'])
def test_named_malformed_fail_closed_preserves_legacy(write_policy, caller, monkeypatch, raw):
    monkeypatch.setenv('SP_DOMAIN', 'example.test')
    monkeypatch.setenv('SP_APP_LINKS', raw)
    monkeypatch.setenv('SP_MODULE_LINKS', '{"design_studio":"https://design.example.test"}')
    rows = get(caller, PLATFORM[0]).body['apps']
    assert [row['id'] for row in rows] == ['design_studio']
    assert rows[0]['module'] == 'design_studio'


def test_empty_null_unknown_and_invalid_named_entries(write_policy, caller, monkeypatch):
    monkeypatch.setenv('SP_DOMAIN', 'example.test')
    monkeypatch.setenv('SP_APP_LINKS', json.dumps({
        'opendesign': '', 'craft_studio': None,
        'photocraft': 'https://photo.example.test/edit',
        'filmcraft': {'url': 'https://film.example.test', 'name': 'Fake'},
        'effectcraft': [], 'vectorcraft': False,
        'injected': 'https://injected.example.test', '<script>': None,
    }))
    rows = get(caller, NOBODY[0]).body['apps']
    assert [(row['id'], row['status'], row['url']) for row in rows] == [
        ('opendesign', 'unconfigured', None), ('craft_studio', 'unconfigured', None),
        ('photocraft', 'configured', 'https://photo.example.test/edit'),
    ]
    assert rows[0]['name'] == {'nl': 'OpenDesign', 'en': 'OpenDesign'}


@pytest.mark.parametrize('url', [
    'https://evil.test', 'https://example.test.evil.test',
    'https://user:secret@design.example.test', 'https://design.example.test?token=secret',
    'https://design.example.test#token', 'http://design.example.test',
    'https://node.tail.test.evil.test:9443', 'https://127.0.0.1',
    'https://169.254.169.254/latest', 'https://design.example.test:444',
    'https://design.example.test\\@evil.test', 'https://n8n.example.test',
])
def test_named_urls_keep_trusted_origin_boundary(write_policy, caller, monkeypatch, url):
    monkeypatch.setenv('SP_DOMAIN', 'example.test')
    monkeypatch.setenv('SP_MESH_FQDN', 'node.tail.test')
    monkeypatch.setenv('SP_APP_LINKS', json.dumps({'opendesign': url}))
    assert get(caller, PLATFORM[0]).body == {'apps': []}


@pytest.mark.parametrize('replacement', [None, '', 'https://evil.test', 'https://design.example.test/new'])
def test_legacy_replacement_only_when_named_app_configured(write_policy, caller, monkeypatch, replacement):
    monkeypatch.setenv('SP_DOMAIN', 'example.test')
    monkeypatch.setenv('SP_APP_LINKS', json.dumps({'opendesign': replacement,
                                                 'n8n': 'https://workflows.example.test/new'}))
    monkeypatch.setenv('SP_MODULE_LINKS', json.dumps({
        'design_studio': 'https://design.example.test',
        'workflows': 'https://workflows.example.test',
    }))
    rows = get(caller, PLATFORM[0]).body['apps']
    ids = [row['id'] for row in rows]
    assert ids.count('n8n') == 1 and 'workflows' not in ids
    assert ('design_studio' in ids) == (replacement != 'https://design.example.test/new')


def test_policy_failure_exposes_no_registry(write_policy, caller, monkeypatch):
    monkeypatch.setenv('SP_APP_LINKS', '{"opendesign":null}')
    def unavailable(handler):
        raise RuntimeError('unavailable')
    monkeypatch.setattr(apps_launcher, 'context', unavailable)
    response = get(caller, PLATFORM[0])
    assert response.status == 503 and response.body == {'error': 'policy_unavailable'}


def test_core_stack_includes_knowledge_and_observability_without_personal_dashboards(write_policy, caller, monkeypatch):
    monkeypatch.setenv('SP_MESH_FQDN', 'node.tail.test')
    ids = ['ragflow', 'langfuse', 'pareltje', 'silverbullet', 'slibdetect', 'pixel_office', 'mnemosyne', 'strategy_gantt', 'openwebui', 'cockpit']
    monkeypatch.setenv('SP_APP_LINKS', json.dumps({key: 'https://node.tail.test:9443/' for key in ids}))
    rows = get(caller, PLATFORM[0]).body['apps']
    assert {row['id'] for row in rows} == {'ragflow', 'langfuse'}
    assert get(caller, NOBODY[0]).body['apps'] == []


@pytest.mark.parametrize('app_id,url,allowed', [
    ('hermes_docs', 'https://node.tail.test:8130/docs/', True),
    ('hermes_docs', 'https://github.com/other/repo/releases/latest', False),
    ('hermes_docs', 'https://github.com/criptogus/HermesOffice/releases/latest?next=evil', False),
    ('opendesign', 'https://github.com/criptogus/HermesOffice/releases/latest', False),
])
def test_office_links_use_the_same_trusted_host_policy(write_policy, caller, monkeypatch, app_id, url, allowed):
    monkeypatch.setenv('SP_MESH_FQDN', 'node.tail.test')
    monkeypatch.setenv('SP_APP_LINKS', json.dumps({app_id: url}))
    rows = get(caller, PLATFORM[0]).body['apps']
    assert bool(rows) is allowed
    if allowed:
        assert rows[0]['url'] == url and rows[0]['access'] == 'platform'


def test_managed_app_off_hides_destination_and_fails_closed(write_policy,caller,monkeypatch):
    from api import control_plane_client, msp_service
    monkeypatch.setenv('SP_DOMAIN','example.test')
    monkeypatch.setenv('SP_APP_LINKS','{"opendesign":"https://design.example.test"}')
    monkeypatch.setattr(control_plane_client,'configured',lambda:True)
    monkeypatch.setattr(msp_service,'overview',lambda h:(200,{'apps':[{'id':'opendesign','enabled':False,'support_levels':[],'support':None}],'permissions':{'manage_apps':True}}))
    h=get(caller,PLATFORM[0]);assert h.status==200
    app=next(a for a in h.body['apps'] if a['id']=='opendesign')
    assert app['url'] is None and app['enabled'] is False and app['available'] is True
    assert h.body['msp']['manage_apps'] is True
    monkeypatch.setattr(msp_service,'overview',lambda h:(503,{'error':'unavailable'}))
    h=get(caller,PLATFORM[0]);assert h.status==503 and 'apps' not in h.body


@pytest.mark.parametrize('can_request', [True, False])
def test_request_catalog_is_separate_from_launch_permissions(write_policy, caller, monkeypatch, can_request):
    from api import control_plane_client, msp_service
    monkeypatch.setattr(control_plane_client, 'configured', lambda: True)
    ids = ('twenty', 'ragflow', 'n8n', 'omniroute', 'nango', 'langfuse', 'unknown')
    monkeypatch.setattr(msp_service, 'overview', lambda h: (200, {
        'apps': [{'id': ident, 'enabled': False, 'support_levels': [], 'support': None} for ident in ids],
        'permissions': {'manage_apps': True, 'request_apps': can_request},
    }))
    body = get(caller, 'owner@acme.example').body
    assert not set(ids).intersection(row['id'] for row in body['apps'])
    if not can_request:
        assert 'requestable_apps' not in body['msp']
        return
    catalog = body['msp']['requestable_apps']
    assert {row['id'] for row in catalog} == {'twenty', 'ragflow', 'n8n'}
    assert next(row['name'] for row in catalog if row['id'] == 'twenty') == 'Twenty CRM'
    assert all(set(row) == {'id', 'name'} for row in catalog)
    assert not modules.module_active('knowledge_base')


def test_pi_loopback_apps_are_explicit_and_port_limited():
    env={'SP_LOCAL_PI_APPS':'1'}
    for url in ('http://127.0.0.1:5220/photocraft/', 'http://127.0.0.1:16081/vnc.html', 'http://127.0.0.1:20128'):
        assert apps_launcher.safe_link(url,{}) is None
        assert apps_launcher.safe_link(url,env)==url
    for url in ('http://evil.test:5220/', 'http://127.0.0.1:22/', 'http://user@127.0.0.1:5220/', 'http://127.0.0.1:5220/?token=x'):
        assert apps_launcher.safe_link(url,env) is None


def test_pi_office_gateway_is_explicitly_opt_in():
    from api.apps_launcher import safe_link
    assert safe_link("http://127.0.0.1:18130/docs/", {"SP_LOCAL_PI_APPS":"1"})
    assert safe_link("http://127.0.0.1:18130/docs/", {}) is None
    assert safe_link("http://example.com:18130/docs/", {"SP_LOCAL_PI_APPS":"1"}) is None
