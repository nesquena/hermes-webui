"""Shared profile-scoped transaction helpers for provider/model mutations."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


@dataclass(frozen=True)
class FileSnapshot:
    existed: bool
    data: bytes
    mode: int | None


def snapshot_file(path: Path) -> FileSnapshot:
    """Capture exact bytes and mode, failing closed on unreadable existing files."""
    try:
        with path.open("rb") as handle:
            file_stat = os.fstat(handle.fileno())
            data = handle.read()
    except FileNotFoundError:
        return FileSnapshot(False, b"", None)
    return FileSnapshot(True, data, stat.S_IMODE(file_stat.st_mode))


def _atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        if mode is not None:
            try:
                path.chmod(mode)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def restore_file(path: Path, snapshot: FileSnapshot) -> None:
    """Restore one exact snapshot, removing files created by the failed action."""
    if not snapshot.existed:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write_bytes(path, snapshot.data, snapshot.mode)


def restore_file_if_unchanged(
    path: Path, published: FileSnapshot, original: FileSnapshot
) -> bool:
    """Compare and restore atomically against every in-process .env writer."""
    from api.streaming import _ENV_LOCK

    with _ENV_LOCK:
        if snapshot_file(path) != published:
            return False
        restore_file(path, original)
        return True


def _parse_yaml_mapping_strict(text: str) -> dict:
    """Parse one YAML mapping while rejecting ambiguous duplicate keys."""
    try:
        import yaml
        from yaml.constructor import ConstructorError
    except ImportError as exc:
        raise ValueError("PyYAML is required to read config.yaml safely.") from exc

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_unique_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_unique_mapping,
    )
    loaded = yaml.load(text, Loader=UniqueKeyLoader)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(
            "Existing config.yaml must contain a mapping; no changes were applied."
        )
    return loaded


def load_yaml_mapping_strict(path: Path) -> dict:
    """Load raw YAML without env expansion; malformed existing files fail closed."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return _parse_yaml_mapping_strict(text)
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Existing config.yaml must"):
            raise
        raise ValueError(
            "Existing config.yaml could not be read safely; no changes were applied."
        ) from exc


def assert_profile_home(expected: Path, resolver: Callable[[], Path | str]) -> None:
    current = Path(resolver()).expanduser().resolve()
    if current != expected:
        raise RuntimeError("Active profile changed during the mutation.")


@contextmanager
def _test_profile_file_lock(home: Path):
    """Cross-process fallback enabled only by the isolated pytest server."""
    lock_root = Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"]) / "profile-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:24]
    lock_path = lock_root / f"{digest}.lock"
    with lock_path.open("a+b") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


@contextmanager
def active_profile_transaction(
    home_resolver: Callable[[], Path | str] | None = None,
) -> Iterator[Path]:
    """Acquire the Agent-authoritative Profile lock for one captured home."""
    if home_resolver is None:
        from api.profiles import get_active_hermes_home

        home_resolver = get_active_hermes_home

    try:
        import hermes_constants
    except Exception as exc:
        raise RuntimeError("Hermes Agent shared Profile lock is unavailable.") from exc

    home = Path(home_resolver()).expanduser().resolve()
    multi_lock = getattr(hermes_constants, "profile_mutation_locks", None)
    single_lock = getattr(hermes_constants, "profile_mutation_lock", None)
    if callable(multi_lock):
        lock_scope = multi_lock((str(home),))
    elif callable(single_lock):
        lock_scope = single_lock(str(home))
    elif os.environ.get("HERMES_WEBUI_TEST_STATE_DIR"):
        lock_scope = _test_profile_file_lock(home)
    else:
        raise RuntimeError("Hermes Agent shared Profile lock is unavailable.")

    with lock_scope:
        current = Path(home_resolver()).expanduser().resolve()
        if current != home:
            raise RuntimeError("Active profile changed before the mutation could start.")
        yield home
