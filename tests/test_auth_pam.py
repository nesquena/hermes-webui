from types import SimpleNamespace

from api import auth_pam


def _pwd(users):
    def getpwnam(name):
        if name not in users:
            raise KeyError(name)
        return users[name]

    return SimpleNamespace(getpwnam=getpwnam)


def _account(name, uid=1000, shell="/bin/bash"):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_shell=shell)


def test_pam_username_ui_only_for_multi_user_pam(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_AUTH_MODE", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_PAM_ALLOW_ANY_USER", raising=False)
    assert auth_pam.login_uses_username() is False

    monkeypatch.setenv("HERMES_WEBUI_AUTH_MODE", "pam")
    assert auth_pam.login_uses_username() is False

    monkeypatch.setenv("HERMES_WEBUI_PAM_ALLOW_ANY_USER", "1")
    assert auth_pam.login_uses_username() is True


def test_profile_name_for_user_is_safe_and_deterministic():
    assert auth_pam.profile_name_for_user("dbyte") == "dbyte"

    dotted = auth_pam.profile_name_for_user("Jane.Doe")
    assert dotted.startswith("jane-doe-")
    assert dotted == auth_pam.profile_name_for_user("Jane.Doe")
    assert "." not in dotted

    default = auth_pam.profile_name_for_user("default")
    assert default != "default"
    assert default.startswith("default-")


def test_resolve_login_user_uses_fixed_user_when_multi_user_disabled(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PAM_USER", "alice")
    monkeypatch.delenv("HERMES_WEBUI_PAM_ALLOW_ANY_USER", raising=False)
    monkeypatch.setattr(auth_pam, "pwd", _pwd({
        "alice": _account("alice"),
        "bob": _account("bob"),
    }))

    assert auth_pam.resolve_login_user(None, allow_default=True) == "alice"
    assert auth_pam.resolve_login_user("alice") == "alice"
    assert auth_pam.resolve_login_user("bob") is None


def test_resolve_login_user_filters_service_accounts(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PAM_ALLOW_ANY_USER", "1")
    monkeypatch.setenv("HERMES_WEBUI_PAM_MIN_UID", "1000")
    monkeypatch.setattr(auth_pam, "pwd", _pwd({
        "alice": _account("alice", uid=1000),
        "daemon": _account("daemon", uid=1),
        "disabled": _account("disabled", uid=1001, shell="/usr/sbin/nologin"),
    }))

    assert auth_pam.resolve_login_user("alice") == "alice"
    assert auth_pam.resolve_login_user("daemon") is None
    assert auth_pam.resolve_login_user("disabled") is None


def test_authenticate_does_not_assume_helper_without_configuration(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PAM_ALLOW_ANY_USER", "1")
    monkeypatch.delenv("HERMES_WEBUI_PAM_HELPER", raising=False)
    monkeypatch.setattr(auth_pam, "pwd", _pwd({"alice": _account("alice")}))
    monkeypatch.setattr(auth_pam, "_authenticate_with_python_pam", lambda _u, _p: False)

    def fail_run(*_args, **_kwargs):
        raise AssertionError("subprocess helper should not run without HERMES_WEBUI_PAM_HELPER")

    monkeypatch.setattr(auth_pam.subprocess, "run", fail_run)
    assert auth_pam.authenticate("alice", "pw") is None


def test_authenticate_uses_explicit_helper_as_fallback(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("HERMES_WEBUI_PAM_ALLOW_ANY_USER", "1")
    monkeypatch.setenv("HERMES_WEBUI_PAM_HELPER", "/opt/hermes/pam-helper")
    monkeypatch.setattr(auth_pam, "pwd", _pwd({"alice": _account("alice")}))
    monkeypatch.setattr(auth_pam, "_authenticate_with_python_pam", lambda _u, _p: False)
    monkeypatch.setattr(auth_pam.subprocess, "run", fake_run)

    identity = auth_pam.authenticate("alice", "pw")

    assert identity == {"user": "alice", "profile": "alice"}
    assert captured["cmd"] == ["/opt/hermes/pam-helper", "--user", "alice", "--service", "login"]
    assert captured["input"] == "pw"
