import re
from pathlib import Path


def test_messages_rule_has_overflow_x_hidden():
    """Verify .messages rule includes overflow-x:hidden property."""
    css_file = Path(__file__).resolve().parent.parent / "static" / "style.css"
    content = css_file.read_text()

    # Find the .messages{ rule
    messages_rule_match = re.search(r'\.messages\{[^}]*overflow-y:auto[^}]*\}', content)
    assert messages_rule_match, ".messages rule not found"

    messages_rule = messages_rule_match.group(0)
    assert 'overflow-x:hidden' in messages_rule, (
        ".messages rule does not contain overflow-x:hidden"
    )


def test_messages_inner_mobile_has_containment():
    """Verify .messages-inner in mobile breakpoint includes containment properties."""
    css_file = Path(__file__).resolve().parent.parent / "static" / "style.css"
    content = css_file.read_text()

    # Find the @media(max-width:640px) block and then .messages-inner within a reasonable window
    media_match = re.search(r'@media\(max-width:640px\)\{', content)
    assert media_match, "@media(max-width:640px) block not found"

    # Extract content after the media query opening brace
    media_start = media_match.start()
    remaining_content = content[media_start:media_start + 5000]  # Look ahead 5000 chars

    # Find .messages-inner rule within this section
    messages_inner_match = re.search(r'\.messages-inner\{([^}]*)\}', remaining_content)
    assert messages_inner_match, ".messages-inner rule not found in @media(max-width:640px) block"

    messages_inner_rule = messages_inner_match.group(0)

    # Verify all required properties are present
    assert 'max-width:100%' in messages_inner_rule, (
        ".messages-inner in mobile block missing max-width:100%"
    )
    assert 'overflow-x:hidden' in messages_inner_rule, (
        ".messages-inner in mobile block missing overflow-x:hidden"
    )
    assert 'word-break:break-word' in messages_inner_rule, (
        ".messages-inner in mobile block missing word-break:break-word"
    )
    assert 'min-width:0' in messages_inner_rule, (
        ".messages-inner in mobile block missing min-width:0"
    )
