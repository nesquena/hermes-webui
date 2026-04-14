import json
import urllib.error
import urllib.request


BASE = "http://127.0.0.1:8788"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read().decode(), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _current_language():
    settings, status = get("/api/settings")
    assert status == 200
    return settings.get("language") or "en"


def test_login_page_uses_simplified_chinese_for_zh_cn_alias():
    prev_lang = _current_language()
    try:
        saved, status = post("/api/settings", {"language": "zh-CN"})
        assert status == 200
        assert saved.get("language") == "zh-CN"
        html, status2 = get_raw("/login")
        assert status2 == 200
        assert 'lang="zh-CN"' in html
        assert "\u767b\u5f55" in html
        assert "\u8f93\u5165\u5bc6\u7801\u7ee7\u7eed\u4f7f\u7528" in html
    finally:
        restored, restore_status = post("/api/settings", {"language": prev_lang})
        assert restore_status == 200
        assert restored.get("language") == prev_lang


def test_login_page_uses_traditional_chinese_for_zh_hant():
    prev_lang = _current_language()
    try:
        saved, status = post("/api/settings", {"language": "zh-Hant"})
        assert status == 200
        assert saved.get("language") == "zh-Hant"
        html, status2 = get_raw("/login")
        assert status2 == 200
        assert 'lang="zh-TW"' in html
        assert "\u8f38\u5165\u5bc6\u78bc\u7e7c\u7e8c\u4f7f\u7528" in html
        assert "\u5bc6\u78bc\u932f\u8aa4" in html
    finally:
        restored, restore_status = post("/api/settings", {"language": prev_lang})
        assert restore_status == 200
        assert restored.get("language") == prev_lang
