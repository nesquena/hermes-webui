from __future__ import annotations

from inspect import iscoroutine

__all__ = ("amap", "as_completed", "gather")

from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeVar, overload

from ..abc._tasks import get_coro_name
from ._exceptions import BrokenResourceError
from ._streams import create_memory_object_stream
from ._tasks import TaskHandle, create_task_group

if TYPE_CHECKING:
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

R = TypeVar("R")
S = TypeVar("S")
T = TypeVar("T")
U = TypeVar("U")
V = TypeVar("V")
W = TypeVar("W")


@overload
async def gather(
    coro1: Coroutine[Any, Any, R], coro2: Coroutine[Any, Any, S], /
) -> tuple[R, S]: ...


@overload
async def gather(
    coro1: Coroutine[Any, Any, R],
    coro2: Coroutine[Any, Any, S],
    coro3: Coroutine[Any, Any, T],
    /,
) -> tuple[R, S, T]: ...


@overload
async def gather(
    coro1: Coroutine[Any, Any, R],
    coro2: Coroutine[Any, Any, S],
    coro3: Coroutine[Any, Any, T],
    coro4: Coroutine[Any, Any, U],
    /,
) -> tuple[R, S, T, U]: ...


@overload
async def gather(
    coro1: Coroutine[Any, Any, R],
    coro2: Coroutine[Any, Any, S],
    coro3: Coroutine[Any, Any, T],
    coro4: Coroutine[Any, Any, U],
    coro5: Coroutine[Any, Any, V],
    /,
) -> tuple[R, S, T, U, V]: ...


@overload
async def gather(
    coro1: Coroutine[Any, Any, R],
    coro2: Coroutine[Any, Any, S],
    coro3: Coroutine[Any, Any, T],
    coro4: Coroutine[Any, Any, U],
    coro5: Coroutine[Any, Any, V],
    coro6: Coroutine[Any, Any, W],
    /,
) -> tuple[R, S, T, U, V, W]: ...


# handle arbitrary length if awaitables are all of the same type
@overload
async def gather(*coros: Coroutine[Any, Any, R]) -> tuple[R, ...]: ...


async def gather(*coros: Coroutine[Any, Any, Any]) -> tuple[Any, ...]:
    """
    Run coroutines concurrently in a task group. The order of result values corresponds
    to the order of coroutines passed.

    :param coros: coroutine objects to run as tasks
    :return: task results for each argument in the same order as they were passed

    """
    handles: list[TaskHandle[Any]] = []
    async with create_task_group() as tg:
        handles.extend(tg.create_task(coro) for coro in coros)

    return tuple(h.return_value for h in handles)


@asynccontextmanager
async def as_completed(
    *awaitables: Awaitable[R],
) -> AsyncGenerator[MemoryObjectReceiveStream[TaskHandle[R]]]:
    """
    Run awaitable objects concurrently in a task group, returning an iterator which can
    be used to get finished task handles in the order they complete.

    :param awaitables: awaitable objects to run as tasks
    :return: MemoryObjectReceiveStream for iterating over task handles as tasks complete.

    """
    if not awaitables:
        raise ValueError("as_completed() takes at least one awaitable")

    send, recv = create_memory_object_stream[TaskHandle[R]](len(awaitables))
    task_handles: list[TaskHandle[R]] = []

    async def runner(
        awaitable: Awaitable[R],
        index: int,
        _send: MemoryObjectSendStream[TaskHandle[R]],
    ) -> R:
        async with _send:
            try:
                return await awaitable
            finally:
                try:
                    _send.send_nowait(task_handles[index])
                except BrokenResourceError:
                    pass

    async with recv, create_task_group() as tg:
        async with send:
            for i, awaitable in enumerate(awaitables):
                name = get_coro_name(awaitable) if iscoroutine(awaitable) else None
                task_handles.append(
                    tg.start_soon(runner, awaitable, i, send.clone(), name=name)
                )
        try:
            yield recv
        finally:
            tg.cancel_scope.cancel()


async def amap(
    func: Callable[[T], Coroutine[Any, Any, R]], args: Iterable[T]
) -> list[R]:
    """
    Run the given coroutine function concurrently for multiple argument values.

    :param func: a coroutine function that takes a single argument
    :param args: a sequence of argument values to pass to ``func``
    :return: task results for each argument in the same order as they were passed

    """
    handles: list[TaskHandle[R]] = []
    async with create_task_group() as tg:
        handles.extend(tg.start_soon(func, arg) for arg in args)

    return [h.return_value for h in handles]
