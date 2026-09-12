"""Process isolation for profile-pinned cron execution."""

import logging
import json
import multiprocessing
import pickle
import queue
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CRON_SUBPROCESS_POLL_SECONDS = 0.25
_CRON_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0


def _serialize_child_request(job, args, kwargs):
    """Encode only JSON-shaped cron data for the spawned child."""
    try:
        return (
            json.dumps(job, separators=(",", ":"), ensure_ascii=False),
            json.dumps(tuple(args), separators=(",", ":"), ensure_ascii=False),
            json.dumps(kwargs, separators=(",", ":"), ensure_ascii=False),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(
            "cron subprocess request contains non-serializable job/context data"
        ) from exc


def _child_kwargs_for_operation(operation, kwargs):
    """Reject process-local scheduler handles at the child boundary."""
    child_kwargs = dict(kwargs)
    if operation == "run_one_job":
        for name in ("adapters", "loop"):
            if child_kwargs.get(name) is not None:
                raise RuntimeError(
                    f"run_one_job child request cannot carry live {name}"
                )
    return child_kwargs


def _cron_job_subprocess_main(job_json, profile_home, operation, args_json, kwargs_json, result_queue):
    try:
        job = json.loads(job_json)
        args = tuple(json.loads(args_json))
        kwargs = json.loads(kwargs_json)
        from api.profiles import _cron_child_execution

        child_token = _cron_child_execution.set(True)
        if profile_home is None:
            _run_in_profile = None
        else:
            from api.profiles import cron_profile_context_for_home

            _run_in_profile = cron_profile_context_for_home(profile_home)

        try:
            if _run_in_profile is None:
                result = _invoke_cron_operation(job, operation, args, kwargs)
            else:
                with _run_in_profile:
                    result = _invoke_cron_operation(job, operation, args, kwargs)
        finally:
            _cron_child_execution.reset(child_token)
        try:
            result_payload = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            result_queue.put(("error", f"cron subprocess result was not serializable: {exc}", ""))
        else:
            result_queue.put(("ok", result_payload))
    except BaseException as exc:  # pragma: no cover - surfaced in parent
        import traceback

        result_queue.put(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))


def _invoke_cron_operation(job, operation, args, kwargs):
    if operation not in {"run_job", "run_one_job"}:
        raise ValueError(f"Unsupported cron operation: {operation}")
    import cron.scheduler as scheduler

    callable_ = getattr(scheduler, operation, None)
    if callable_ is None:
        raise RuntimeError(f"cron.scheduler.{operation} is unavailable")
    if getattr(callable_, "_webui_profile_isolated", False):
        callable_ = getattr(callable_, "_webui_original_" + operation, callable_)
    return callable_(job, *args, **kwargs)


def _cron_subprocess_result_timeout_seconds(job):
    for key in ("timeout_seconds", "max_runtime_seconds", "timeout"):
        raw = (job or {}).get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return max(60.0, value + 30.0)
    return 6 * 60 * 60.0


def run_cron_in_profile_subprocess(
    job, profile_home, operation, *, args=(), kwargs=None, cancel_event=None
):
    """Run one supported cron operation in a profile-pinned spawned child.

    ``cancel_event`` stays in the parent because it is a live control object.
    A set event terminates the child; Agent then recovers the abandoned claim
    through its normal expiry and takeover path.
    """
    if operation not in {"run_job", "run_one_job"}:
        raise ValueError(f"Unsupported cron operation: {operation}")
    args = tuple(args)
    kwargs = _child_kwargs_for_operation(operation, kwargs or {})
    job_json, args_json, kwargs_json = _serialize_child_request(job, args, kwargs)
    profile_home = None if profile_home is None else str(Path(profile_home))

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_cron_job_subprocess_main,
        args=(job_json, profile_home, operation, args_json, kwargs_json, result_queue),
    )
    result_timeout = _cron_subprocess_result_timeout_seconds(job)
    status = "error"
    payload = ["cron run subprocess failed before producing a result", ""]
    try:
        try:
            process.start()
        except BaseException as exc:
            raise RuntimeError(f"cron run subprocess failed to start: {exc}") from exc
        deadline = time.monotonic() + result_timeout
        try:
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    raise queue.Empty
                try:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError(
                            "cron run subprocess cancelled and was terminated"
                        )
                    status, *payload = result_queue.get(
                        timeout=min(_CRON_SUBPROCESS_POLL_SECONDS, remaining)
                    )
                    break
                except queue.Empty:
                    if not process.is_alive():
                        try:
                            status, *payload = result_queue.get(timeout=0)
                        except queue.Empty:
                            payload = [
                                f"cron run subprocess exited with code {process.exitcode} without producing a result",
                                "",
                            ]
                        break
        except RuntimeError as exc:
            if process.is_alive():
                process.terminate()
                process.join(timeout=_CRON_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS)
            payload = [str(exc), ""]
            status = "error"
        except queue.Empty:
            if process.is_alive():
                process.terminate()
                process.join(timeout=_CRON_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS)
                payload = [
                    f"cron run subprocess produced no result within {result_timeout:g}s and was terminated",
                    "",
                ]
            else:
                payload = [
                    f"cron run subprocess exited with code {process.exitcode} without producing a result",
                    "",
                ]
        finally:
            process.join(timeout=_CRON_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(timeout=_CRON_SUBPROCESS_CLEANUP_TIMEOUT_SECONDS)
                if process.is_alive():
                    status = "error"
                    payload = [
                        "cron run subprocess did not terminate within the cleanup bound",
                        "",
                    ]
                elif status == "ok":
                    status = "error"
                    payload = ["cron run subprocess did not exit after returning a result", ""]
    finally:
        result_queue.close()
        result_queue.join_thread()

    if status == "ok":
        try:
            return pickle.loads(payload[0])
        except Exception as exc:
            raise RuntimeError("cron subprocess returned an invalid result payload") from exc
    message = payload[0]
    traceback_text = payload[1] if len(payload) > 1 else ""
    if traceback_text:
        logger.error("Cron subprocess failed:\n%s", traceback_text)
    raise RuntimeError(message)
