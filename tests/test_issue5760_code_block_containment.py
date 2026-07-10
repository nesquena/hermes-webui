from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _base_msg_body_pre_rule() -> str:
    match = re.search(r"(^|\n)\s*\.msg-body pre\{([^}]*)\}", STYLE_CSS)
    assert match, "base .msg-body pre rule not found in style.css"
    return match.group(2)


def test_msg_body_pre_containment_contract():
    rule = _base_msg_body_pre_rule()
    assert "overflow-x:auto" in rule, ".msg-body pre must keep horizontal scrolling"
    assert "contain:content" in rule, ".msg-body pre must add contain: content"


def test_msg_body_pre_avoids_strict_containment():
    rule = _base_msg_body_pre_rule()
    assert "contain:strict" not in rule, ".msg-body pre must not use contain: strict"
