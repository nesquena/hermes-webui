from types import SimpleNamespace
from api import control_plane_client
from tests import test_org_api_read as org_fixture
from tests.test_org_api_read import _Handler, PLATFORM

home = org_fixture.home
write_policy = org_fixture.write_policy
caller = org_fixture.caller


def test_app_intake_uses_policy_departments_and_signed_identity(write_policy, caller, monkeypatch):
    from api import msp_service
    import uuid
    caller.email = 'owner@acme.example'; calls = []
    monkeypatch.setattr(control_plane_client, 'call', lambda *a, **k: (calls.append((a, k)) or (201, {'id': 'tkt_test'})))
    body = {'app_id': 'opendesign', 'scope': 'departments', 'departments': ['sales', 'finance'],
            'note': 'Shared design for both teams.', 'request_key': str(uuid.uuid4())}
    h = _Handler(); assert msp_service.handle_post(h, '/api/msp/app-intake', body)
    assert h.status == 201
    assert calls[0][0] == ('POST', '/v1/customer/apps/opendesign/intake')
    assert calls[0][1]['body']['departments'] == ['finance', 'sales']
    assert calls[0][1]['acting']['user'] == caller.email
    for extra in ({'departments': ['unknown']}, {'departments': [True]}, {'org_id': 'other'}, {'scope': 'organisation'}):
        h = _Handler(); msp_service.handle_post(h, '/api/msp/app-intake', {**body, **extra})
        assert h.status == 422
    assert len(calls) == 1


def test_msp_proxy_binds_identity_not_browser_body(write_policy,caller,monkeypatch):
    from api import msp_service
    caller.email=PLATFORM[0]
    calls=[]
    monkeypatch.setattr(control_plane_client,'call',lambda *args,**kwargs:(calls.append((args,kwargs)) or (200,{'tickets':[]})))
    h=_Handler();msp_service.handle_get(h,SimpleNamespace(path='/api/msp/tickets',query='scope=mine'))
    assert h.status==200
    assert calls[0][1]['acting']['user']==PLATFORM[0]
    h=_Handler();msp_service.handle_post(h,'/api/msp/app-state',{'app_id':'opendesign','enabled':False,'org_id':'another'})
    assert h.status==422 and len(calls)==1


def test_unknown_and_anonymous_users_cannot_reach_msp(write_policy,caller,monkeypatch):
    from api import msp_service
    monkeypatch.setattr(control_plane_client,'call',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')))
    for who in [None,'stranger@elsewhere.example']:
        caller.email=who;h=_Handler();msp_service.handle_get(h,SimpleNamespace(path='/api/msp/overview',query=''))
        assert h.status==403


def test_job_cost_proxy_rejects_spoofed_profile_and_job(write_policy,caller,monkeypatch):
    from api import msp_service, profiles
    caller.email=PLATFORM[0]
    monkeypatch.setattr(profiles,'get_active_profile_name',lambda:'default')
    monkeypatch.setattr(control_plane_client,'call',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')))
    for body in ({'job_id':{},'profile':'default'}, {'job_id':'valid','profile':'other'}, {'job_id':'valid','org_id':'other'}):
        h=_Handler();msp_service.handle_post(h,'/api/msp/job-cost',body)
        assert h.status==422


def test_paid_order_proxy_binds_identity_and_rejects_price_or_status(write_policy,caller,monkeypatch):
    from api import msp_service
    caller.email=PLATFORM[0];calls=[]
    monkeypatch.setattr(control_plane_client,'call',lambda *a,**k:(calls.append((a,k)) or (200,{'status':'offered'})))
    h=_Handler();assert msp_service.handle_post(h,'/api/msp/order',{'order_id':'so_example','action':'submit'})
    assert h.status==200
    assert calls[0][0]==('POST','/v1/customer/orders/so_example/submit')
    assert calls[0][1]['body']=={}
    assert calls[0][1]['acting']['user']==PLATFORM[0]
    for extra in [{'paid':True},{'amount':1},{'org_id':'other'},{'user':'other'}]:
        h=_Handler();msp_service.handle_post(h,'/api/msp/order',{'order_id':'so_example','action':'submit',**extra});assert h.status==422
    assert len(calls)==1


def test_invoice_proxy_keeps_customer_scope_and_hides_provider_choices(write_policy,caller,monkeypatch):
    from api import msp_service
    caller.email=PLATFORM[0];calls=[]
    monkeypatch.setattr(control_plane_client,'call',lambda *a,**k:(calls.append((a,k)) or (200,{'content_type':'application/pdf','pdf_base64':'JVBERi0='})))
    h=_Handler();assert msp_service.handle_post(h,'/api/msp/invoice',{'request_id':'br_example'})
    assert h.status==200 and calls[0][0]==('POST','/v1/customer/invoices/br_example/download')
    assert calls[0][1]['body']=={} and calls[0][1]['acting']['user']==PLATFORM[0]
    assert calls[0][1]['timeout']==60
    for extra in [{'org_id':'other'},{'invoice_id':'invoice_other'},{'url':'https://other.test'},{'paid':True}]:
        h=_Handler();msp_service.handle_post(h,'/api/msp/invoice',{'request_id':'br_example',**extra});assert h.status==422
    assert len(calls)==1


def test_cloud_requires_personal_session_even_for_local_admin(write_policy,caller,monkeypatch):
    from api import auth, msp_service
    write_policy()
    caller.email=PLATFORM[0]
    monkeypatch.setenv('SP_CLOUD_PERSONAL_MSP','1')
    monkeypatch.setattr(auth,'cloud_msp_proof',lambda handler:None)
    monkeypatch.setattr(control_plane_client,'call',lambda *a,**k: __import__('pytest').fail('A shared credential cannot contact MSP'))
    status, error=msp_service.fetch(_Handler(),'/v1/customer/overview')
    assert status==403 and error['error']['code']=='personal_login_required'


def test_personal_handoff_keeps_secret_out_of_identity(monkeypatch):
    from api import auth
    monkeypatch.setattr(auth,'_save_sessions',lambda sessions:None)
    proof='a'*64
    cookie=auth.create_session({'method':'synthpulse_cloud','email':'tester@example.com','_cloud_msp_proof':proof})
    assert '_cloud_msp_proof' not in auth.get_session_identity(cookie)
    monkeypatch.setattr(auth,'parse_cookie',lambda handler:cookie)
    assert auth.cloud_msp_proof(object())==proof


def test_cloud_login_refuses_another_origin_before_exchange(monkeypatch):
    from api import cloud_login
    monkeypatch.setenv('SP_CLOUD_PERSONAL_MSP','1')
    monkeypatch.setattr(cloud_login,'j',lambda handler,payload,status: setattr(handler,'status',status))
    handler=SimpleNamespace(headers={'Host':'work.example','Origin':'https://evil.example','X-SynthPulse-Login':'1'})
    assert cloud_login.redeem(handler,{'code':'b'*64})
    assert handler.status==403
