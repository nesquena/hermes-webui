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
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

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


def _partial_message_signature(message) -> tuple | None:
    if callable(_repo_partial_message_signature):
        result = _repo_partial_message_signature(message)
        return result if isinstance(result, tuple) else None
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
_TARGET_ARRAYS = frozenset({'messages', 'context_messages'})


class StreamJSONError(ValueError):
    """Raised when a sidecar cannot be transformed safely."""


class StreamReader:
    def __init__(self, handle: TextIO, chunk_chars: int = _CHUNK_CHARS):
        self.handle = handle
        self.chunk_chars = chunk_chars
        self.buffer = ''
        self.pos = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

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
            while self.pos < len(self.buffer) and self.buffer[self.pos].isspace():
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
            self.pos = end
            return value

    def copy_raw_value(self, output: TextIO | None) -> None:
        """Copy one non-target JSON value without materializing containers."""
        self.skip_ws()
        first = self.peek()
        if not first:
            raise StreamJSONError('missing JSON value')
        if first not in '[{"':
            self._copy_scalar(output)
            return

        depth = 0
        in_string = False
        escaped = False
        started_container = first in '[{'
        pending: list[str] = []
        while True:
            char = self.take()
            pending.append(char)
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
                    if not started_container and depth == 0:
                        self._flush(output, pending)
                        return
            else:
                if char == '"':
                    in_string = True
                elif char in '[{':
                    depth += 1
                elif char in ']}':
                    depth -= 1
                    if started_container and depth == 0:
                        self._flush(output, pending)
                        return
            if len(pending) >= 65_536:
                self._flush(output, pending)

    def _copy_scalar(self, output: TextIO | None) -> None:
        pending: list[str] = []
        while True:
            char = self.peek()
            if not char or char in ',}]':
                text = ''.join(pending).rstrip()
                if not text:
                    raise StreamJSONError('empty scalar value')
                if output is not None:
                    output.write(text)
                return
            pending.append(self.take())
            if len(pending) >= 65_536:
                self._flush(output, pending)

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


def _copy_with_generation(source: Path, target: TextIO, generation: int) -> None:
    generation = _valid_generation(generation)
    with source.open('r', encoding='utf-8') as raw:
        reader = StreamReader(raw)
        reader.expect('{')
        target.write('{')
        reader.skip_ws()
        if reader.peek() == '}':
            reader.take()
            target.write(
                f'"_sidecar_generation_v1":{generation}' + '}'
            )
            return
        saw_generation = False
        while True:
            key_buffer = io.StringIO()
            reader.copy_raw_value(key_buffer)
            key_text = key_buffer.getvalue()
            key = json.loads(key_text)
            if not isinstance(key, str):
                raise StreamJSONError('sidecar object key must be a string')
            target.write(key_text)
            reader.expect(':')
            target.write(':')
            if key == '_sidecar_generation_v1':
                reader.copy_raw_value(None)
                target.write(str(generation))
                saw_generation = True
            else:
                reader.copy_raw_value(target)
            reader.skip_ws()
            delimiter = reader.take()
            if delimiter == '}':
                if not saw_generation:
                    target.write(f',"_sidecar_generation_v1":{generation}')
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
    metadata: dict[str, int] | None = None,
) -> dict[str, ArrayStats]:
    if write_generation is not None:
        write_generation = _valid_generation(write_generation)
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
            if output is not None:
                if write_generation is not None:
                    output.write(
                        f'"_sidecar_generation_v1":{write_generation}'
                    )
                output.write('}')
            return stats
        saw_generation = False
        while True:
            key = reader.decode_value()
            if type(key) is not str:
                raise StreamJSONError('top-level session keys must be strings')
            reader.expect(':')
            if output is not None:
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
                if output is not None:
                    output.write(
                        str(
                            write_generation
                            if write_generation is not None
                            else source_generation
                        )
                    )
                saw_generation = True
            else:
                reader.copy_raw_value(output)
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
        if output is not None:
            if write_generation is not None and not saw_generation:
                output.write(
                    f',"_sidecar_generation_v1":{write_generation}'
                )
            output.write('}')
    return stats


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


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


def _copy_exclusive(source: Path, destination: Path) -> None:
    try:
        with source.open('rb') as src, destination.open('xb') as dst:
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


def compact_sidecar(path: Path, *, dry_run: bool = False) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    lock_path = _sidecar_lock_path(path)
    with lock_path.open('a+', encoding='utf-8') as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        source_signature = _signature(path)
        source_sha256 = _sha256(path)
        source_metadata: dict[str, int] = {}
        analysis = transform_sidecar(path, metadata=source_metadata)
        source_generation = source_metadata['generation']
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
        else:
            _copy_exclusive(path, backup)
            if _sha256(backup) != source_sha256:
                backup.unlink(missing_ok=True)
                raise StreamJSONError('source changed while the backup was copied')
            _fsync_dir(path.parent)

        temp = path.with_name(f'.{path.name}.replay-v10.tmp.{os.getpid()}')
        manifest = backup.with_suffix(f'{backup.suffix}.manifest.json')
        try:
            with temp.open('x', encoding='utf-8') as output:
                written = transform_sidecar(
                    path,
                    output,
                    known=analysis,
                    write_generation=output_generation,
                )
                output.flush()
                os.fsync(output.fileno())
            verification_metadata: dict[str, int] = {}
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
                'arrays': result['arrays'],
            }
            manifest_tmp = manifest.with_name(f'.{manifest.name}.tmp.{os.getpid()}')
            manifest_tmp.write_text(
                json.dumps(manifest_payload, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
            with manifest_tmp.open('rb') as handle:
                os.fsync(handle.fileno())
            os.replace(manifest_tmp, manifest)
            os.replace(temp, path)
            _fsync_dir(path.parent)
        finally:
            temp.unlink(missing_ok=True)
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
        source_signature = _signature(source)
        if _sha256(source) != payload['output_sha256']:
            raise StreamJSONError('current sidecar no longer matches compacted output')
        if _sha256(backup) != payload['source_sha256']:
            raise StreamJSONError('backup checksum mismatch')
        expected_generation = payload.get('output_generation')
        current_generation = _valid_generation(
            expected_generation if expected_generation is not None else 0
        )
        restore_generation = current_generation + 1
        temp = source.with_name(f'.{source.name}.replay-v10.restore.{os.getpid()}')
        try:
            with temp.open('x', encoding='utf-8') as output:
                _copy_with_generation(backup, output, restore_generation)
                output.flush()
                os.fsync(output.fileno())
            restored_metadata: dict[str, int] = {}
            transform_sidecar(temp, metadata=restored_metadata)
            if restored_metadata['generation'] != restore_generation:
                raise StreamJSONError('restored sidecar generation failed verification')
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
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('path', nargs='?', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    if bool(args.restore) == bool(args.path):
        parser.error('provide exactly one session path or --restore MANIFEST')
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
