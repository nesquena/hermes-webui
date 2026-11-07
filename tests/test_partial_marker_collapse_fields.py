"""Adjacent `_partial` markers must not collapse when they differ.

`_partial_message_signature` used to key on content + reasoning + tool calls
only. Two adjacent partial markers that differed in attachments, timestamp, id,
provider mirror or persisted image provenance had equal signatures, so
`_collapse_adjacent_duplicate_partials` deleted one — and the loader persists
that shrink immediately (api/models.py:2480-2488), making the loss permanent
except via `.bak`.
"""
from __future__ import annotations

import pytest


def _row(**overrides):
    row = {"role": "assistant", "_partial": True, "content": "working", "timestamp": 1}
    row.update(overrides)
    return row


def test_distinct_attachments_are_not_collapsed():
    from api.models import _collapse_adjacent_duplicate_partials

    rows = [
        _row(attachments=[{"name": "a.png"}], timestamp=1),
        _row(attachments=[{"name": "b.png"}], timestamp=2),
    ]
    collapsed, changed = _collapse_adjacent_duplicate_partials(rows)
    assert len(collapsed) == 2
    assert changed is False


@pytest.mark.parametrize("field_a,field_b", [
    ({"timestamp": 1}, {"timestamp": 2}),
    ({"id": "evt-1"}, {"id": "evt-2"}),
    ({"_image_payload_digest": "aaaa"}, {"_image_payload_digest": "bbbb"}),
    ({"api_content": '[{"type":"text","text":"a"}]'}, {"api_content": '[{"type":"text","text":"b"}]'}),
])
def test_differing_provenance_keeps_both_rows(field_a, field_b):
    from api.models import _collapse_adjacent_duplicate_partials

    base = {"role": "assistant", "_partial": True, "content": "working"}
    collapsed, changed = _collapse_adjacent_duplicate_partials([{**base, **field_a}, {**base, **field_b}])
    assert len(collapsed) == 2
    assert changed is False


def test_true_duplicate_partials_still_collapse():
    from api.models import _collapse_adjacent_duplicate_partials

    row = _row()
    collapsed, changed = _collapse_adjacent_duplicate_partials([row, dict(row)])
    assert len(collapsed) == 1
    assert changed is True
