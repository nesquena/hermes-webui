#!/usr/bin/env python3
"""Offline, bounded-memory repair for replay-bloated session sidecars.

The WebUI intentionally refuses to repair very large sidecars inline. This tool
performs a two-pass, exact-JSON replay reduction while holding an operator lock,
then publishes only if the source generation and SHA-256 are unchanged. It keeps
a full backup and a checksum manifest for deterministic rollback.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import math
import os
import pickle
import resource
import secrets
import shutil
import sqlite3
import stat as stat_module
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, TextIO, overload

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Compatibility helpers shared by the independently imported classifier
# fallbacks below. A WebUI base can expose one classifier without the others;
# importing them as one group would silently discard the available semantics.
_STRUCTURED_FIELDS = frozenset({
    'tool_call_id',
    'tool_calls',
    'function_call',
    'function_calls',
    '_partial_tool_calls',
    'refusal',
    'attachments',
})


def _strict_json_tree(value) -> bool:
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_strict_json_tree(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _strict_json_tree(item)
            for key, item in value.items()
        )
    return False


def _message_digest(message):
    if not _strict_json_tree(message):
        return None
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload).digest()


def _typed_scalar(value):
    if isinstance(value, bool):
        return None
    if type(value) is str:
        return ('str', value) if value else None
    if type(value) is int:
        return ('int', value)
    if type(value) is float and math.isfinite(value):
        return ('float', value)
    return None


def _plain_empty_assistant(message) -> bool:
    content = message.get('content') if type(message) is dict else None
    return bool(
        type(message) is dict
        and message.get('role') == 'assistant'
        and (
            content is None
            or (
                type(content) is str
                and not content.strip()
            )
        )
        and not any(field in message for field in _STRUCTURED_FIELDS)
    )


from api import models as _models  # noqa: E402

_repo_partial_message_signature = getattr(
    _models,
    '_partial_message_signature',
    None,
)
_repo_incomplete_reasoning_message_id = getattr(
    _models,
    '_incomplete_reasoning_message_id',
    None,
)
_repo_durable_empty_assistant_replay_key = getattr(
    _models,
    '_durable_empty_assistant_replay_key',
    None,
)


def _partial_message_signature(message) -> bytes | tuple | None:
    if callable(_repo_partial_message_signature):
        result = _repo_partial_message_signature(message)
        return result if isinstance(result, (bytes, tuple)) else None
    if type(message) is not dict:
        return None
    digest = _message_digest(message)
    return ('exact', digest) if digest is not None else None


def _incomplete_reasoning_message_id(message) -> tuple | None:
    if callable(_repo_incomplete_reasoning_message_id):
        result = _repo_incomplete_reasoning_message_id(message)
        return result if isinstance(result, tuple) else None
    if not _plain_empty_assistant(message):
        return None
    if str(message.get('finish_reason') or '').lower() != 'incomplete':
        return None
    message_id = _typed_scalar(message.get('id'))
    digest = _message_digest(message)
    if message_id is None or digest is None:
        return None
    return ('message_id', message_id, digest)


def _durable_empty_assistant_replay_key(message) -> tuple | None:
    if callable(_repo_durable_empty_assistant_replay_key):
        result = _repo_durable_empty_assistant_replay_key(message)
        return result if isinstance(result, tuple) else None
    if not _plain_empty_assistant(message) or message.get('_partial'):
        return None
    if str(message.get('finish_reason') or '').lower() == 'incomplete':
        return None
    digest = _message_digest(message)
    if digest is None:
        return None
    message_id = _typed_scalar(message.get('id'))
    if message_id is not None:
        return ('message_id', message_id, digest)
    stream_id = _typed_scalar(message.get('_recovered_stream_id'))
    timestamp = message.get('timestamp', message.get('_ts'))
    timestamp_key = _typed_scalar(timestamp)
    if stream_id is None or timestamp_key is None:
        return None
    return ('recovered', stream_id, timestamp_key, digest)

_CHUNK_CHARS = 1 << 20
_MAX_ITEM_CHARS = 64 << 20
_MAX_CAPTURED_STRING_CHARS = 65_536
_TARGET_ARRAYS = frozenset({'messages', 'context_messages'})


class StreamJSONError(ValueError):
    """Raised when a sidecar cannot be transformed safely."""


def _reject_json_constant(constant: str):
    raise StreamJSONError(f'invalid JSON constant: {constant}')


def _is_json_whitespace(char: str) -> bool:
    """Return whether ``char`` is whitespace permitted by RFC 8259."""
    return char in ' \t\r\n'


class StreamReader:
    def __init__(self, handle: TextIO, chunk_chars: int = _CHUNK_CHARS):
        self.handle = handle
        self.chunk_chars = chunk_chars
        self.buffer = ''
        self.pos = 0
        self.eof = False
        self.decoder = json.JSONDecoder(parse_constant=_reject_json_constant)

    def _compact_and_fill(self) -> bool:
        if self.pos:
            self.buffer = self.buffer[self.pos:]
            self.pos = 0
        if self.eof:
            return False
        chunk = self.handle.read(self.chunk_chars)
        if chunk:
            self.buffer += chunk
            return True
        self.eof = True
        return False

    def ensure(self) -> bool:
        while self.pos >= len(self.buffer):
            if not self._compact_and_fill():
                return False
        return True

    def peek(self) -> str:
        if not self.ensure():
            return ''
        return self.buffer[self.pos]

    def take(self) -> str:
        char = self.peek()
        if not char:
            raise StreamJSONError('unexpected end of JSON')
        self.pos += 1
        return char

    def skip_ws(self) -> None:
        while True:
            if not self.ensure():
                return
            start = self.pos
            while (
                self.pos < len(self.buffer)
                and _is_json_whitespace(self.buffer[self.pos])
            ):
                self.pos += 1
            if self.pos == start:
                return

    def expect(self, expected: str) -> None:
        self.skip_ws()
        actual = self.take()
        if actual != expected:
            raise StreamJSONError(f'expected {expected!r}, got {actual!r}')

    def decode_value(self):
        self.skip_ws()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.pos)
            except json.JSONDecodeError as exc:
                retained = len(self.buffer) - self.pos
                if retained > _MAX_ITEM_CHARS:
                    raise StreamJSONError(
                        f'one JSON item exceeds {_MAX_ITEM_CHARS} characters'
                    ) from exc
                if self._compact_and_fill():
                    continue
                raise StreamJSONError(f'invalid JSON value: {exc}') from exc
            if (
                end == len(self.buffer)
                and not self.eof
                and type(value) in (int, float)
            ):
                retained = len(self.buffer) - self.pos
                if retained > _MAX_ITEM_CHARS:
                    raise StreamJSONError(
                        f'one JSON item exceeds {_MAX_ITEM_CHARS} characters'
                    )
                if self._compact_and_fill():
                    continue
            self.pos = end
            return value

    def copy_raw_value(self, output: TextIO | None, *, _depth: int = 0) -> None:
        """Validate and copy one JSON value without materializing containers."""
        if _depth > 512:
            raise StreamJSONError('invalid JSON value: nesting exceeds 512 levels')
        self._copy_ws(output)
        first = self.peek()
        if not first:
            raise StreamJSONError('invalid JSON value: missing value')
        if first == '"':
            self._copy_string(output, capture=False)
            return
        if first == '{':
            self._copy_object(output, _depth)
            return
        if first == '[':
            self._copy_array(output, _depth)
            return
        self._copy_scalar(output)

    def _copy_ws(self, output: TextIO | None) -> None:
        pending: list[str] = []
        while self.peek() and _is_json_whitespace(self.peek()):
            pending.append(self.take())
            if len(pending) >= 65_536:
                self._flush(output, pending)
        self._flush(output, pending)

    def _copy_string(
        self,
        output: TextIO | None,
        *,
        capture: bool = True,
    ) -> str | None:
        opening = self.take()
        if opening != '"':
            raise StreamJSONError('invalid JSON string: expected opening quote')
        pending = [opening]
        captured = [opening] if capture else None
        total_chars = 1

        def append(char: str) -> None:
            nonlocal total_chars
            pending.append(char)
            total_chars += 1
            if total_chars > _MAX_ITEM_CHARS:
                raise StreamJSONError(
                    f'invalid JSON string: exceeds {_MAX_ITEM_CHARS} characters'
                )
            if captured is not None:
                captured.append(char)
                if len(captured) > _MAX_CAPTURED_STRING_CHARS:
                    raise StreamJSONError(
                        'captured JSON string exceeds the bounded key limit'
                    )
            if len(pending) >= 65_536:
                self._flush(output, pending)

        while True:
            char = self.take()
            append(char)
            if char == '"':
                break
            if char == '\\':
                escape = self.take()
                append(escape)
                if escape == 'u':
                    for _ in range(4):
                        digit = self.take()
                        append(digit)
                        if digit not in '0123456789abcdefABCDEF':
                            raise StreamJSONError(
                                'invalid JSON string: malformed unicode escape'
                            )
                elif escape not in '"\\/bfnrt':
                    raise StreamJSONError(
                        f'invalid JSON string escape: {escape!r}'
                    )
            elif ord(char) < 0x20:
                raise StreamJSONError(
                    'invalid JSON string: unescaped control character'
                )
        self._flush(output, pending)
        if captured is None:
            return None
        text = ''.join(captured)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StreamJSONError(f'invalid JSON string: {exc}') from exc
        if not isinstance(value, str):
            raise StreamJSONError('invalid JSON string')
        return value

    def _copy_object(self, output: TextIO | None, depth: int) -> None:
        opening = self.take()
        if output is not None:
            output.write(opening)
        self._copy_ws(output)
        if self.peek() == '}':
            closing = self.take()
            if output is not None:
                output.write(closing)
            return
        while True:
            if self.peek() != '"':
                raise StreamJSONError('invalid JSON object key: expected string')
            self._copy_string(output, capture=False)
            self._copy_ws(output)
            if self.take() != ':':
                raise StreamJSONError("invalid JSON object: expected ':'")
            if output is not None:
                output.write(':')
            self.copy_raw_value(output, _depth=depth + 1)
            self._copy_ws(output)
            delimiter = self.take()
            if delimiter not in ',}':
                raise StreamJSONError(
                    f'invalid JSON object delimiter: {delimiter!r}'
                )
            if output is not None:
                output.write(delimiter)
            if delimiter == '}':
                return
            self._copy_ws(output)

    def _copy_array(self, output: TextIO | None, depth: int) -> None:
        opening = self.take()
        if output is not None:
            output.write(opening)
        self._copy_ws(output)
        if self.peek() == ']':
            closing = self.take()
            if output is not None:
                output.write(closing)
            return
        while True:
            self.copy_raw_value(output, _depth=depth + 1)
            self._copy_ws(output)
            delimiter = self.take()
            if delimiter not in ',]':
                raise StreamJSONError(
                    f'invalid JSON array delimiter: {delimiter!r}'
                )
            if output is not None:
                output.write(delimiter)
            if delimiter == ']':
                return
            self._copy_ws(output)

    def _copy_scalar(self, output: TextIO | None) -> None:
        pending: list[str] = []
        while True:
            char = self.peek()
            if not char or _is_json_whitespace(char) or char in ',}]':
                break
            pending.append(self.take())
            if len(pending) > _MAX_ITEM_CHARS:
                raise StreamJSONError(
                    f'invalid JSON scalar: exceeds {_MAX_ITEM_CHARS} characters'
                )
        text = ''.join(pending)
        if not text:
            raise StreamJSONError('invalid JSON scalar: empty value')
        try:
            value = json.loads(
                text,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise StreamJSONError(f'invalid JSON scalar: {exc}') from exc
        if isinstance(value, (dict, list)):
            raise StreamJSONError('invalid JSON scalar')
        if output is not None:
            output.write(text)

    @staticmethod
    def _flush(output: TextIO | None, pending: list[str]) -> None:
        if output is not None and pending:
            output.write(''.join(pending))
        pending.clear()


def _valid_generation(value) -> int:
    if type(value) is not int or value < 0:
        raise StreamJSONError(
            '_sidecar_generation_v1 must be a non-negative integer'
        )
    return value


def _valid_epoch(value) -> str:
    if type(value) is not str or len(value) != 32 or any(
        char not in '0123456789abcdef' for char in value
    ):
        raise StreamJSONError('_sidecar_epoch_v1 must be 32 lowercase hex characters')
    return value


def _copy_with_generation(
    source: TextIO,
    target: TextIO,
    generation: int,
    epoch: str,
) -> None:
    generation = _valid_generation(generation)
    epoch = _valid_epoch(epoch)
    reader = StreamReader(source)
    reader.expect('{')
    target.write('{')
    reader.skip_ws()
    if reader.peek() == '}':
        reader.take()
        target.write(
            f'"_sidecar_epoch_v1":{json.dumps(epoch)},'
            f'"_sidecar_generation_v1":{generation}' + '}'
        )
        return
    saw_generation = False
    saw_epoch = False
    while True:
        reader.skip_ws()
        if reader.peek() != '"':
            raise StreamJSONError('sidecar object key must be a string')
        key = reader._copy_string(None)
        if type(key) is not str:
            raise StreamJSONError('sidecar object key must be a string')
        key_text = json.dumps(key, ensure_ascii=False)
        target.write(key_text)
        reader.expect(':')
        target.write(':')
        if key == '_sidecar_generation_v1':
            reader.copy_raw_value(None)
            target.write(str(generation))
            saw_generation = True
        elif key == '_sidecar_epoch_v1':
            reader.copy_raw_value(None)
            target.write(json.dumps(epoch))
            saw_epoch = True
        else:
            reader.copy_raw_value(target)
        reader.skip_ws()
        delimiter = reader.take()
        if delimiter == '}':
            if not saw_generation:
                target.write(f',"_sidecar_generation_v1":{generation}')
            if not saw_epoch:
                target.write(f',"_sidecar_epoch_v1":{json.dumps(epoch)}')
            target.write('}')
            return
        if delimiter != ',':
            raise StreamJSONError(
                f'expected object delimiter, got {delimiter!r}'
            )
        target.write(',')


@dataclass
class ArrayStats:
    input_count: int = 0
    output_count: int = 0
    changed: bool = False


class SeenKeyIndex:
    """Exact disk-backed membership with a bounded SQLite page cache."""

    def __init__(self):
        self.connection = sqlite3.connect('')
        self.connection.execute('PRAGMA journal_mode=OFF')
        self.connection.execute('PRAGMA synchronous=OFF')
        self.connection.execute('PRAGMA temp_store=FILE')
        self.connection.execute('PRAGMA cache_size=-4096')
        self.connection.execute(
            'CREATE TABLE seen ('
            'namespace INTEGER NOT NULL, '
            'replay_key BLOB NOT NULL, '
            'PRIMARY KEY (namespace, replay_key)'
            ') WITHOUT ROWID'
        )

    def add(self, namespace: int, key: tuple) -> bool:
        encoded = sqlite3.Binary(pickle.dumps(key, protocol=5))
        cursor = self.connection.execute(
            'INSERT OR IGNORE INTO seen(namespace, replay_key) VALUES (?, ?)',
            (namespace, encoded),
        )
        return cursor.rowcount == 1

    def close(self) -> None:
        self.connection.close()


class ReplayReducer:
    def __init__(self):
        self.previous_partial = None
        self.seen = SeenKeyIndex()

    def close(self) -> None:
        self.seen.close()

    def keep(self, message) -> bool:
        if isinstance(message, dict) and message.get('_partial'):
            partial_key = _partial_message_signature(message)
            if partial_key is not None and partial_key == self.previous_partial:
                return False
            self.previous_partial = partial_key
        else:
            self.previous_partial = None

        incomplete_key = _incomplete_reasoning_message_id(message)
        if incomplete_key is not None:
            if not self.seen.add(1, incomplete_key):
                return False

        durable_key = _durable_empty_assistant_replay_key(message)
        if durable_key is not None:
            if not self.seen.add(2, durable_key):
                return False
        return True


def _transform_array(reader: StreamReader, output: TextIO | None) -> ArrayStats:
    stats = ArrayStats()
    reader.expect('[')
    if output is not None:
        output.write('[')
    reader.skip_ws()
    first_output = True
    if reader.peek() == ']':
        reader.take()
        if output is not None:
            output.write(']')
        return stats
    reducer = ReplayReducer()
    try:
        while True:
            message = reader.decode_value()
            stats.input_count += 1
            if reducer.keep(message):
                stats.output_count += 1
                if output is not None:
                    if not first_output:
                        output.write(',')
                    output.write(
                        json.dumps(
                            message,
                            ensure_ascii=False,
                            separators=(',', ':'),
                            allow_nan=False,
                        )
                    )
                    first_output = False
            else:
                stats.changed = True
            reader.skip_ws()
            delimiter = reader.take()
            if delimiter == ']':
                break
            if delimiter != ',':
                raise StreamJSONError(f'expected array delimiter, got {delimiter!r}')
    finally:
        reducer.close()
    if output is not None:
        output.write(']')
    return stats


def transform_sidecar(
    source: Path,
    output: TextIO | None = None,
    *,
    known: dict[str, ArrayStats] | None = None,
    write_generation: int | None = None,
    write_epoch: str | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, ArrayStats]:
    if write_generation is not None:
        write_generation = _valid_generation(write_generation)
    if write_epoch is not None:
        write_epoch = _valid_epoch(write_epoch)
    stats: dict[str, ArrayStats] = {}
    with source.open('r', encoding='utf-8') as handle:
        reader = StreamReader(handle)
        reader.expect('{')
        if output is not None:
            output.write('{')
        first_field = True
        reader.skip_ws()
        if reader.peek() == '}':
            reader.take()
            if metadata is not None:
                metadata['generation'] = 0
                metadata['epoch'] = None
            if output is not None:
                injected = []
                if write_epoch is not None:
                    injected.append(
                        f'"_sidecar_epoch_v1":{json.dumps(write_epoch)}'
                    )
                if write_generation is not None:
                    injected.append(f'"_sidecar_generation_v1":{write_generation}')
                output.write(','.join(injected))
                output.write('}')
            return stats
        saw_generation = False
        saw_epoch = False
        if output is not None:
            injected = []
            if write_epoch is not None:
                injected.append(f'"_sidecar_epoch_v1":{json.dumps(write_epoch)}')
            if write_generation is not None:
                injected.append(f'"_sidecar_generation_v1":{write_generation}')
            if injected:
                output.write(','.join(injected))
                first_field = False
        while True:
            reader.skip_ws()
            if reader.peek() != '"':
                raise StreamJSONError('top-level session keys must be strings')
            key = reader._copy_string(None)
            if type(key) is not str:
                raise StreamJSONError('top-level session keys must be strings')
            reader.expect(':')
            emit_field = not (
                output is not None
                and (
                    (write_generation is not None and key == '_sidecar_generation_v1')
                    or (write_epoch is not None and key == '_sidecar_epoch_v1')
                )
            )
            if output is not None and emit_field:
                if not first_field:
                    output.write(',')
                output.write(json.dumps(key, ensure_ascii=False))
                output.write(':')
            if key in _TARGET_ARRAYS:
                array_stats = _transform_array(reader, output)
                stats[key] = array_stats
                if known is not None and array_stats != known.get(key, ArrayStats()):
                    raise StreamJSONError(f'{key} changed between analysis and write pass')
            elif key == 'message_count' and known is not None and 'messages' in known:
                reader.copy_raw_value(None)
                if output is not None:
                    output.write(str(known['messages'].output_count))
            elif (
                key == 'compression_anchor_visible_idx'
                and known is not None
                and known.get('messages', ArrayStats()).changed
            ):
                reader.copy_raw_value(None)
                if output is not None:
                    output.write('null')
            elif key == '_sidecar_generation_v1':
                source_generation = _valid_generation(reader.decode_value())
                if metadata is not None:
                    metadata['generation'] = source_generation
                if output is not None and emit_field:
                    output.write(
                        str(
                            write_generation
                            if write_generation is not None
                            else source_generation
                        )
                    )
                saw_generation = True
            elif key == '_sidecar_epoch_v1':
                source_epoch = _valid_epoch(reader.decode_value())
                if metadata is not None:
                    metadata['epoch'] = source_epoch
                if output is not None and emit_field:
                    output.write(json.dumps(write_epoch or source_epoch))
                saw_epoch = True
            else:
                reader.copy_raw_value(output)
            if output is not None and emit_field:
                first_field = False
            reader.skip_ws()
            delimiter = reader.take()
            if delimiter == '}':
                break
            if delimiter != ',':
                raise StreamJSONError(
                    f'expected top-level delimiter, got {delimiter!r}'
                )
        reader.skip_ws()
        if reader.peek():
            raise StreamJSONError('trailing data after session object')
        if metadata is not None and not saw_generation:
            metadata['generation'] = 0
        if metadata is not None and not saw_epoch:
            metadata['epoch'] = None
        if output is not None:
            output.write('}')
    return stats


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_open_file(handle) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    for chunk in iter(lambda: handle.read(4 << 20), b''):
        digest.update(chunk)
    return digest.hexdigest()


def _open_regular_binary_nofollow(path: Path, *, label: str):
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StreamJSONError(f'{label} must be a non-symlink regular file: {path}') from exc
    try:
        if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
            raise StreamJSONError(f'{label} must be a regular file: {path}')
        return os.fdopen(descriptor, 'rb')
    except Exception:
        os.close(descriptor)
        raise


def _signature(path: Path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@overload
def _open_exclusive_private(path: Path, mode: Literal['wb']) -> BinaryIO: ...


@overload
def _open_exclusive_private(path: Path, mode: Literal['w']) -> TextIO: ...


def _open_exclusive_private(path: Path, mode: Literal['w', 'wb']):
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, 'O_CLOEXEC', 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        if 'b' in mode:
            return os.fdopen(descriptor, mode)
        return os.fdopen(descriptor, mode, encoding='utf-8')
    except Exception:
        os.close(descriptor)
        raise


def _copy_exclusive(
    source: Path,
    destination: Path,
    *,
    mode: int | None = None,
) -> None:
    try:
        with source.open('rb') as src, _open_exclusive_private(destination, 'wb') as dst:
            os.fchmod(
                dst.fileno(),
                mode if mode is not None else stat_module.S_IMODE(source.stat().st_mode),
            )
            shutil.copyfileobj(src, dst, length=4 << 20)
            dst.flush()
            os.fsync(dst.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _sidecar_lock_path(path: Path) -> Path:
    lock_dir = path.parent / '.sidecar-locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f'{path.stem}.lock'


def _validate_sidecar_identity(path: Path) -> str:
    try:
        metadata = _models._read_bounded_session_metadata(path)
    except ValueError as exc:
        raise StreamJSONError(
            f'invalid JSON while validating sidecar session_id: {exc}'
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise StreamJSONError(f'cannot validate sidecar session_id: {path}') from exc
    session_id = metadata.get('session_id')
    if type(session_id) is str and len(session_id) > 150:
        raise StreamJSONError('sidecar session_id exceeds the 150-character artifact limit')
    if (
        type(session_id) is not str
        or not _models.is_safe_session_id(session_id)
        or session_id != path.stem
    ):
        raise StreamJSONError(
            f'sidecar session_id must match filename: {session_id!r} != {path.stem!r}'
        )
    return session_id


def compact_sidecar(path: Path, *, dry_run: bool = False) -> dict:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise StreamJSONError(f'source must not be a symlink: {path}')
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    lock_path = _sidecar_lock_path(path)
    with lock_path.open('a+', encoding='utf-8') as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        _validate_sidecar_identity(path)
        source_mode = stat_module.S_IMODE(path.stat().st_mode)
        source_signature = _signature(path)
        source_sha256 = _sha256(path)
        source_metadata: dict[str, object] = {}
        analysis = transform_sidecar(path, metadata=source_metadata)
        source_generation = _valid_generation(source_metadata['generation'])
        source_epoch = source_metadata['epoch']
        output_epoch = (
            _valid_epoch(source_epoch)
            if source_epoch is not None
            else secrets.token_hex(16)
        )
        output_generation = source_generation + 1
        changed = any(value.changed for value in analysis.values())
        result = {
            'status': 'would_compact' if changed else 'no_change',
            'source': str(path),
            'source_sha256': source_sha256,
            'source_generation': source_generation,
            'arrays': {
                key: {
                    'input': value.input_count,
                    'output': value.output_count,
                    'removed': value.input_count - value.output_count,
                }
                for key, value in sorted(analysis.items())
            },
        }
        if dry_run or not changed:
            result['max_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return result

        backup = path.with_name(f'{path.name}.replay-v10.{source_sha256[:16]}.bak')
        if backup.exists():
            if _sha256(backup) != source_sha256:
                raise StreamJSONError(f'existing backup checksum mismatch: {backup}')
            os.chmod(backup, source_mode)
        else:
            _copy_exclusive(path, backup, mode=source_mode)
            if _sha256(backup) != source_sha256:
                backup.unlink(missing_ok=True)
                raise StreamJSONError('source changed while the backup was copied')
            _fsync_dir(path.parent)

        temp = path.with_name(
            f'.{path.name}.replay-v10.tmp.{os.getpid()}.{secrets.token_hex(8)}'
        )
        manifest = path.with_name(
            f'_replay-v10.{path.name}.{source_sha256[:16]}.manifest.json'
        )
        manifest_tmp = None
        manifest_published = False
        source_installed = False
        try:
            with _open_exclusive_private(temp, 'w') as output:
                os.fchmod(output.fileno(), source_mode)
                written = transform_sidecar(
                    path,
                    output,
                    known=analysis,
                    write_generation=output_generation,
                    write_epoch=output_epoch,
                )
                output.flush()
                os.fsync(output.fileno())
            verification_metadata: dict[str, object] = {}
            verification = transform_sidecar(temp, metadata=verification_metadata)
            if any(item.changed for item in verification.values()):
                raise StreamJSONError('written sidecar is not replay-idempotent')
            if {
                key: item.output_count for key, item in written.items()
            } != {
                key: item.output_count for key, item in verification.items()
            }:
                raise StreamJSONError('written sidecar counts failed verification')
            if verification_metadata['generation'] != output_generation:
                raise StreamJSONError('written sidecar generation failed verification')
            if verification_metadata['epoch'] != output_epoch:
                raise StreamJSONError('written sidecar epoch failed verification')
            output_sha256 = _sha256(temp)
            if _signature(path) != source_signature or _sha256(path) != source_sha256:
                raise StreamJSONError('source generation changed during offline repair')
            manifest_payload = {
                'version': 2,
                'source': str(path),
                'backup': str(backup),
                'source_sha256': source_sha256,
                'output_sha256': output_sha256,
                'source_generation': source_generation,
                'output_generation': output_generation,
                'sidecar_epoch': output_epoch,
                'arrays': result['arrays'],
            }
            manifest_tmp = manifest.with_name(
                f'.{manifest.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}'
            )
            with _open_exclusive_private(manifest_tmp, 'w') as handle:
                os.fchmod(handle.fileno(), source_mode)
                handle.write(
                    json.dumps(manifest_payload, ensure_ascii=False, indent=2) + '\n'
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(manifest_tmp, manifest)
            manifest_published = True
            _fsync_dir(path.parent)
            os.replace(temp, path)
            source_installed = True
            _fsync_dir(path.parent)
        except BaseException as exc:
            if manifest_published and not source_installed:
                try:
                    manifest.unlink(missing_ok=True)
                    _fsync_dir(path.parent)
                except Exception as cleanup_exc:
                    exc.add_note(
                        f'failed to remove unpublished manifest {manifest}: '
                        f'{cleanup_exc}'
                    )
            raise
        finally:
            temp.unlink(missing_ok=True)
            if manifest_tmp is not None:
                manifest_tmp.unlink(missing_ok=True)
        result.update(
            status='compacted',
            backup=str(backup),
            manifest=str(manifest),
            output_sha256=_sha256(path),
            output_generation=output_generation,
            max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        return result


def restore_manifest(manifest: Path) -> dict:
    manifest = manifest.resolve()
    payload = json.loads(manifest.read_text(encoding='utf-8'))
    source = Path(payload['source'])
    backup = Path(payload['backup'])
    lock_path = _sidecar_lock_path(source)
    with lock_path.open('a+', encoding='utf-8') as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        source_mode = stat_module.S_IMODE(source.stat().st_mode)
        source_signature = _signature(source)
        if _sha256(source) != payload['output_sha256']:
            raise StreamJSONError('current sidecar no longer matches compacted output')
        expected_generation = payload.get('output_generation')
        current_generation = _valid_generation(
            expected_generation if expected_generation is not None else 0
        )
        restore_epoch = (
            _valid_epoch(payload['sidecar_epoch'])
            if payload.get('sidecar_epoch') is not None
            else secrets.token_hex(16)
        )
        restore_generation = current_generation + 1
        temp = source.with_name(
            f'.{source.name}.replay-v10.restore.{os.getpid()}.{secrets.token_hex(8)}'
        )
        try:
            with _open_regular_binary_nofollow(backup, label='backup') as backup_raw:
                if _sha256_open_file(backup_raw) != payload['source_sha256']:
                    raise StreamJSONError('backup checksum mismatch')
                backup_raw.seek(0)
                backup_text = io.TextIOWrapper(backup_raw, encoding='utf-8')
                try:
                    with _open_exclusive_private(temp, 'w') as output:
                        os.fchmod(output.fileno(), source_mode)
                        _copy_with_generation(
                            backup_text,
                            output,
                            restore_generation,
                            restore_epoch,
                        )
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    backup_text.detach()
            restored_metadata: dict[str, object] = {}
            transform_sidecar(temp, metadata=restored_metadata)
            if restored_metadata['generation'] != restore_generation:
                raise StreamJSONError('restored sidecar generation failed verification')
            if restored_metadata['epoch'] != restore_epoch:
                raise StreamJSONError('restored sidecar epoch failed verification')
            restored_sha256 = _sha256(temp)
            if (
                _signature(source) != source_signature
                or _sha256(source) != payload['output_sha256']
            ):
                raise StreamJSONError('sidecar changed during rollback preparation')
            os.replace(temp, source)
            _fsync_dir(source.parent)
        finally:
            temp.unlink(missing_ok=True)
    return {
        'status': 'restored',
        'source': str(source),
        'source_sha256': payload['source_sha256'],
        'output_sha256': restored_sha256,
        'output_generation': restore_generation,
        'sidecar_epoch': restore_epoch,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('path', nargs='?', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    if bool(args.restore) == bool(args.path):
        parser.error('provide exactly one session path or --restore MANIFEST')
    if args.restore and args.dry_run:
        parser.error('--dry-run cannot be combined with --restore')
    try:
        result = (
            restore_manifest(args.restore)
            if args.restore
            else compact_sidecar(args.path, dry_run=args.dry_run)
        )
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
