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
_INTERVAL = 2.0
_THREAD = None
_LOCK = threading.Lock()
_STOP = threading.Event()


def _outputs(home: str):
    """Bounded-memory walk with held directory descriptors and no symlink hops.

    None entries also count towards a tick's budget. No file content is read.
    Unsupported no-follow directory access fails closed.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = []
    try:
        for part in (home, "cron", "output"):
            fds.append(os.open(part, flags, **({"dir_fd": fds[-1]} if fds else {})))
        with os.scandir(fds[-1]) as jobs:
            for job in jobs:
                yield None
                if not job.is_dir(follow_symlinks=False):
                    continue
                try:
                    fd = os.open(job.name, flags, dir_fd=fds[-1])
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
        self.cursors = {}  # pinned home -> [watermark, sweep upper bound, iterator]

    def close(self):
        for _, _, scan in self.cursors.values():
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
                if cursor[2] is not None:
                    cursor[2].close()
        now = time.time_ns() if now_ns is None else now_ns
        for home, targets in routes.items():
            # Enrollment time suppresses pre-consent history; process start
            # suppresses replay after a restart. Never infer a session owner.
            floor = max(self.started_ns, min(since for _, since in targets))
            cursor = self.cursors.setdefault(home, [floor, floor, None])
            cursor[0] = max(cursor[0], floor)
            if cursor[2] is None:
                cursor[1], cursor[2] = now, _outputs(home)
            try:
                for _ in range(_BATCH):
                    event = next(cursor[2])
                    if event is None:
                        continue
                    identity, stamp = event
                    if not cursor[0] < stamp <= cursor[1]:
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
            except StopIteration:
                cursor[0], cursor[2] = cursor[1], None
            except OSError:
                cursor[2].close()
                cursor[2] = None  # retry next tick without advancing the watermark


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
    """One dormant observer per WebUI process; absent dependencies do no IO."""
    global _THREAD
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
