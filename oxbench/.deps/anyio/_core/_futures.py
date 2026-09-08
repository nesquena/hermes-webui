from __future__ import annotations

from collections.abc import Generator
from enum import Enum, auto
from typing import Any, Generic, TypeVar

from ._exceptions import (
    FutureAlreadyFinished,
    FutureCancelled,
    FutureFailed,
    FutureNotFinished,
)
from ._synchronization import Event

T = TypeVar("T")


class Future(Generic[T]):
    """
    An awaitable object that works similar to a :class:`asyncio.Future` but
    with similar characteristics to a :class:`.TaskHandle`.
    """

    class Status(Enum):
        """
        The status of a future handle.

        .. attribute:: PENDING

            The future has not finished yet.
        .. attribute:: FINISHED

            The future has finished with a return value.
        .. attribute:: CANCELLED

            The future was cancelled and has finished since.
        .. attribute:: FAILED

            The future raised an exception.
        """

        PENDING = auto()
        FINISHED = auto()
        CANCELLED = auto()
        FAILED = auto()

    __slots__ = (
        "_cancelled",
        "_exception",
        "_finished_event",
        "_name",
        "_return_value",
    )
    _return_value: T

    def __init__(self, *, name: str | None = None) -> None:
        self._finished_event = Event()
        self._exception: BaseException | None = None
        self._cancelled: bool = False
        self._name = name

    def _check_pending(self) -> None:
        """Shortcut for checking if a Future is pending

        :raises FutureAlreadyFinished: if future was already given a result or exception.
        :raises FutureCancelled: if future has been cancelled previously.
        """
        match self.status:
            case Future.Status.PENDING:
                return
            case Future.Status.FINISHED:
                raise FutureAlreadyFinished("future has already finished")
            case Future.Status.FAILED:
                raise FutureAlreadyFinished("future already failed")
            case Future.Status.CANCELLED:
                raise FutureCancelled("future was cancelled")

    async def wait(self) -> None:
        """
        Waits for the future to finish.

        This method will attempt to wait for a result or exception
        """
        await self._finished_event.wait()

    def cancel(self) -> None:
        """Cancels a pending `.Future` object

        Does nothing if the Future was already finished.
        """
        if self.status is Future.Status.PENDING:
            self._cancelled = True
            self._finished_event.set()

    @property
    def exception(self) -> BaseException | None:
        """
        The exception value of a `.Future`

        :raises FutureNotFinished: if future is still pending
        :raises FutureCancelled: if future was cancelled
        :returns: None if future succeeds with a result sent instead
            otherwise this will be an exception
        """

        match self.status:
            case Future.Status.PENDING:
                raise FutureNotFinished("the future has not finished yet")
            case Future.Status.FINISHED:
                return None
            case Future.Status.CANCELLED:
                raise FutureCancelled("the future was cancelled")
            case Future.Status.FAILED:
                return self._exception

    @exception.setter
    def exception(self, exception: BaseException) -> None:
        """Send exception for a `.Future` object

        :raises FutureAlreadyFinished: if future was already given a result or exception.
        :raises FutureCancelled: if future has been cancelled previously.
        """
        self._check_pending()
        self._exception = exception
        self._finished_event.set()

    @property
    def return_value(self) -> T:
        """
        The return value of the future.

        :raises FutureNotFinished: if the future has not finished yet
        :raises FutureCancelled: if the future was cancelled
        :raises FutureFailed: if the future raised an exception

        """
        match self.status:
            case Future.Status.PENDING:
                raise FutureNotFinished("the future has not finished yet")
            case Future.Status.FINISHED:
                return self._return_value
            case Future.Status.CANCELLED:
                raise FutureCancelled("the future was cancelled")
            case Future.Status.FAILED:
                raise FutureFailed(
                    "the future raised an exception"
                ) from self._exception

    @return_value.setter
    def return_value(self, value: T) -> None:
        """
        Send pending result for a `.Future` object

        :raises FutureAlreadyFinished: if future was already given a result or exception.
        :raises FutureCancelled: if future has been cancelled previously.
        """
        self._check_pending()
        self._return_value = value
        self._finished_event.set()

    @property
    def status(self) -> Future.Status:
        """
        The current status of a future.

        Every future starts in the :attr:`~Future.Status.PENDING` state.
        If a future is cancelled while in this state, it will transition to the
        :attr:`Future.Status.CANCELLED` state. When the task finishes, it will
        transition to one of the three final states (
        :attr:`Future.Status.FINISHED`, :attr:`Future.Status.FAILED`, or
        :attr:`Future.Status.CANCELLED`) depending on the exception the task
        raised, if any. No other status transitions will happen.
        """
        if not self._finished_event.is_set():
            return Future.Status.PENDING
        elif self._cancelled:
            return Future.Status.CANCELLED
        elif self._exception is not None:
            return Future.Status.FAILED
        else:
            return Future.Status.FINISHED

    def __await__(self) -> Generator[Any, Any, T]:
        yield from self.wait().__await__()
        return self.return_value

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} {self.status.name.lower()} "
            f"name={self._name!r}>"
        )
