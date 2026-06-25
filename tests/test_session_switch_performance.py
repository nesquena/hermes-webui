import api.models as models


def test_compression_continuation_fallback_reads_only_file_head(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = tmp_path / "_index.json"
    index_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    parent_sid = "parent"
    candidate = session_dir / "child.json"
    candidate.write_text("x" * 8192 + f'"parent_session_id": "{parent_sid}"', encoding="utf-8")

    opened_for_read = []
    read_text_calls = []

    original_path_open = models.Path.open
    original_path_read_text = models.Path.read_text

    class _TrackingHandle:
        def __init__(self, path, inner):
            self._path = str(path)
            self._inner = inner

        def read(self, size=-1):
            opened_for_read.append((self._path, size))
            return self._inner.read(size)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def tracking_open(path, *args, **kwargs):
        return _TrackingHandle(path, original_path_open(path, *args, **kwargs))

    def tracking_read_text(self, *args, **kwargs):
        read_text_calls.append(str(self))
        return original_path_read_text(self, *args, **kwargs)

    monkeypatch.setattr(models.Path, "open", tracking_open)
    monkeypatch.setattr(models.Path, "read_text", tracking_read_text)

    session = models.Session(session_id=parent_sid)

    assert models._has_compression_continuation(session) is False

    assert str(index_file) in read_text_calls
    assert all(path != str(candidate) for path in read_text_calls)

    candidate_reads = [size for path, size in opened_for_read if path == str(candidate)]
    assert candidate_reads, "fallback should read the candidate sidecar"
    # Bounded read: at most 16384 bytes (== 4096 UTF-8 chars worst case), never
    # an unbounded -1 read of the full sidecar.
    assert all(size != -1 and size <= 16384 for size in candidate_reads)


def test_compression_continuation_prefix_match_stays_true(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = tmp_path / "_index.json"
    index_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    parent_sid = "parent"
    candidate = session_dir / "child.json"
    candidate.write_text('{"session_id":"child", "parent_session_id": "parent",', encoding="utf-8")

    session = models.Session(session_id=parent_sid)

    assert models._has_compression_continuation(session) is True


def test_compression_continuation_multibyte_summary_before_marker(monkeypatch, tmp_path):
    """A multi-byte (emoji) compression summary written BEFORE parent_session_id
    can push the marker past a 4096-BYTE cutoff while still inside the old
    4096-CHARACTER prefix. The bounded read must preserve the char-prefix
    semantics (read 16384 bytes, slice 4096 chars) so the continuation is still
    detected. Regression for the gate-found byte-vs-char truncation."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = tmp_path / "_index.json"
    index_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    parent_sid = "parent"
    candidate = session_dir / "child.json"
    # 1200 emoji (4 bytes each in UTF-8) → marker at char offset ~1230 but byte
    # offset ~4900: within the old 4096-char window, PAST a 4096-byte window.
    emoji_summary = "😀" * 1200
    candidate.write_text(
        '{"session_id":"child", "compression_anchor_summary": "' + emoji_summary
        + '", "parent_session_id": "parent",',
        encoding="utf-8",
    )

    # sanity: the marker is past 4096 bytes but within 4096 chars
    text = candidate.read_text(encoding="utf-8")
    needle = '"parent_session_id": "parent"'
    assert text.encode("utf-8").index(needle.encode()) > 4096, "test setup: needle must be past 4096 bytes"
    assert text.index(needle) < 4096, "test setup: needle must be within 4096 chars"

    session = models.Session(session_id=parent_sid)
    assert models._has_compression_continuation(session) is True
