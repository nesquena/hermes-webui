from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    needle = f"function {name}"
    start = source.find(needle)
    if start < 0:
        needle = f"async function {name}"
        start = source.find(needle)
    assert start >= 0, f"missing function {name}"
    brace = source.find("{", start)
    assert brace >= 0
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace : idx + 1]
    raise AssertionError(f"unterminated function {name}")


def test_pet_slash_command_is_registered_with_wakeup_sleep_subcommands():
    assert "{name:'pet'" in COMMANDS_JS
    assert "desc:t('cmd_pet')" in COMMANDS_JS
    assert "fn:cmdPet" in COMMANDS_JS
    assert "arg:'wakeup|sleep'" in COMMANDS_JS
    assert "subArgs:['wakeup','sleep']" in COMMANDS_JS
    assert "noEcho:true" in COMMANDS_JS


def test_pet_slash_command_reuses_settings_toggle_path():
    body = _function_body(COMMANDS_JS, "cmdPet")
    assert "toggleDesktopPetFromAppearance(true,{notifySetup:true})" in body
    assert "toggleDesktopPetFromAppearance(false)" in body
    assert "startDesktopPet({notifySetup:true})" in body
    assert "closeDesktopPet()" in body
    assert "if(!ok) return;" in body
    assert "cmd_pet_usage" in body
    assert "_petCommandAvailableOnThisDevice()" in body
    assert "cmd_pet_unavailable" in body
    assert "cmd_pet_wakeup_done" in body
    assert "cmd_pet_sleep_done" in body
    assert "api('/api/pet/launch'" not in body
    assert "api('/api/pet/close'" not in body


def test_pet_slash_command_matches_existing_settings_switch_semantics():
    assert 'id="settingsDesktopPetField"' in INDEX_HTML
    assert 'id="settingsDesktopPetEnabled"' in INDEX_HTML
    assert 'role="switch"' in INDEX_HTML
    assert 'onchange="toggleDesktopPetFromAppearance(this.checked)"' in INDEX_HTML


def test_pet_slash_command_i18n_keys_exist_in_all_locales():
    for key in (
        "cmd_pet",
        "cmd_pet_usage",
        "cmd_pet_wakeup_done",
        "cmd_pet_sleep_done",
        "cmd_pet_unavailable",
    ):
        assert I18N_JS.count(f"{key}:") >= 6, f"missing {key} in one or more locales"
