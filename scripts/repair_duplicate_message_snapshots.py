#!/usr/bin/env python3
"""Stream-repair duplicate stable-ID message snapshots in a session sidecar."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


class JSONReader:
    def __init__(self, handle, chunk_size=1024 * 1024):
        self.handle = handle
        self.chunk_size = chunk_size
        self.buffer = b""
        self.eof = False

    def _fill(self):
        if self.eof:
            return False
        chunk = self.handle.read(self.chunk_size)
        if chunk:
            self.buffer += chunk
            return True
        self.eof = True
        return False

    def skip_ws(self):
        while True:
            stripped = self.buffer.lstrip(b" \t\r\n")
            consumed = len(self.buffer) - len(stripped)
            if consumed:
                self.buffer = stripped
            if self.buffer or self.eof:
                return
            self._fill()

    def consume(self, count):
        self.buffer = self.buffer[count:]

    def value(self):
        decoder = json.JSONDecoder()
        while True:
            self.skip_ws()
            try:
                text = self.buffer.decode("utf-8")
                value, end = decoder.raw_decode(text)
                consumed = len(text[:end].encode("utf-8"))
                self.consume(consumed)
                return value
            except (json.JSONDecodeError, UnicodeDecodeError):
                if not self._fill():
                    raise ValueError("truncated JSON value") from None

    def byte(self):
        self.skip_ws()
        if not self.buffer and not self.eof:
            self._fill()
            self.skip_ws()
        return self.buffer[:1]


def write_json(handle, value):
    handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def repair(source: Path, destination: Path):
    source = source.resolve()
    destination = destination.resolve()
    try:
        same_file = destination.exists() and source.samefile(destination)
    except OSError:
        same_file = False
    if source == destination or same_file:
        raise ValueError("source and destination must be different files")

    seen_ids = set()
    input_messages = 0
    output_messages = 0
    duplicate_messages = 0
    first_field = True
    message_count_seen = False
    with source.open("rb") as source_handle, destination.open("w", encoding="utf-8") as out:
        reader = JSONReader(source_handle)
        reader.skip_ws()
        if reader.byte() != b"{":
            raise ValueError("sidecar root is not an object")
        reader.consume(1)
        out.write("{")
        while True:
            reader.skip_ws()
            if reader.byte() == b",":
                reader.consume(1)
                continue
            if reader.byte() == b"}":
                reader.consume(1)
                if message_count_seen:
                    if not first_field:
                        out.write(",")
                    out.write('"message_count":')
                    out.write(str(output_messages))
                out.write("}")
                break
            key = reader.value()
            if not isinstance(key, str):
                raise ValueError("object key is not a string")
            reader.skip_ws()
            if reader.byte() != b":":
                raise ValueError("missing object colon")
            reader.consume(1)
            if key == "message_count":
                reader.value()
                message_count_seen = True
                continue
            if not first_field:
                out.write(",")
            out.write(json.dumps(key, ensure_ascii=False, separators=(",", ":")))
            out.write(":")
            first_field = False
            if key != "messages":
                write_json(out, reader.value())
                continue

            if reader.byte() != b"[":
                raise ValueError("messages is not an array")
            reader.consume(1)
            out.write("[")
            first_message = True
            while True:
                reader.skip_ws()
                if reader.byte() == b",":
                    reader.consume(1)
                    continue
                if reader.byte() == b"]":
                    reader.consume(1)
                    out.write("]")
                    break
                message = reader.value()
                input_messages += 1
                message_id = message.get("id") if isinstance(message, dict) else None
                duplicate = (
                    message_id is not None
                    and not isinstance(message_id, bool)
                    and message_id in seen_ids
                )
                if message_id is not None and not isinstance(message_id, bool):
                    seen_ids.add(message_id)
                if duplicate:
                    duplicate_messages += 1
                    continue
                if not first_message:
                    out.write(",")
                write_json(out, message)
                first_message = False
                output_messages += 1
    return {
        "input_messages": input_messages,
        "output_messages": output_messages,
        "duplicate_messages_removed": duplicate_messages,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Write a repaired sidecar to a separate destination; replace the source only after validation and backup."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = repair(args.source, args.destination)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
