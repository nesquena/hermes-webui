from __future__ import annotations

from typing import TYPE_CHECKING

from .._lazyimport import (
    fix_package_names,
    install_lazy_importer,
    set_deprecated_aliases,
)

if TYPE_CHECKING or not install_lazy_importer():
    from ._eventloop import AsyncBackend as AsyncBackend
    from ._resources import AsyncResource as AsyncResource
    from ._sockets import ConnectedUDPSocket as ConnectedUDPSocket
    from ._sockets import ConnectedUNIXDatagramSocket as ConnectedUNIXDatagramSocket
    from ._sockets import IPAddressType as IPAddressType
    from ._sockets import IPSockAddrType as IPSockAddrType
    from ._sockets import SocketAttribute as SocketAttribute
    from ._sockets import SocketListener as SocketListener
    from ._sockets import SocketStream as SocketStream
    from ._sockets import UDPPacketType as UDPPacketType
    from ._sockets import UDPSocket as UDPSocket
    from ._sockets import UNIXDatagramPacketType as UNIXDatagramPacketType
    from ._sockets import UNIXDatagramSocket as UNIXDatagramSocket
    from ._sockets import UNIXSocketStream as UNIXSocketStream
    from ._streams import AnyByteReceiveStream as AnyByteReceiveStream
    from ._streams import AnyByteSendStream as AnyByteSendStream
    from ._streams import AnyByteStream as AnyByteStream
    from ._streams import AnyByteStreamConnectable as AnyByteStreamConnectable
    from ._streams import (
        AnyUnreliableByteReceiveStream as AnyUnreliableByteReceiveStream,
    )
    from ._streams import AnyUnreliableByteSendStream as AnyUnreliableByteSendStream
    from ._streams import AnyUnreliableByteStream as AnyUnreliableByteStream
    from ._streams import ByteReceiveStream as ByteReceiveStream
    from ._streams import ByteSendStream as ByteSendStream
    from ._streams import ByteStream as ByteStream
    from ._streams import ByteStreamConnectable as ByteStreamConnectable
    from ._streams import Listener as Listener
    from ._streams import ObjectReceiveStream as ObjectReceiveStream
    from ._streams import ObjectSendStream as ObjectSendStream
    from ._streams import ObjectStream as ObjectStream
    from ._streams import ObjectStreamConnectable as ObjectStreamConnectable
    from ._streams import UnreliableObjectReceiveStream as UnreliableObjectReceiveStream
    from ._streams import UnreliableObjectSendStream as UnreliableObjectSendStream
    from ._streams import UnreliableObjectStream as UnreliableObjectStream
    from ._subprocesses import Process as Process
    from ._tasks import TaskGroup as TaskGroup
    from ._tasks import TaskStatus as TaskStatus
    from ._testing import TestRunner as TestRunner

    fix_package_names()
    set_deprecated_aliases(
        {
            "CapacityLimiter": "anyio.CapacityLimiter",
            "Condition": "anyio.Condition",
            "Event": "anyio.Event",
            "Lock": "anyio.Lock",
            "Semaphore": "anyio.Semaphore",
            "CancelScope": "anyio.CancelScope",
            "BlockingPortal": "anyio.from_thread.BlockingPortal",
        }
    )
