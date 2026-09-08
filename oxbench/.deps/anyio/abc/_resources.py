from __future__ import annotations

import sys
from abc import ABCMeta, abstractmethod
from types import TracebackType
from typing import TypeVar

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

T = TypeVar("T")


class AsyncResource(metaclass=ABCMeta):
    """
    Abstract base class for all closeable asynchronous resources.

    Works as an asynchronous context manager which returns the instance itself on enter,
    and calls :meth:`aclose` on exit.
    """

    __slots__ = ()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @abstractmethod
    async def aclose(self) -> None:
        """Close the resource."""
