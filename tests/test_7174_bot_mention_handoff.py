"""Bot Mode (#7174): leading @mention hands the composer message to another bot.

Hermes Desktop's hermes-bots plugin routes cross-bot messages via @mention
handoffs. The WebUI equivalent: a message that *opens* with ``@<profile>`` is
not sent to the current profile — the client switches to the mentioned
profile's session (the canonical handoff target) and delivers the remaining
text there. Mentions that don't open the message, or name no visible profile,
stay literal text.

`_parseBotMention` is extracted from static/messages.js and executed under
node (same style as tests/test_5306_subagent_sidebar_flicker.py).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_js_function(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    marker = re.compile(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(")
    m = marker.search(src)
    assert m, f"{name} not found in {path.name}"
    start = m.start()
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} braces unbalanced"
    return src[start:i]


def _eval_parse_mentions(cases):
    """Evaluate _parseBotMention from messages.js under node against cases."""
    func = _extract_js_function(MESSAGES_JS_PATH, "_parseBotMention")
    import json
    script = (
        func
        + "\nconst cases = "
        + json.dumps(cases)
        + ";\n"
        + "for (const c of cases) console.log(JSON.stringify(_parseBotMention(c)));\n"
    )
    out = _run_node(script)
    return [__import__("json").loads(line) for line in out.splitlines() if line]


class TestParseBotMention:
    def test_leading_mention_splits_target_and_text(self):
        (result,) = _eval_parse_mentions(["@sam please review the CRM copy"])
        assert result == {"target": "sam", "text": "please review the CRM copy"}

    def test_mention_with_no_body_keeps_target(self):
        (result,) = _eval_parse_mentions(["@leads"])
        assert result == {"target": "leads", "text": ""}

    def test_mention_not_at_start_is_not_a_handoff(self):
        (result,) = _eval_parse_mentions(["please ask @sam about this"])
        assert result is None

    def test_double_at_is_email_like_not_a_handoff(self):
        (result,) = _eval_parse_mentions(["@@sam hello"])
        assert result is None

    def test_at_alone_is_not_a_handoff(self):
        (result,) = _eval_parse_mentions(["@ hello"])
        assert result is None

    def test_email_address_is_not_a_handoff(self):
        (result,) = _eval_parse_mentions(["mail it@ablixar.com asap"])
        assert result is None

    def test_profile_names_with_hyphens_and_underscores(self):
        (result,) = _eval_parse_mentions(["@sam-assistant take it"])
        assert result == {"target": "sam-assistant", "text": "take it"}
        (result,) = _eval_parse_mentions(["@rest_infra ping"])
        assert result == {"target": "rest_infra", "text": "ping"}

    def test_empty_input_is_none(self):
        (result,) = _eval_parse_mentions([""])
        assert result is None

    def test_whitespace_only_is_none(self):
        (result,) = _eval_parse_mentions(["  "])
        assert result is None

    def test_leading_whitespace_still_parses(self):
        (result,) = _eval_parse_mentions(["  @sam go"])
        assert result == {"target": "sam", "text": "go"}


class TestSendRoutesMentionHandoff:
    """send() must consult _parseBotMention before dispatching, and route a
    resolved handoff through _routeBotMentionHandoff (switch + deliver)."""

    def test_send_parses_bot_mention_before_dispatch(self):
        src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
        start = src.find("async function send(")
        assert start >= 0
        end = src.find("\nasync function ", start + 1)
        body = src[start:end if end > 0 else start + 20000]
        assert "_parseBotMention(" in body, \
            "send() must detect leading @mentions (#7174)"
        assert "_routeBotMentionHandoff(" in body, \
            "send() must route resolved mentions through the handoff helper (#7174)"

    def test_handoff_helper_exists_and_switches_profile(self):
        src = MESSAGES_JS_PATH.read_text(encoding="utf-8")
        start = src.find("async function _routeBotMentionHandoff(")
        assert start >= 0, "_routeBotMentionHandoff missing (#7174)"
        end = src.find("\nasync function ", start + 1)
        body = src[start:end if end > 0 else start + 4000]
        assert "switchToProfile(" in body, \
            "handoff must switch to the mentioned profile (#7174)"
        assert "visible" in body, \
            "handoff must only target visible profiles (#7174)"


class TestComposerMentionAutocomplete:
    """Typing @ in the composer opens a bot roster popup; picking an entry
    inserts @name into the composer."""

    def test_autocomplete_helpers_exist(self):
        src = PANELS_JS_PATH.read_text(encoding="utf-8")
        for name in ("_botRosterForMentions", "_renderBotMentionPopup",
                     "_hideBotMentionPopup", "_insertBotMention"):
            assert f"function {name}(" in src, f"{name} missing (#7174)"

    def test_composer_wires_mention_popup(self):
        src = PANELS_JS_PATH.read_text(encoding="utf-8")
        start = src.find("function _wireBotMentionAutocomplete(")
        assert start >= 0, "_wireBotMentionAutocomplete missing (#7174)"
        end = src.find("\nfunction ", start + 1)
        body = src[start:end if end > 0 else start + 4000]
        assert "'msg'" in body, "autocomplete must bind to the #msg composer (#7174)"
