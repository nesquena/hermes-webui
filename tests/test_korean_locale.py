from collections import Counter
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_locale_block(src: str, locale_key: str) -> str:
    start_match = re.search(rf"\b{re.escape(locale_key)}\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"

    start = start_match.end() - 1
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    escape = False

    for i in range(start, len(src)):
        ch = src[i]

        if escape:
            escape = False
            continue

        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue

        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue

        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue

        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "`":
            in_backtick = True
            continue

        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]

    raise AssertionError(f"{locale_key} locale block braces are not balanced")


def locale_keys(src: str, locale_key: str) -> list[str]:
    # Locale objects are flat. Match every key regardless of indentation so
    # accidental 2-space lines cannot hide from duplicate-key checks.
    key_pattern = re.compile(r"^\s*([a-zA-Z0-9_]+)\s*:", re.MULTILINE)
    return key_pattern.findall(extract_locale_block(src, locale_key))


def test_korean_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  ko: {" in src
    assert "_lang: 'ko'" in src
    assert "_label: '한국어'" in src
    assert "_speech: 'ko-KR'" in src


def test_korean_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_title: '설정'",
        "settings_label_language: '언어'",
        "login_title: '로그인'",
        "approval_heading: '승인 필요'",
        "tab_chat: '채팅'",
        "tab_tasks: '작업'",
        "tab_profiles: 'Agent 프로필'",
        "empty_title: '무엇을 도와드릴까요?'",
        "onboarding_title: 'Hermes Web UI에 오신 것을 환영합니다'",
    ]
    for entry in expected:
        assert entry in src


def test_korean_settings_detail_descriptions_are_translated():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_desc_workspace_panel_open: '활성화하면 새 세션마다 워크스페이스/파일 브라우저 패널이 자동으로 열립니다. 언제든지 수동으로 닫을 수 있습니다.'",
        "settings_desc_notifications: '앱이 백그라운드에 있을 때 응답이 완료되면 시스템 알림을 표시합니다.'",
        "settings_desc_token_usage: '각 Assistant 응답 아래에 입력/출력 토큰 수를 표시합니다. /usage로도 전환할 수 있습니다.'",
        "settings_desc_sidebar_density: '왼쪽 사이드바의 세션 목록에 표시할 메타데이터 양을 제어합니다.'",
        "settings_desc_auto_title_refresh: '최신 대화를 바탕으로 세션 제목을 자동으로 다시 생성해 대화가 진행되어도 제목을 관련 있게 유지합니다. LLM 제목 생성 모델 설정이 필요합니다.'",
        "settings_desc_cli_sessions: 'Hermes CLI(state.db)의 세션을 세션 목록에 병합합니다. CLI 세션을 클릭하면 가져와서 대화를 계속할 수 있습니다.'",
        "settings_desc_sync_insights: 'WebUI 토큰 사용량을 state.db에 반영하여 hermes /insights에 브라우저 세션 데이터가 포함되도록 합니다. 기본값은 꺼짐입니다.'",
        "settings_desc_check_updates: 'WebUI 또는 Agent의 새 버전이 있으면 배너를 표시합니다. 백그라운드에서 주기적으로 git fetch를 실행합니다.'",
        "settings_desc_bot_name: 'UI 전체에 표시되는 Assistant 이름입니다. 기본값은 Hermes입니다.'",
        "settings_desc_password: '새 비밀번호를 설정하거나 변경하려면 입력하세요. 현재 설정을 유지하려면 비워 두세요.'",
    ]
    for entry in expected:
        assert entry in src


def test_korean_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    ko_keys = set(locale_keys(src, "ko"))
    assert sorted(en_keys - ko_keys) == []
    assert sorted(ko_keys - en_keys) == []


def test_korean_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    keys = locale_keys(src, "ko")
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Korean locale has duplicate keys: {duplicates}"


def test_korean_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    ko_block = extract_locale_block(src, "ko")
    badly_indented = [
        line.strip()
        for line in ko_block.splitlines()
        if re.match(r"^\s{1,3}[a-zA-Z0-9_]+\s*:", line)
    ]
    assert badly_indented == []
