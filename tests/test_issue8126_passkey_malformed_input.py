"""#8126 — a malformed passkey response is the client's error, never a 500.

The report: an attestation whose authenticator data declares a credential ID
longer than the one it carries makes the COSE-key decode start inside the key,
and the CBOR reader's text branch raises ``UnicodeDecodeError``. The routes turn
only ``PasskeyError`` into a 400/401, so that became a 500.

The same class had more members, on both ceremonies: any byte of the attestation
object changed into invalid UTF-8, a map key that cannot be hashed, nesting deep
enough for ``RecursionError``, a COSE key whose point is not on the curve
(``ValueError`` from cryptography), a field that is not base64url or not a
string, client data or a ``response`` that is not a JSON object, and a challenge
issued under a host name IDNA cannot encode. On the login route the 500 also
skipped the failed-attempt counter.

Everything here drives ``finish_registration`` / ``finish_login`` or the two
routes with handler stand-ins; no server, no browser, no authenticator. The key
pair is derived from a fixed number so every case is the same on every run.
"""
from __future__ import annotations

import hashlib
import io
import json
import random
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from tests.test_passkey_auth import FakeHandler, FakeHeaders, RouteFakeHandler, _client_data, _set_paths, b64u, cbor

_KEY = ec.derive_private_key(81260001, ec.SECP256R1())
_PUB = _KEY.public_key().public_numbers()
_COSE = {1: 2, 3: -7, -1: 1, -2: _PUB.x.to_bytes(32, "big"), -3: _PUB.y.to_bytes(32, "big")}
_COSE_BYTES = cbor(_COSE)
_CID = b"short-id"


def _auth_data(rp_id: str, cid: bytes, tail: bytes, *, declared: int | None = None, flags: int = 0x41) -> bytes:
    length = len(cid) if declared is None else declared
    return (
        hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + (1).to_bytes(4, "big")
        + bytes(16) + length.to_bytes(2, "big") + cid + tail
    )


def _attestation(auth: bytes) -> bytes:
    return cbor({"fmt": "none", "authData": auth, "attStmt": {}})


def _registration(passkeys, *, attestation=None, auth=None, handler=None):
    """A fresh challenge and a registration payload for it. ``attestation`` is
    the attestationObject field as sent (bytes are base64url-encoded here);
    ``auth`` builds the authenticator data from the RP ID instead."""
    handler = handler or FakeHandler()
    opts = passkeys.registration_options(handler)
    _, client = _client_data("webauthn.create", opts["challenge"], origin=passkeys.rp_context(handler)[1])
    if attestation is None:
        attestation = _attestation(auth(opts["rp"]["id"]))
    if isinstance(attestation, bytes):
        attestation = b64u(attestation)
    return {"response": {"clientDataJSON": client, "attestationObject": attestation}}


def _valid_auth(rp_id: str) -> bytes:
    return _auth_data(rp_id, _CID, _COSE_BYTES)


def _login(passkeys):
    """Register the fixed key, then a login payload with a valid signature."""
    passkeys.finish_registration(_registration(passkeys, auth=_valid_auth), FakeHandler())
    opts = passkeys.authentication_options(FakeHandler())
    raw, client = _client_data("webauthn.get", opts["challenge"])
    auth = hashlib.sha256(opts["rpId"].encode()).digest() + bytes([0x01]) + (2).to_bytes(4, "big")
    signature = _KEY.sign(auth + hashlib.sha256(raw).digest(), ec.ECDSA(hashes.SHA256()))
    return {
        "id": b64u(_CID),
        "response": {"clientDataJSON": client, "authenticatorData": b64u(auth), "signature": b64u(signature)},
    }


# ── The report: a declared credential length longer than the credential ──────


@pytest.mark.parametrize("extra", range(1, len(_COSE_BYTES) + 3))
def test_a_declared_length_past_the_credential_id_is_a_passkey_error(monkeypatch, tmp_path, extra):
    """The report's probe, at every offset into the key: ``bytes(16) + length +
    cid + cose + cbor({})`` with the ED flag. On master nine of these offsets
    land the decode on a text string that is not UTF-8."""
    passkeys = _set_paths(monkeypatch, tmp_path)
    payload = _registration(
        passkeys,
        auth=lambda rp: _auth_data(rp, _CID, _COSE_BYTES + cbor({}), declared=len(_CID) + extra, flags=0xC1),
    )

    with pytest.raises(passkeys.PasskeyError):
        passkeys.finish_registration(payload, FakeHandler())
    assert passkeys.registered_credentials() == []


