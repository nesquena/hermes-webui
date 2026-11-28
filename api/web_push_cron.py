"""Read-only, cross-process cron output observer. No SSE client is required.

Observe only explicitly enrolled canonical profile homes, never live process env
or cron.jobs globals. Inspect metadata only: prompts/results never enter push.
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import os
import stat
import threading
import time
from pathlib import Path

from api import web_push

logger = logging.getLogger(__name__)
_BATCH = 128  # directory entries examined per profile/tick; no unbounded glob/sort
_RECENT = 1024  # output identities per profile; retire oldest timestamps at capacity
_INTERVAL = 2.0
_THREAD = None
_LOCK = threading.Lock()
_STOP = threading.Event()


def _open_directory(path, *, dir_fd=None):
    # Verify the held inode as well as O_NOFOLLOW. Some emulated filesystems
    # ignore that flag; a symlink or a check/open substitution must still fail.
    before = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("not a directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise OSError("directory changed during open")
    return fd


def _outputs(home: str):
    """Bounded-memory walk with held directory descriptors and no symlink hops.

    None entries also count towards a tick's budget. No file content is read.
    Unsupported no-follow directory access fails closed.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        return
    fds = []
    try:
        for part in (*Path(home).parts, "cron", "output"):
            fds.append(_open_directory(part, dir_fd=fds[-1] if fds else None))
        with os.scandir(fds[-1]) as jobs:
            for job in jobs:
                yield None
                if not job.is_dir(follow_symlinks=False):
                    continue
                try:
                    fd = _open_directory(job.name, dir_fd=fds[-1])
                except OSError:
                    continue
                try:
                    with os.scandir(fd) as runs:
                        for run in runs:
                            try:
                                info = run.stat(follow_symlinks=False)
                                yield (job.name + "/" + run.name, info.st_mtime_ns) if (
                                    run.name.endswith(".md") and stat.S_ISREG(info.st_mode)
                                ) else None
                            except OSError:
                                yield None
                finally:
                    os.close(fd)
    finally:
        for fd in reversed(fds):
            os.close(fd)


class CronObserver:
    def __init__(self, *, started_ns=None):
        self.started_ns = time.time_ns() if started_ns is None else started_ns
        self.cursors = {}  # pinned home -> {floor, scan, seen}; no live profile globals

    def close(self):
        for cursor in self.cursors.values():
            scan = cursor["scan"]
            if scan is not None:
                scan.close()
        self.cursors.clear()

    def poll(self, *, now_ns=None):
        if not web_push.is_enabled():
            self.close()
            return
        routes = web_push.cron_routes()  # corrupt/unreadable store fails closed
        for home in list(self.cursors):
            if home not in routes:
                cursor = self.cursors.pop(home)
                if cursor["scan"] is not None:
                    cursor["scan"].close()
        now = time.time_ns() if now_ns is None else now_ns
        for home, targets in routes.items():
            # Enrollment time suppresses pre-consent history; process start
            # suppresses replay after a restart. Never infer a session owner.
            floor = max(self.started_ns, min(since for _, since in targets))
            cursor = self.cursors.setdefault(home, {"floor": floor, "scan": None, "seen": {}})
            cursor["floor"] = max(cursor["floor"], floor)
            if cursor["scan"] is None:
                cursor["scan"] = _outputs(home)
            try:
                for _ in range(_BATCH):
                    event = next(cursor["scan"])
                    if event is None:
                        continue
                    identity, stamp = event
                    if not cursor["floor"] < stamp <= now or cursor["seen"].get(identity) == stamp:
                        continue
                    owners = sorted({owner for owner, since in targets if since <= stamp})
                    if not owners:
                        continue
                    payload = web_push.notification_payload(
                        "Cron output ready", "A scheduled task has new output. Open Tasks to review.", owners=owners
                    )
                    payload["options"]["tag"] = "hermes-cron-" + hashlib.sha256(
                        (home + "/" + identity).encode()
                    ).hexdigest()[:24]
                    payload.update(_cron_home=home, _cron_stamp=stamp)
                    web_push.enqueue(payload)  # full queue drops; never retries/backlogs
                    cursor["seen"][identity] = stamp
                    if len(cursor["seen"]) > _RECENT:
                        oldest = min(cursor["seen"], key=cursor["seen"].get)
                        cursor["floor"] = max(cursor["floor"], cursor["seen"].pop(oldest))
            except StopIteration:
                # Never advance to wall time: an atomic rename can publish a
                # file whose mtime predates the last directory scan.
                cursor["scan"] = None
            except OSError:
                cursor["scan"].close()
                cursor["scan"] = None  # retry next tick; seen suppresses duplicates


def _run():
    observer = CronObserver()
    try:
        while not _STOP.is_set():
            try:
                observer.poll()
            except Exception:
                logger.debug("Cron Web Push observer poll failed", exc_info=True)
            _STOP.wait(_INTERVAL)
    finally:
        observer.close()


def start():
    """One observer per enabled WebUI process; absent dependencies start nothing."""
    global _THREAD
    if not web_push.is_enabled():
        return
    with _LOCK:
        if _THREAD is not None or _STOP.is_set():
            return
        _THREAD = threading.Thread(target=_run, name="web-push-cron", daemon=True)
        _THREAD.start()
        atexit.register(stop)


def stop():
    _STOP.set()
    thread = _THREAD
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
