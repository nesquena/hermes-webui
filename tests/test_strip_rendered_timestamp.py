"""Unit tests for _strip_rendered_timestamp in tests/browser_conversation_lifecycle.py (#7792)."""
from tests.browser_conversation_lifecycle import _strip_rendered_timestamp


def test_strip_rendered_timestamp_12hr():
    assert _strip_rendered_timestamp("Alpha\n  12:34 PM") == "Alpha"
    assert _strip_rendered_timestamp("Alpha\n1:23 am") == "Alpha"
    assert _strip_rendered_timestamp("Alpha\n\t9:45 PM\t") == "Alpha"


def test_strip_rendered_timestamp_24hr():
    assert _strip_rendered_timestamp("Line 1\nLine 2\n14:05") == "Line 1\nLine 2"
    assert _strip_rendered_timestamp("Process output\n23:59:59") == "Process output"


def test_strip_rendered_timestamp_preserves_blank_lines():
    text = "alpha\n\nbeta\n10:00 AM"
    assert _strip_rendered_timestamp(text) == "alpha\n\nbeta"


def test_strip_rendered_timestamp_preserves_indentation():
    text = "  leading space\ntrailing space  \n\n11:22"
    assert _strip_rendered_timestamp(text) == "  leading space\ntrailing space  \n"


def test_strip_rendered_timestamp_no_timestamp():
    text = "Just normal text\nwith multiple lines"
    assert _strip_rendered_timestamp(text) == text
    assert _strip_rendered_timestamp("") == ""
    assert _strip_rendered_timestamp("Single line without newline") == "Single line without newline"