def _post(monkeypatch, tmp_path, path: str, payload: dict):
    import api.auth as auth
    import api.routes as routes

    monkeypatch.setenv("HERMES_WEBUI_PASSKEY", "1")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    handler = RouteFakeHandler()
    body = json.dumps(payload).encode()
    handler.headers = FakeHeaders({"Host": "localhost:8787", "Content-Length": str(len(body))})
    handler.rfile = io.BytesIO(body)
    routes.handle_post(handler, SimpleNamespace(path=path))
    return handler, auth


def test_the_register_route_answers_400_and_stores_nothing(monkeypatch, tmp_path):
    """The same probe through ``handle_post``. Offset +13 is one of the nine: an
    exception there is not caught by the route and the server answers 500."""
    import api.auth as auth

    passkeys = _set_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)  # first passkey, from localhost
    payload = _registration(
        passkeys,
        auth=lambda rp: _auth_data(rp, _CID, _COSE_BYTES + cbor({}), declared=len(_CID) + 13, flags=0xC1),
    )

    handler, _ = _post(monkeypatch, tmp_path, "/api/auth/passkey/register", payload)

    assert handler.status == 400
    assert "error" in json.loads(handler.wfile.getvalue())
    assert passkeys.registered_credentials() == []


def test_the_register_route_still_stores_a_valid_credential(monkeypatch, tmp_path):
    """Control for the test above: the same route, a well-formed response."""
    import api.auth as auth

    passkeys = _set_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)

    handler, _ = _post(monkeypatch, tmp_path, "/api/auth/passkey/register", _registration(passkeys, auth=_valid_auth))

    assert handler.status == 200
    assert [cred["id"] for cred in passkeys.registered_credentials()] == [b64u(_CID)]


# ── The CBOR reader ──────────────────────────────────────────────────────────

_NESTED_X5C = cbor({"fmt": "packed", "attStmt": {"alg": -7, "sig": b"s", "x5c": [b"cert", b"chain"]}, "authData": b"a"})


class TestTheCborReader:
    @pytest.mark.parametrize(
        ("what", "raw"),
        [
            ("a text string that is not UTF-8", b"\x61\xff"),
            ("the same as a map key", b"\xa1\x61\xff\x00"),
            ("the same as a map value", b"\xa1\x00\x61\xff"),
            ("the same inside an array", b"\x81\x61\xff"),
            ("an array as a map key", b"\xa1\x80\x00"),
            ("a map as a map key", b"\xa1\xa0\x00"),
            ("arrays nested 5000 deep", b"\x81" * 5000 + b"\x00"),
            ("maps nested 3000 deep", b"\xa1\x00" * 3000 + b"\x00"),
            # Refusing a map as a key comes after reading it: the depth must
            # be counted on the way into a key too.
            ("maps nested 3000 deep through their keys", b"\xa1" * 3000 + b"\x00" * 3001),
        ],
    )
    def test_bytes_it_cannot_decode_are_a_passkey_error(self, what, raw):
        from api.passkeys import PasskeyError, _cbor_loads

        with pytest.raises(PasskeyError):
            _cbor_loads(raw)

    def test_nesting_is_accepted_up_to_the_limit_and_refused_one_past_it(self):
        from api import passkeys

        at_the_limit = b"\x81" * passkeys._CBOR_MAX_DEPTH + b"\x00"
        value = passkeys._cbor_loads(at_the_limit)
        for _ in range(passkeys._CBOR_MAX_DEPTH):
            assert isinstance(value, list) and len(value) == 1
            value = value[0]
        assert value == 0

        with pytest.raises(passkeys.PasskeyError):
            passkeys._cbor_loads(b"\x81" + at_the_limit)

    def test_the_limit_is_far_from_what_an_attestation_needs(self):
        """attStmt.x5c is the deepest thing a registration carries: three levels."""
        from api import passkeys

        value = passkeys._cbor_loads(_NESTED_X5C)

        assert value["attStmt"]["x5c"] == [b"cert", b"chain"]
        assert passkeys._CBOR_MAX_DEPTH >= 8

    def test_what_it_read_before_is_read_the_same(self):
        from api.passkeys import _cbor_loads

        value = {"text": "é", -1: [1, -2, b"\x00\xff", None, True, False], 2: {"k": "v"}, b"bytes key": 3}

        assert _cbor_loads(cbor(value)) == value


