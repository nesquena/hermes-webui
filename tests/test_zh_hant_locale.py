from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def locale_block(src: str, marker: str) -> str:
    start = src.index(marker)
    open_brace = src.index("{", start)
    pos = open_brace + 1
    depth = 1
    in_single = False
    in_double = False
    in_template = False
    escaped = False
    line_comment = False
    block_comment = False

    while pos < len(src):
        char = src[pos]
        next_char = src[pos + 1] if pos + 1 < len(src) else ""

        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                pos += 1
        elif escaped:
            escaped = False
        elif in_single:
            if char == "\\":
                escaped = True
            elif char == "'":
                in_single = False
        elif in_double:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
        elif in_template:
            if char == "\\":
                escaped = True
            elif char == "`":
                in_template = False
        elif char == "/" and next_char == "/":
            line_comment = True
            pos += 1
        elif char == "/" and next_char == "*":
            block_comment = True
            pos += 1
        elif char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == "`":
            in_template = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace + 1 : pos]

        pos += 1

    raise AssertionError(f"Locale block not closed: {marker}")


def entries(block: str) -> list[tuple[str, str]]:
    return re.findall(
        r"^\s+([a-zA-Z0-9_]+):\s*(.*?)(?:,\s*)?$",
        block,
        re.MULTILINE,
    )


def keys(block: str) -> list[str]:
    return [key for key, _ in entries(block)]


def value_map(block: str) -> dict[str, str]:
    return dict(entries(block))


def arrow_arg_names(value: str) -> list[str] | None:
    match = re.match(r"\((?P<args>[^)]*)\)\s*=>", value)
    if not match:
        match = re.match(r"(?P<arg>[a-zA-Z_$][\w$]*)\s*=>", value)
        if not match:
            return None
        return [match.group("arg")]

    args = match.group("args").strip()
    if not args:
        return []
    return [arg.strip() for arg in args.split(",") if arg.strip()]


def arrow_arg_count(value: str) -> int | None:
    args = arrow_arg_names(value)
    if args is None:
        return None
    return len(args)


def template_arg_refs(value: str, arg_names: list[str] | None) -> list[str]:
    arg_index = {arg: index for index, arg in enumerate(arg_names or [])}
    refs = []
    for var_name in re.findall(r"\$\{([a-zA-Z_]\w*)\}", value):
        if var_name in arg_index:
            refs.append(f"arg:{arg_index[var_name]}")
        else:
            refs.append(f"name:{var_name}")
    return sorted(set(refs))



def test_english_locale_remains_english_source_text():
    src = read(REPO / "static" / "i18n.js")
    en_block = locale_block(src, "\n  en: {")
    cjk_lines = [
        line.strip()
        for line in en_block.splitlines()
        if re.search(r"[\u4e00-\u9fff]", line)
    ]
    assert not cjk_lines, cjk_lines

def test_zh_hant_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  'zh-Hant': {" in src
    assert "_label: '繁體中文'" in src
    assert "_speech: 'zh-TW'" in src


def test_zh_hant_locale_covers_english_keys_without_duplicates():
    src = read(REPO / "static" / "i18n.js")
    en_block = locale_block(src, "\n  en: {")
    zh_block = locale_block(src, "\n  'zh-Hant': {")
    en_keys = set(keys(en_block))
    zh_keys = keys(zh_block)

    duplicates = sorted({key for key in zh_keys if zh_keys.count(key) > 1})
    assert not duplicates, f"zh-Hant locale duplicate keys: {duplicates}"

    missing = sorted(en_keys - set(zh_keys))
    extra = sorted(set(zh_keys) - en_keys)
    assert not missing, f"zh-Hant locale missing keys: {missing}"
    assert not extra, f"zh-Hant locale extra keys: {extra}"



def test_zh_hant_locale_preserves_function_and_placeholder_shapes():
    src = read(REPO / "static" / "i18n.js")
    en_values = value_map(locale_block(src, "\n  en: {"))
    zh_values = value_map(locale_block(src, "\n  'zh-Hant': {"))

    function_mismatches = []
    placeholder_mismatches = []
    template_var_mismatches = []
    for key, en_value in en_values.items():
        assert key in zh_values, f"Missing zh-Hant translation: {key}"
        zh_value = zh_values[key]
        en_arg_names = arrow_arg_names(en_value)
        zh_arg_names = arrow_arg_names(zh_value)
        en_args = None if en_arg_names is None else len(en_arg_names)
        zh_args = None if zh_arg_names is None else len(zh_arg_names)
        if en_args != zh_args:
            function_mismatches.append((key, en_args, zh_args))

        placeholder_pattern = r"(?<!\$)\{(?:\d+|[a-zA-Z_]\w*)\}"
        en_placeholders = sorted(set(re.findall(placeholder_pattern, en_value)))
        zh_placeholders = sorted(set(re.findall(placeholder_pattern, zh_value)))
        if en_placeholders != zh_placeholders:
            placeholder_mismatches.append((key, en_placeholders, zh_placeholders))

        en_template_refs = template_arg_refs(en_value, en_arg_names)
        zh_template_refs = template_arg_refs(zh_value, zh_arg_names)
        if en_template_refs != zh_template_refs:
            template_var_mismatches.append((key, en_template_refs, zh_template_refs))

    assert not function_mismatches, function_mismatches
    assert not placeholder_mismatches, placeholder_mismatches
    assert not template_var_mismatches, template_var_mismatches

def test_zh_hant_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "approval_heading: '需要核准'",
        "settings_label_language: '語言'",
        "login_title: '登入'",
        "tab_todos: '待辦'",
        "onboarding_title: '歡迎使用 Hermes Web UI'",
        "onboarding_complete: '初始設定已完成'",
    ]
    for entry in expected:
        assert entry in src


def test_zh_hant_locale_has_no_known_untranslated_strings():
    src = read(REPO / "static" / "i18n.js")
    block = locale_block(src, "\n  'zh-Hant': {")
    untranslated = [
        "Summarize What's New with AI",
        "Changes the What's New action",
        "TODO: translate",
    ]
    hits = sorted(text for text in untranslated if text in block)
    assert not hits, f"zh-Hant locale has untranslated strings: {hits}"
