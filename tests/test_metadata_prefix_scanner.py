"""_find_top_level_json_key returns the same offsets as the previous character loop."""
import random

from api.models import _find_top_level_json_key


def _reference(text, key):
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            start = i
            i += 1
            escaped = False
            chars = []
            while i < n:
                c = text[i]
                if escaped:
                    chars.append(c)
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    break
                else:
                    chars.append(c)
                i += 1
            if i >= n:
                return None
            if depth == 1 and ''.join(chars) == key:
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j < n and text[j] == ':':
                    return start
        elif ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
        i += 1
    return None


def test_offsets_match_previous_scanner():
    samples = [
        '{"summary": "a \\"messages\\": {[ x", "nested": {"messages": 1}, "messages": [',
        '{"mess\\u0061ges": 1, "m\\essages" :2, "messages"\n: [',
        '{"title": "caf\u00e9 \u2603", "messages": []}',
        '{"title": "unfinished \\',
        '{"a": [{"messages": 1}], "messages": [',
    ]
    rng = random.Random(1558)
    for _ in range(2000):
        samples.append(''.join(rng.choice('{}[]":,\\ messag\u00e9x') for _ in range(rng.randrange(40))))
    for text in samples:
        for end in range(len(text) + 1):
            assert _find_top_level_json_key(text[:end], 'messages') == _reference(text[:end], 'messages'), text[:end]
