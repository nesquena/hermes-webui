from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _normalize_selector(selector: str) -> str:
    return " ".join(selector.split())


def _selector_parts(prelude: str) -> set[str]:
    return {_normalize_selector(part) for part in prelude.split(",") if part.strip()}


def _css_rule_blocks() -> list[tuple[set[str], str]]:
    blocks = []
    stack: list[int] = []
    segment_start = 0
    for idx, char in enumerate(STYLE_CSS):
        if char == "{":
            stack.append(idx)
            if len(stack) == 1:
                prelude = STYLE_CSS[segment_start:idx].strip()
                if prelude.startswith("@"):
                    continue
                depth = 1
                for end in range(idx + 1, len(STYLE_CSS)):
                    if STYLE_CSS[end] == "{":
                        depth += 1
                    elif STYLE_CSS[end] == "}":
                        depth -= 1
                        if depth == 0:
                            blocks.append((_selector_parts(prelude), STYLE_CSS[idx + 1 : end]))
                            break
        elif char == "}":
            if stack:
                stack.pop()
            if not stack:
                segment_start = idx + 1
    return blocks


def _css_blocks(selector: str) -> list[str]:
    normalized = _normalize_selector(selector)
    blocks = [body for selectors, body in _css_rule_blocks() if normalized in selectors]
    assert blocks, f"Missing CSS selector: {selector}"
    return blocks


def _assert_any_block_contains(selector: str, *properties: str) -> None:
    blocks = _css_blocks(selector)
    for block in blocks:
        if all(prop in block for prop in properties):
            return
    raise AssertionError(f"No CSS block for {selector} contains {properties}")


def test_usage_footer_text_can_shrink_instead_of_wrapping_timestamp():
    for selector in (
        ".msg-usage-inline",
        ".msg-duration-inline",
        ".msg-gateway-inline",
        ".gateway-failover-inline",
        ".msg-model-warning-inline",
    ):
        _assert_any_block_contains(
            selector,
            "flex: 0 1 auto",
            "min-width: 0",
            "overflow: hidden",
            "text-overflow: ellipsis",
            "white-space: nowrap",
        )


def test_message_footer_timestamp_and_actions_do_not_wrap_or_shrink():
    _assert_any_block_contains(
        ".msg-foot .msg-time",
        "white-space: nowrap",
        "flex: 0 0 auto",
    )
    _assert_any_block_contains(
        ".msg-foot .msg-actions",
        "white-space: nowrap",
        "flex: 0 0 auto",
    )
    _assert_any_block_contains(".msg-foot .msg-action-btn", "flex: 0 0 auto")


def test_usage_footer_hover_rules_do_not_override_nowrap_or_flex_contract():
    for selector in (
        ".msg-foot-with-usage .msg-time",
        ".msg-foot-with-usage .msg-actions",
    ):
        for block in _css_blocks(selector):
            assert "white-space:" not in block
            assert "flex:" not in block
