import threading
from api import approvals, config, grant_requests


def test_admin_delivery_does_not_hold_registry_lock(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_WEBUI_GOVERNANCE_POLICY', raising=False)
    monkeypatch.setattr(config, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(grant_requests, '_load_spool', lambda: {
        'u|skill|example': {'email': 'u@example.test', 'gkind': 'skill', 'value': 'example'}
    })
    observed = []
    completed_during_delivery = []
    delivered = threading.Event()
    def deliver(entry):
        def reader():
            with approvals._REGISTRY_LOCK:
                observed.append(approvals.load()['grant:u|skill|example']['status'])
        worker = threading.Thread(target=reader)
        worker.start()
        worker.join(timeout=1)
        completed_during_delivery.append(not worker.is_alive())
        delivered.set()
        return True
    monkeypatch.setattr(grant_requests, '_notify_admins_of_request', deliver)
    assert grant_requests.ingest_spool() == 1
    assert delivered.wait(5), 'The asynchronous reviewer must finish before checking delivery'
    assert completed_during_delivery == [True]
    assert observed == ['pending'], 'External delivery must not block registry readers/decisions'
