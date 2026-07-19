"""Shared helpers for WebUI completion/delegation delivery."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Older Hermes Agent builds do not expose durable claim/complete/release APIs.
# Keep their in-process compatibility dedupe bounded so long-lived WebUI
# processes cannot retain every delegation id forever.
LEGACY_ASYNC_DELIVERY_DEDUPE_MAX = 1024
ASYNC_DELIVERY_CLAIM_RETRY_SECONDS = 301.0
ASYNC_DELIVERY_ROUTING_RETRY_SECONDS = 5.0
_LEGACY_ASYNC_DELIVERY_LOCK = threading.Lock()
_LEGACY_ASYNC_DELIVERY_IDS: OrderedDict[str, None] = OrderedDict()
_ASYNC_DELIVERY_RETRY_LOCK = threading.Lock()
_ASYNC_DELIVERY_RETRY_TIMER: threading.Timer | None = None
_ASYNC_DELIVERY_RETRY_DEADLINE = 0.0
_ASYNC_DELIVERY_RETRY_QUEUE: Any = None
_ASYNC_DELIVERY_RETRY_GENERATION = 0


@dataclass(frozen=True)
class AsyncDelegationDeliveryClaim:
    """Opaque ownership token for one WebUI async-delegation consumer."""

    delegation_id: str
    claim_id: str
    durable: bool


def completion_delivery_id(evt: Any) -> str:
    """Return the stable WebUI delivery/dedupe id for a completion event.

    Terminal background-process events use ``session_id`` for the process id.
    Async ``delegate_task`` completions carry ``delegation_id`` instead, so both
    WebUI delivery paths must key those events by ``delegation_id``.
    """
    if not isinstance(evt, dict):
        return ""
    if evt.get("type") == "async_delegation":
        return str(
            evt.get("delegation_id")
            or evt.get("session_id")
            or evt.get("task_id")
            or ""
        ).strip()
    return str(evt.get("session_id") or "").strip()


def _claim_bounded_local(delegation_id: str) -> bool:
    with _LEGACY_ASYNC_DELIVERY_LOCK:
        if delegation_id in _LEGACY_ASYNC_DELIVERY_IDS:
            return False
        _LEGACY_ASYNC_DELIVERY_IDS[delegation_id] = None
        while len(_LEGACY_ASYNC_DELIVERY_IDS) > LEGACY_ASYNC_DELIVERY_DEDUPE_MAX:
            _LEGACY_ASYNC_DELIVERY_IDS.popitem(last=False)
        return True


def _release_bounded_local(delegation_id: str) -> None:
    with _LEGACY_ASYNC_DELIVERY_LOCK:
        _LEGACY_ASYNC_DELIVERY_IDS.pop(delegation_id, None)


def _arm_async_delegation_restore_sweep(completion_queue: Any, delay: float) -> bool:
    """Arm one process-wide durable restore sweep at the earliest deadline.

    The durable database is the backlog. Keeping one shared timer avoids both
    one-thread-per-event growth and lossy eviction of individual retry entries.
    The sweep restores every still-pending record; atomic claims suppress races
    and delivered rows are excluded by the core query.
    """
    global _ASYNC_DELIVERY_RETRY_TIMER
    global _ASYNC_DELIVERY_RETRY_DEADLINE
    global _ASYNC_DELIVERY_RETRY_QUEUE
    global _ASYNC_DELIVERY_RETRY_GENERATION

    if completion_queue is None:
        return False
    retry_delay = max(0.0, float(delay))
    deadline = time.monotonic() + retry_delay

    with _ASYNC_DELIVERY_RETRY_LOCK:
        if (
            _ASYNC_DELIVERY_RETRY_TIMER is not None
            and deadline >= _ASYNC_DELIVERY_RETRY_DEADLINE
        ):
            return True
        previous = _ASYNC_DELIVERY_RETRY_TIMER
        if previous is not None:
            previous.cancel()
        _ASYNC_DELIVERY_RETRY_GENERATION += 1
        generation = _ASYNC_DELIVERY_RETRY_GENERATION
        _ASYNC_DELIVERY_RETRY_DEADLINE = deadline
        _ASYNC_DELIVERY_RETRY_QUEUE = completion_queue

        def _restore() -> None:
            global _ASYNC_DELIVERY_RETRY_TIMER
            global _ASYNC_DELIVERY_RETRY_DEADLINE
            global _ASYNC_DELIVERY_RETRY_QUEUE

            with _ASYNC_DELIVERY_RETRY_LOCK:
                if generation != _ASYNC_DELIVERY_RETRY_GENERATION:
                    return
                target_queue = _ASYNC_DELIVERY_RETRY_QUEUE
                _ASYNC_DELIVERY_RETRY_TIMER = None
                _ASYNC_DELIVERY_RETRY_DEADLINE = 0.0
                _ASYNC_DELIVERY_RETRY_QUEUE = None
            try:
                from tools.async_delegation import restore_undelivered_completions

                restore_undelivered_completions(target_queue)
            except Exception:
                logger.warning(
                    "Failed to restore pending async delegations; retrying sweep",
                    exc_info=True,
                )
                _arm_async_delegation_restore_sweep(
                    target_queue,
                    ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
                )

        timer = threading.Timer(retry_delay, _restore)
        timer.daemon = True
        _ASYNC_DELIVERY_RETRY_TIMER = timer
        timer.start()
    return True


def schedule_async_delegation_claim_retry(
    evt: Any,
    completion_queue: Any,
    *,
    delay: float | None = None,
) -> bool:
    """Schedule a durable restore sweep after a claim or routing lease delay."""
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation":
        return False
    delegation_id = str(evt.get("delegation_id") or "").strip()
    if not delegation_id or completion_queue is None:
        return False
    try:
        from tools.async_delegation import get_durable_delegation

        durable = get_durable_delegation(delegation_id)
    except (ImportError, AttributeError):
        return False
    except Exception:
        logger.debug(
            "Failed to inspect durable async delegation %s for retry",
            delegation_id,
            exc_info=True,
        )
        return False
    if not isinstance(durable, dict) or durable.get("delivery_state") != "pending":
        return False

    retry_delay = (
        ASYNC_DELIVERY_CLAIM_RETRY_SECONDS if delay is None else max(0.0, float(delay))
    )
    return _arm_async_delegation_restore_sweep(completion_queue, retry_delay)


def requeue_async_delegation_event(
    evt: Any,
    completion_queue: Any,
    *,
    delay: float = 0.0,
    stop_event: threading.Event | None = None,
    durable: bool | None = None,
) -> bool:
    """Requeue an async event, falling back to the durable restore sweep.

    Callers pass the queue reference they already resolved so an import failure
    cannot strand a released durable claim. Legacy events without a durable row
    still get one best-effort direct requeue; durable events additionally arm a
    restore sweep when the direct queue write fails.
    """
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation":
        return False
    if completion_queue is None:
        return False
    retry_delay = max(0.0, float(delay))
    if retry_delay:
        if stop_event is not None:
            if stop_event.wait(retry_delay):
                return False
        else:
            time.sleep(retry_delay)
    try:
        completion_queue.put(dict(evt))
        return True
    except Exception:
        logger.warning("Failed to requeue async delegation event", exc_info=True)
        if durable is True:
            return _arm_async_delegation_restore_sweep(
                completion_queue,
                ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
            )
        return schedule_async_delegation_claim_retry(
            evt,
            completion_queue,
            delay=ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
        )


def _cancel_async_delegation_claim_retry(_delegation_id: str) -> None:
    # Retry is a shared durable-store sweep, not a per-delegation timer. It must
    # remain armed because other pending records may rely on the same sweep.
    return None


def async_delivery_retry_timer_count() -> int:
    with _ASYNC_DELIVERY_RETRY_LOCK:
        return 1 if _ASYNC_DELIVERY_RETRY_TIMER is not None else 0


def claim_async_delegation_delivery(
    evt: Any,
    consumer: str,
) -> AsyncDelegationDeliveryClaim | None:
    """Atomically claim an async completion for one WebUI delivery path.

    Current Hermes Agent builds provide a durable SQLite-backed claim contract.
    Older builds fall back to the bounded process-local claim above. The local
    claim also serializes duplicate legacy events on current cores where no
    durable row exists yet.
    """
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation":
        return None
    delegation_id = completion_delivery_id(evt)
    if not delegation_id or not _claim_bounded_local(delegation_id):
        return None

    try:
        from tools.async_delegation import (
            claim_event_delivery,
            complete_event_delivery,  # noqa: F401 - capability contract probe
            release_event_delivery,  # noqa: F401 - capability contract probe
        )
    except (ImportError, AttributeError):
        return AsyncDelegationDeliveryClaim(
            delegation_id=delegation_id,
            claim_id="",
            durable=False,
        )

    try:
        claim_id = claim_event_delivery(evt, str(consumer or "webui"))
    except Exception:
        _release_bounded_local(delegation_id)
        logger.warning(
            "Failed to claim durable async delegation delivery for %s",
            delegation_id,
            exc_info=True,
        )
        raise
    if claim_id is None:
        _release_bounded_local(delegation_id)
        return None
    return AsyncDelegationDeliveryClaim(
        delegation_id=delegation_id,
        claim_id=str(claim_id or ""),
        durable=True,
    )


def _mark_legacy_async_delivery_complete(delegation_id: str) -> bool:
    """Acknowledge completion through progressively older core APIs."""
    try:
        from tools import async_delegation as async_delivery
    except Exception:
        return False

    marker = getattr(async_delivery, "mark_completion_delivered", None)
    if callable(marker):
        try:
            if marker(delegation_id) is not False:
                return True
        except Exception:
            logger.debug(
                "mark_completion_delivered failed for %s; trying legacy marker",
                delegation_id,
                exc_info=True,
            )

    legacy_marker = getattr(async_delivery, "mark_async_delegation_consumed", None)
    if callable(legacy_marker):
        try:
            legacy_marker(delegation_id)
            return True
        except Exception:
            logger.debug(
                "Legacy async delegation marker failed for %s",
                delegation_id,
                exc_info=True,
            )
    return False


def complete_async_delegation_delivery(
    evt: Any,
    claim: AsyncDelegationDeliveryClaim,
) -> None:
    """Complete a claim after WebUI has accepted the event for delivery."""
    if claim.durable:
        from tools.async_delegation import complete_event_delivery

        try:
            complete_event_delivery(evt, claim.claim_id)
            _cancel_async_delegation_claim_retry(claim.delegation_id)
            return
        except Exception:
            logger.warning(
                "Durable async delegation completion ACK failed for %s; "
                "trying compatibility marker",
                claim.delegation_id,
                exc_info=True,
            )
            if _mark_legacy_async_delivery_complete(claim.delegation_id):
                _cancel_async_delegation_claim_retry(claim.delegation_id)
                return
            raise
    _mark_legacy_async_delivery_complete(claim.delegation_id)


def release_async_delegation_delivery(
    evt: Any,
    claim: AsyncDelegationDeliveryClaim,
) -> None:
    """Release a failed claim so a later WebUI consumer can retry it."""
    try:
        if claim.durable:
            from tools.async_delegation import release_event_delivery

            release_event_delivery(evt, claim.claim_id)
    except Exception:
        logger.warning(
            "Failed to release durable async delegation delivery for %s",
            claim.delegation_id,
            exc_info=True,
        )
    finally:
        _release_bounded_local(claim.delegation_id)


def legacy_async_delivery_dedupe_size() -> int:
    """Return bounded compatibility-dedupe size for regression coverage."""
    with _LEGACY_ASYNC_DELIVERY_LOCK:
        return len(_LEGACY_ASYNC_DELIVERY_IDS)


def _reset_legacy_async_delivery_dedupe_for_tests() -> None:
    global _ASYNC_DELIVERY_RETRY_TIMER
    global _ASYNC_DELIVERY_RETRY_DEADLINE
    global _ASYNC_DELIVERY_RETRY_QUEUE
    global _ASYNC_DELIVERY_RETRY_GENERATION

    with _LEGACY_ASYNC_DELIVERY_LOCK:
        _LEGACY_ASYNC_DELIVERY_IDS.clear()
    with _ASYNC_DELIVERY_RETRY_LOCK:
        timer = _ASYNC_DELIVERY_RETRY_TIMER
        _ASYNC_DELIVERY_RETRY_TIMER = None
        _ASYNC_DELIVERY_RETRY_DEADLINE = 0.0
        _ASYNC_DELIVERY_RETRY_QUEUE = None
        _ASYNC_DELIVERY_RETRY_GENERATION += 1
    if timer is not None:
        timer.cancel()