# ── Registration: every other way in ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("what", "tail"),
    [
        ("invalid UTF-8 text", b"\xa1\x61\xff\x00"),
        ("an unhashable map key", b"\xa1\x80\x00"),
        ("deep arrays", b"\x81" * 5000 + b"\x00"),
        ("a point that is not on the curve", cbor({**_COSE, -2: bytes(32), -3: bytes(32)})),
        ("x longer than the field", cbor({**_COSE, -2: b"\xff" * 64})),
        ("empty coordinates", cbor({**_COSE, -2: b"", -3: b""})),
        ("coordinates at the field size", cbor({**_COSE, -2: b"\xff" * 32, -3: b"\xff" * 32})),
    ],
)
def test_a_bad_cose_key_is_a_passkey_error(monkeypatch, tmp_path, what, tail):
    passkeys = _set_paths(monkeypatch, tmp_path)
    payload = _registration(passkeys, auth=lambda rp: _auth_data(rp, _CID, tail))

    with pytest.raises(passkeys.PasskeyError):
        passkeys.finish_registration(payload, FakeHandler())
    assert passkeys.registered_credentials() == []


@pytest.mark.parametrize(
    ("what", "attestation"),
    [
        ("invalid UTF-8 text", b"\xa1\x61\xff\x00"),
        ("an unhashable map key", b"\xa1\x80\x00"),
        ("deep arrays", b"\x81" * 5000 + b"\x00"),
        ("deep maps", b"\xa1\x00" * 3000 + b"\x00"),
        ("a number", 5),
        ("an object", {"a": 1}),
        ("a list", ["a"]),
        ("text that is not ASCII", "é"),
        ("text that is not base64", "a"),
    ],
)
def test_a_bad_attestation_object_is_a_passkey_error(monkeypatch, tmp_path, what, attestation):
    passkeys = _set_paths(monkeypatch, tmp_path)
    payload = _registration(passkeys, attestation=attestation)

    with pytest.raises(passkeys.PasskeyError):
        passkeys.finish_registration(payload, FakeHandler())
    assert passkeys.registered_credentials() == []


_BAD_CLIENT_DATA = [
    ("a number", 5),
    ("a list", ["a"]),
    ("text that is not ASCII", "é"),
    ("text that is not base64", "a"),
    ("JSON that is a list", b64u(b"[]")),
    ("JSON that is a number", b64u(b"5")),
    ("JSON that is a string", b64u(b'"x"')),
]


@pytest.mark.parametrize(("what", "client_data"), _BAD_CLIENT_DATA)
def test_bad_client_data_is_a_passkey_error_at_registration(monkeypatch, tmp_path, what, client_data):
    passkeys = _set_paths(monkeypatch, tmp_path)
    payload = _registration(passkeys, auth=_valid_auth)
    payload["response"]["clientDataJSON"] = client_data

    with pytest.raises(passkeys.PasskeyError):
        passkeys.finish_registration(payload, FakeHandler())
    assert passkeys.registered_credentials() == []


@pytest.mark.parametrize("response", ["x", 5, ["a"], True])
@pytest.mark.parametrize("ceremony", ["finish_registration", "finish_login"])
def test_a_response_that_is_not_an_object_is_a_passkey_error(monkeypatch, tmp_path, ceremony, response):
    passkeys = _set_paths(monkeypatch, tmp_path)
    if ceremony == "finish_login":
        payload = _login(passkeys)
    else:
        payload = _registration(passkeys, auth=_valid_auth)
    payload["response"] = response

    with pytest.raises(passkeys.PasskeyError):
        getattr(passkeys, ceremony)(payload, FakeHandler())


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "a" * 64 + ".example:8787"},
        {"Host": "a..example"},
        {"Origin": "http://" + "a" * 64 + ".example:8787", "Host": "localhost:8787"},
    ],
    ids=["a 64-character label in Host", "an empty label in Host", "a 64-character label in Origin"],
)
def test_a_host_name_idna_cannot_encode_is_a_passkey_error(monkeypatch, tmp_path, headers):
    """The RP ID is taken from Origin or Host when the challenge is issued and
    hashed when the response comes back."""
    passkeys = _set_paths(monkeypatch, tmp_path)
    handler = SimpleNamespace(headers=FakeHeaders(headers))
    payload = _registration(passkeys, auth=_valid_auth, handler=handler)

    with pytest.raises(passkeys.PasskeyError, match="RP ID mismatch"):
        passkeys.finish_registration(payload, handler)


# ── Login ────────────────────────────────────────────────────────────────────


def test_a_valid_login_is_still_accepted(monkeypatch, tmp_path):
    """Control for every login case below, which changes one field of this."""
    passkeys = _set_paths(monkeypatch, tmp_path)

    assert passkeys.finish_login(_login(passkeys), FakeHandler())["ok"] is True


