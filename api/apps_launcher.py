"""Authenticated app links from the rendered installation environment only."""
from __future__ import annotations
import json
import os
import re
from urllib.parse import urlsplit
from api import app_catalog, modules
from api.org_modules import CATALOG, context
from api.helpers import j
from api.governance import delegation

_HOST = re.compile(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z')


def safe_link(value, environ):
    """Public subdomains or this node's exact private hostname. No request-derived trust."""
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value) or '\\' in value:
        return None
    try:
        u = urlsplit(value)
        host = (u.hostname or '').lower()
        # Opt-in, deployment-owned links for a standalone Pi desktop / SSH tunnel.
        # Never infer permission from request headers or allow arbitrary HTTP hosts.
        if (environ.get('SP_LOCAL_PI_APPS') == '1' and u.scheme == 'http'
                and host == '127.0.0.1' and u.port in (3000, 3001, 3003, 3009, 5220, 5227, 5678, 8289, 16081, 16082, 16083, 16084, 16085, 18130, 20128)
                and u.username is None and u.password is None and not u.query and not u.fragment):
            return value
        domain = environ.get('SP_DOMAIN', '').strip().lower()
        mesh = environ.get('SP_MESH_FQDN', '').strip().lower()
        if (u.scheme != 'https' or not _HOST.fullmatch(host) or '..' in host
                or u.username is not None or u.password is not None or u.query or u.fragment
                or any(name.lower() in host for name in modules.DISPLAY_DENYLIST)):
            return None
        port = u.port
        public = bool(domain and _HOST.fullmatch(domain) and '.' in domain
                      and (host == domain or host.endswith('.' + domain)))
        private = bool(mesh and _HOST.fullmatch(mesh) and host == mesh)
        if not (public or private) or (public and not private and port not in (None, 443)):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _links(key):
    """Only deployment-owned JSON dictionaries can supply destinations."""
    try:
        value = json.loads(os.environ.get(key, '{}'))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def shared_link(binding, policy, subject, scope):
    """A deployment-owned directory entry. App authentication stays at the app.

    Tenant identity, exact hosts and current department membership are required;
    this only grants launcher visibility and never shares credentials or data.
    """
    if not isinstance(binding, dict) or binding.get('org_id') != delegation.organization_id(policy):
        return None
    known = set(delegation.organization_people(policy)) | set(policy.bootstrap_admins) | set(policy.users)
    if not binding.get('org_id') or subject.normalized_email not in known:
        return None
    audience, departments = binding.get('scope'), binding.get('departments', [])
    if (audience not in ('organisation', 'departments') or not isinstance(departments, list) or len(departments) > 64
            or any(not isinstance(key, str) or key not in policy.departments for key in departments)
            or (audience == 'departments') != bool(departments)):
        return None
    if audience == 'departments' and scope.scope not in ('platform', 'org'):
        own = set(delegation.member_departments(policy, subject.normalized_email)) | set(delegation.admin_departments(policy, subject.normalized_email))
        if not own.intersection(departments):
            return None
    try:
        hosts = json.loads(os.environ.get('SP_SHARED_APP_HOSTS', '[]'))
        value = binding.get('url'); host = urlsplit(value).hostname
        if not isinstance(hosts, list) or not host or host not in hosts:
            return None
        url = safe_link(value, {'SP_DOMAIN': host})
    except (TypeError, ValueError, AttributeError):
        return None
    if not url:
        return None
    return {'url': url, 'placement': 'shared', 'shared_scope': audience,
            'shared_departments': [policy.departments[key].name for key in sorted(set(departments))]}


def handle_get(handler, parsed):
    try:
        policy, subject, scope = context(handler)
    except Exception:
        j(handler, {'error': 'policy_unavailable'}, status=503)
        return True
    if not subject.normalized_email or subject.provider == 'auth_disabled':
        j(handler, {'error': 'forbidden'}, status=403)
        return True
    from api import msp_service, control_plane_client
    managed = control_plane_client.configured()
    customer = {}
    if managed:
        status, customer = msp_service.overview(handler)
        if status != 200:
            j(handler, {'error': 'msp_unavailable', 'message': 'App availability could not be verified. Retry shortly.'}, status=503)
            return True
    customer_apps = {row['id']: row for row in customer.get('apps', [])}
    state = modules.current_state()
    platform = scope.scope == 'platform'
    editor = os.getenv('SP_WORKFLOWS_CLIENT_EDITOR') == '1'
    apps = []
    named_links = _links('SP_APP_LINKS')
    shared_apps = _links('SP_SHARED_APPS')
    for app_id, spec in app_catalog.REGISTRY.items():
        shared = shared_link(shared_apps[app_id], policy, subject, scope) if app_id in shared_apps else None
        if app_id in shared_apps and not shared:
            continue
        if app_id not in named_links and not managed and not shared:
            continue
        access = 'member' if app_id == 'n8n' and editor else spec.access
        if not platform and (access == 'platform' or (spec.module not in state.active and not managed and not shared)):
            continue
        value = shared['url'] if shared else named_links.get(app_id)
        unconfigured = value is None or value == ''
        url = shared['url'] if shared else None if unconfigured else safe_link(value, os.environ)
        if not unconfigured and url is None:
            continue
        apps.append({'id': app_id, 'module': spec.module,
                     'name': {'nl': spec.name, 'en': spec.name},
                     'description': {'nl': spec.description_nl, 'en': spec.description_en},
                     'category': spec.category, 'access': access,
                     'url': url, 'status': 'unconfigured' if unconfigured else 'configured', **(shared or {})})

    # Preserve the previous module-based launcher contract. Named apps do not
    # activate modules; platform users may discover existing external apps only.
    links = _links('SP_MODULE_LINKS')
    configured_ids = {row['id'] for row in apps if row['status'] == 'configured'}
    for mid in state.active:
        spec = modules.REGISTRY[mid]
        if not spec.app_tile:
            continue
        if mid == 'workflows' and not editor and not platform:
            continue
        if configured_ids.intersection(app_catalog.LEGACY_REPLACEMENTS.get(mid, ())):
            continue
        url = safe_link(links.get(mid), os.environ)
        if url:
            apps.append({'id': mid, 'module': mid, 'name': CATALOG[mid]['name'],
                         'description': CATALOG[mid]['summary'],
                         'category': 'creative' if mid == 'design_studio' else
                                     'automation' if mid == 'workflows' else 'productivity',
                         'access': 'platform' if mid == 'workflows' and not editor else 'member',
                         'url': url, 'status': 'configured'})
    for row in apps:
        managed_app = customer_apps.get(row['id'])
        if managed and managed_app:
            available = bool(row['url']) and (row.get('placement') == 'shared' or platform or row['module'] in state.active)
            row.update(enabled=managed_app['enabled'], support_levels=managed_app['support_levels'],
                       support=managed_app['support'], available=available)
            if not row['enabled'] or not available:
                row['url'] = None
    result = {'apps': apps}
    if managed:
        request_apps = customer.get('permissions', {}).get('request_apps', False)
        result['msp'] = {'connected': True, 'manage_apps': customer.get('permissions', {}).get('manage_apps', False),
                         'request_apps': request_apps}
        if request_apps:
            result['msp']['requestable_apps'] = [
                {'id': app_id, 'name': spec.name}
                for app_id, spec in app_catalog.REGISTRY.items()
                if spec.requestable and app_id in customer_apps
            ]
            result['msp']['departments'] = [{'id': key, 'name': department.name} for key, department in sorted(policy.departments.items())]
            result['msp']['app_intakes'] = customer.get('app_intakes', [])
    j(handler, result)
    return True
