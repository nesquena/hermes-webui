"""Registration must distinguish the COSE key from authenticator extensions."""
import hashlib
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from tests.test_passkey_auth import _set_paths, _client_data, FakeHandler, cbor, b64u


def register(monkeypatch, tmp_path, flags, suffix):
    passkeys = _set_paths(monkeypatch, tmp_path)
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    cose = {1: 2, 3: -7, -1: 1, -2: key.x.to_bytes(32, 'big'), -3: key.y.to_bytes(32, 'big')}
    opts = passkeys.registration_options(FakeHandler())
    _, client = _client_data('webauthn.create', opts['challenge'])
    cid = b'extension-credential'
    auth = (hashlib.sha256(opts['rp']['id'].encode()).digest() + bytes([flags])
            + (1).to_bytes(4, 'big') + bytes(16) + len(cid).to_bytes(2, 'big')
            + cid + cbor(cose) + suffix)
    return passkeys.finish_registration({'response': {'clientDataJSON': client,
        'attestationObject': b64u(cbor({'fmt': 'none', 'authData': auth, 'attStmt': {}}))}}, FakeHandler())


def test_registration_accepts_extension_map(monkeypatch, tmp_path):
    assert register(monkeypatch, tmp_path, 0xC1, cbor({'credProtect': 2}))['ok']


@pytest.mark.parametrize('flags,suffix', [
    (0x41, cbor({'credProtect': 2})),  # extension without ED flag
    (0xC1, b''),                    # ED flag without extension
    (0xC1, cbor(1)),                # extension is not a map
    (0xC1, cbor({}) + b'\x00'),     # trailing data after extension
    (0xC1, b'\xa1'),               # truncated extension
    (0xC1, b'\xa1\x80\x00'),       # unhashable map key
    (0xC1, b'\xa1\x61\xff\x00'),   # invalid UTF-8 map key
    (0xC1, b'\xa1\x61x' * 2000 + b'\x00'),  # excessive nesting
])
def test_registration_rejects_invalid_extensions(monkeypatch, tmp_path, flags, suffix):
    from api.passkeys import PasskeyError
    with pytest.raises(PasskeyError):
        register(monkeypatch, tmp_path, flags, suffix)


def register_raw(monkeypatch, tmp_path, cid, cose_bytes, declared_len=None, flags=0x41):
    """Like register(), but with full control of the credential ID, its declared
    length and the COSE key bytes (release gate: exercise the new credential guards)."""
    passkeys = _set_paths(monkeypatch, tmp_path)
    opts = passkeys.registration_options(FakeHandler())
    _, client = _client_data('webauthn.create', opts['challenge'])
    n = len(cid) if declared_len is None else declared_len
    auth = (hashlib.sha256(opts['rp']['id'].encode()).digest() + bytes([flags])
            + (1).to_bytes(4, 'big') + bytes(16) + n.to_bytes(2, 'big')
            + cid + cose_bytes)
    return passkeys.finish_registration({'response': {'clientDataJSON': client,
        'attestationObject': b64u(cbor({'fmt': 'none', 'authData': auth, 'attStmt': {}}))}}, FakeHandler())


def _valid_cose():
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    return cbor({1: 2, 3: -7, -1: 1, -2: key.x.to_bytes(32, 'big'), -3: key.y.to_bytes(32, 'big')})


@pytest.mark.parametrize('case', ['empty_credential_id', 'declared_length_past_end', 'cose_key_not_a_map'])
def test_registration_rejects_malformed_credential_data(monkeypatch, tmp_path, case):
    """Greptile on release PR #8102: the new `cred_len` and non-map COSE-key guards
    had no tests. Each malformed input must be a clean PasskeyError (a 4xx), never a
    crash or a stored credential."""
    from api.passkeys import PasskeyError
    if case == 'empty_credential_id':
        args = dict(cid=b'', cose_bytes=_valid_cose())
    elif case == 'declared_length_past_end':
        args = dict(cid=b'short-id', cose_bytes=b'', declared_len=500)
    else:
        args = dict(cid=b'extension-credential', cose_bytes=cbor([1, 2, 3]))
    with pytest.raises(PasskeyError):
        register_raw(monkeypatch, tmp_path, **args)


def test_register_raw_accepts_a_valid_credential(monkeypatch, tmp_path):
    """Control for the helper above: well-formed data still registers."""
    assert register_raw(monkeypatch, tmp_path, b'extension-credential', _valid_cose())['ok']
