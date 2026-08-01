"""Trusted gateway wake provenance projection and delivery-id dedup."""

from api.models import _normalize_wakeup_rows_for_display


WAKE_TEXT = (
    "[IMPORTANT: Background process proc_5b9fcce4cbff completed (exit_code=1).\n"
    "Command: make test\nOutput:\nfailed]"
)


def _wake(delivery_id, **extra):
    row = {
        "role": "user",
        "content": WAKE_TEXT,
        "display_kind": "process_wakeup",
        "display_metadata": {"delivery_id": delivery_id},
    }
    row.update(extra)
    return row


def test_trusted_wakeup_is_stamped_and_gets_display_metadata():
    row = _wake("delivery-1")
    assert _normalize_wakeup_rows_for_display([row]) == [row]
    assert row["_source"] == "process_wakeup"
    assert row["_wakeup_meta"]["task_id"] == "proc_5b9fcce4cbff"


def test_same_delivery_id_is_deduplicated():
    first = _wake("delivery-1", timestamp=1)
    twin = _wake("delivery-1", timestamp=2)
    assert _normalize_wakeup_rows_for_display([first, twin]) == [first]


def test_distinct_delivery_ids_never_deduplicate_even_with_identical_text():
    first = _wake("delivery-1", timestamp=1)
    second = _wake("delivery-2", timestamp=2)
    assert _normalize_wakeup_rows_for_display([first, second]) == [first, second]


def test_user_typed_wakeup_shape_stays_byte_identical():
    row = {"role": "user", "content": WAKE_TEXT, "timestamp": 1}
    before = dict(row)
    assert _normalize_wakeup_rows_for_display([row]) == [row]
    assert row == before


def test_untrusted_or_incomplete_provenance_stays_byte_identical():
    rows = [
        _wake("", timestamp=1),
        {"role": "user", "content": WAKE_TEXT, "display_kind": "process_wakeup"},
        {
            "role": "user",
            "content": WAKE_TEXT,
            "display_kind": "other",
            "display_metadata": {"delivery_id": "delivery-1"},
        },
    ]
    before = [dict(row) for row in rows]
    assert _normalize_wakeup_rows_for_display(rows) == rows
    assert rows == before


def test_non_gateway_rows_are_byte_identical():
    rows = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": {"not": "coerced"}},
    ]
    before = [dict(row) for row in rows]
    assert _normalize_wakeup_rows_for_display(rows) == rows
    assert rows == before


def test_empty_and_non_list_passthrough():
    assert _normalize_wakeup_rows_for_display([]) == []
    assert _normalize_wakeup_rows_for_display(None) is None