_LOGIN_FIELDS = ["clientDataJSON", "authenticatorData", "signature"]


@pytest.mark.parametrize("field", _LOGIN_FIELDS)
@pytest.mark.parametrize(
    ("what", "value"),
    [("a number", 5), ("a list", ["a"]), ("text that is not ASCII", "é"), ("text that is not base64", "a")],
)
def test_a_login_field_that_is_not_base64url_is_a_passkey_error(monkeypatch, tmp_path, field, what, value):
    passkeys = _set_paths(monkeypatch, tmp_path)
    payload = _login(passkeys)
    payload["response"][field] = value

    with pytest.raises(passkeys.PasskeyError):
        passkeys.finish_login(payload, FakeHandler())


@pytest.mark.parametrize(("what", "client_data"), _BAD_CLIENT_DATA[4:])
def test_client_data_that_is_not_an_object_is_a_passkey_error_at_login(monkeypatch, tmp_path, what, client_data):
    passkeys = _set_paths(monkeypatch, tmp_path)
    payload = _login(passkeys)
    payload["response"]["clientDataJSON"] = client_data

    with pytest.raises(passkeys.PasskeyError):
        passkeys.finish_login(payload, FakeHandler())


def test_the_login_route_answers_401_and_counts_the_attempt(monkeypatch, tmp_path):
    """A 500 skipped ``_record_login_attempt``: malformed logins were not rate
    limited. Now they are refused like any other failed login."""
    import api.auth as auth

    passkeys = _set_paths(monkeypatch, tmp_path)
    payload = _login(passkeys)
    payload["response"]["signature"] = "a"
    counted = []
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_check_login_rate", lambda ip: True)
    monkeypatch.setattr(auth, "_record_login_attempt", counted.append)
    monkeypatch.setattr(
        auth, "create_session", lambda: (_ for _ in ()).throw(AssertionError("a session was created"))
    )

    handler, _ = _post(monkeypatch, tmp_path, "/api/auth/passkey/login", payload)

    assert handler.status == 401
    assert counted == ["127.0.0.1"]


# ── A sweep: nothing but PasskeyError leaves a damaged attestation object ────


def _outcome(monkeypatch, tmp_path, damage) -> str:
    passkeys = _set_paths(monkeypatch, tmp_path)
    for path in (passkeys._CREDENTIALS_FILE, passkeys._CHALLENGES_FILE):
        path.unlink(missing_ok=True)
    opts = passkeys.registration_options(FakeHandler())
    _, client = _client_data("webauthn.create", opts["challenge"])
    attestation = damage(_attestation(_valid_auth(opts["rp"]["id"])))
    try:
        passkeys.finish_registration(
            {"response": {"clientDataJSON": client, "attestationObject": b64u(attestation)}}, FakeHandler()
        )
    except passkeys.PasskeyError:
        return "refused"
    except Exception as exc:  # what this file is about
        return type(exc).__name__
    return "accepted"


def test_every_truncation_is_refused(monkeypatch, tmp_path):
    length = len(_attestation(_valid_auth("localhost")))

    outcomes = {cut: _outcome(monkeypatch, tmp_path, lambda good, cut=cut: good[:cut]) for cut in range(length)}

    assert set(outcomes.values()) == {"refused"}, {k: v for k, v in outcomes.items() if v != "refused"}


def test_no_single_changed_byte_gets_past_as_another_exception(monkeypatch, tmp_path):
    """600 seeded one-byte changes of a valid attestation object. A change in a
    field nothing checks (the AAGUID, the credential ID, ``fmt``) is accepted,
    as before; every other one must be refused, and none may raise anything
    else. On master about 46% of these raise ValueError, UnicodeDecodeError or
    TypeError."""
    length = len(_attestation(_valid_auth("localhost")))
    rng = random.Random(8126)
    changes = [(rng.randrange(length), rng.randrange(256)) for _ in range(600)]

    outcomes = [
        _outcome(monkeypatch, tmp_path, lambda good, pos=pos, value=value: good[:pos] + bytes([value]) + good[pos + 1:])
        for pos, value in changes
    ]

    assert set(outcomes) == {"refused", "accepted"}, sorted(set(outcomes) - {"refused", "accepted"})
    # Guard on the sweep: it must really reach both sides.
    assert outcomes.count("refused") > 300
    assert outcomes.count("accepted") > 50


def test_the_object_the_sweeps_damage_is_accepted_whole(monkeypatch, tmp_path):
    """Guard on the two sweeps: what they damage is a registration that works."""
    assert _outcome(monkeypatch, tmp_path, lambda good: good) == "accepted"
