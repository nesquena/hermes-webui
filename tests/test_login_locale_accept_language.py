"""Follow-up to #7622: the server-rendered /login page must honour Accept-Language.

#7622 was fixed client-side by #7730 — `settings.language` is now `None` until
the user explicitly picks a locale, and `static/i18n.js` resolves the UI locale
from `navigator.languages`. But `/login` is rendered by the server *before* any
of that JavaScript runs, and it still did:

    _lang = _settings.get("language", "en")

So on a fresh install — `language` unset — every visitor, regardless of what
their browser asked for, was greeted in English. That page is quite often the
very first thing a new user sees.

These tests pin the fix:

1. `_login_locale_from_accept_language()` walks the header in `q=` order and
   never launders an unknown tag into "en".
2. `/login` uses it while `settings.language` is unset, and still defers to an
   explicit stored choice.

The tests arrange the unset state by writing ``settings.json`` directly rather
than through the settings API: an empty-string "reset" is deliberately out of
scope for this PR (it needs shared client-resolver handling to be correct), so
the server-side settings endpoint ignores ``{"language": ""}`` and cannot be
used to clear a prior choice.
"""

import json
import pathlib
import sys
import urllib.request

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from tests._pytest_port import BASE  # noqa: E402  (port comes from conftest)


# ── 1. Accept-Language parsing ──────────────────────────────────────────────


def test_accept_language_picks_the_highest_quality_tag():
    from api.routes import _login_locale_from_accept_language

    assert _login_locale_from_accept_language("fr-CA,fr;q=0.9,en;q=0.8") == "fr"
    assert _login_locale_from_accept_language("ja;q=0.4, de;q=0.9") == "de"


def test_accept_language_falls_through_to_a_lower_quality_tag():
    from api.routes import _login_locale_from_accept_language

    # The first entry is unknown, so the parser must keep walking.
    assert _login_locale_from_accept_language("xx-YY, ja, en;q=0.1") == "ja"


def test_accept_language_handles_aliases_and_quoted_tags():
    from api.routes import _login_locale_from_accept_language

    assert _login_locale_from_accept_language("zh-TW") == "zh-Hant"
    assert _login_locale_from_accept_language("zh-Hans-CN") == "zh"
    assert _login_locale_from_accept_language('"pt-BR"') == "pt"
    assert _login_locale_from_accept_language("en-GB") == "en"
    assert _login_locale_from_accept_language("fr_CA") == "fr"


def test_accept_language_returns_none_for_absent_or_unknown():
    from api.routes import _login_locale_from_accept_language

    assert _login_locale_from_accept_language(None) is None
    assert _login_locale_from_accept_language("") is None
    assert _login_locale_from_accept_language("*") is None
    assert _login_locale_from_accept_language("xx-YY, zz") is None
    # q=0 means "not acceptable" — it must not win the sort.
    assert _login_locale_from_accept_language("fr;q=0, en") == "en"


# ── 2. End-to-end against the live test server ──────────────────────────────


def _get_settings():
    with urllib.request.urlopen(BASE + "/api/settings", timeout=10) as r:
        return json.loads(r.read()), r.status


def _post_settings(body):
    req = urllib.request.Request(
        BASE + "/api/settings",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()), r.status


def _set_language_on_disk(value):
    """Arrange the stored ``language`` directly in ``settings.json``.

    ``load_settings()`` re-reads the file on every request (no in-memory
    cache), so a direct write is picked up by the next ``/login`` GET. This is
    how the tests establish (and restore) the unset / prior-choice state — the
    settings API intentionally does not accept an empty-string reset.
    """
    from api.config import SETTINGS_FILE

    data = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if value is None:
        data.pop("language", None)
    else:
        data["language"] = value
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _get_login(accept_language=None):
    headers = {}
    if accept_language is not None:
        headers["Accept-Language"] = accept_language
    req = urllib.request.Request(BASE + "/login", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode(), r.status


def test_fresh_install_login_page_follows_the_browser_language():
    settings, status = _get_settings()
    assert status == 200
    prev = settings.get("language")
    try:
        # Fresh-install state: nothing chosen yet.
        _set_language_on_disk(None)

        html, status = _get_login("fr-CA,fr;q=0.9,en;q=0.8")
        assert status == 200
        assert 'lang="fr-FR"' in html
        assert "Se connecter" in html
    finally:
        _set_language_on_disk(prev)


def test_unsupported_browser_language_still_falls_back_to_english():
    settings, status = _get_settings()
    assert status == 200
    prev = settings.get("language")
    try:
        _set_language_on_disk(None)
        html, status = _get_login("xx-YY")
        assert status == 200
        assert 'lang="en"' in html
    finally:
        _set_language_on_disk(prev)


def test_login_without_accept_language_header_is_english():
    settings, status = _get_settings()
    assert status == 200
    prev = settings.get("language")
    try:
        _set_language_on_disk(None)
        html, status = _get_login(None)
        assert status == 200
        assert 'lang="en"' in html
    finally:
        _set_language_on_disk(prev)


def test_explicit_language_setting_still_overrides_accept_language():
    settings, status = _get_settings()
    assert status == 200
    prev = settings.get("language")
    try:
        _post_settings({"language": "ja"})
        html, status = _get_login("fr-CA")
        assert status == 200
        assert 'lang="ja-JP"' in html
    finally:
        _set_language_on_disk(prev)
