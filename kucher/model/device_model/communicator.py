#
# Copyright (C) 2018 Zubax Robotics OU
#
# This file is part of Kucher.
# Kucher is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
# Kucher is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with Kucher.
# If not, see <http://www.gnu.org/licenses/>.
#
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import time
import popcop
import typing
import asyncio
import threading
from logging import getLogger
from .messages import MessageType, Message, Codec
from popcop.standard import MessageBase as StandardMessageBase
from popcop.transport import ReceivedFrame

__all__ = ['Communicator', 'CommunicatorException', 'LOOPBACK_PORT_NAME']

MAX_PAYLOAD_SIZE = 1024
FRAME_TIMEOUT = 0.5
LOOPBACK_PORT_NAME = 'loop://'

AnyMessage = typing.Union[Message, StandardMessageBase]
StandardMessageType = typing.Type[StandardMessageBase]

_logger = getLogger(__name__)


class CommunicatorException(Exception):
    pass


class Communicator:
    """
    Asynchronous communicator class. This class is not thread-safe!
    """

    IO_WORKER_ERROR_LIMIT = 100

    def __init__(self,
                 port_name: str,
                 event_loop: asyncio.AbstractEventLoop):
        self._event_loop = event_loop
        self._ch = popcop.physical.serial_multiprocessing.Channel(port_name=port_name,
                                                                  max_payload_size=MAX_PAYLOAD_SIZE,
                                                                  frame_timeout=FRAME_TIMEOUT)
        self._codec: Codec = None

        self._log_queue = asyncio.Queue(loop=event_loop)
        self._message_queue = asyncio.Queue(loop=event_loop)

        self._pending_requests: typing.Set[typing.Tuple[typing.Callable, asyncio.Future]] = set()

        self._thread_handle = threading.Thread(target=self._thread_entry,
                                               name='communicator_io_worker',
                                               daemon=True)
        self._thread_handle.start()

    def __del__(self):
        if self.is_open:
            self._ch.close()

    def _thread_entry(self):
        # This thread is NOT allowed to invoke any methods of this class, for thread safety reasons!
        # The only field the thread is allowed to access is the communicatoin channel instance (which is thread safe).
        # Instead, we rely on call_soon_threadsafe() defined by the event loop.
        error_counter = 0
        while self._ch.is_open:
            # noinspection PyBroadException
            try:
                ret = self._ch.receive(0.1)

                if isinstance(ret, bytes):
                    log_str = ret.decode(encoding='utf8', errors='replace')
                    _logger.debug('Received log string: %r', log_str)
                    self._event_loop.call_soon_threadsafe(self._log_queue.put_nowait, log_str)

                elif ret is not None:
                    _logger.debug('Received item: %r', ret)
                    self._event_loop.call_soon_threadsafe(self._process_received_item, ret)

            except popcop.physical.serial_multiprocessing.ChannelClosedException as ex:
                _logger.info('Stopping the IO worker thread because the channel is closed. Error: %r', ex)
                break

            except Exception as ex:
                error_counter += 1
                _logger.error(f'Unhandled exception in IO worker thread '
                              f'({error_counter} of {self.IO_WORKER_ERROR_LIMIT}): {ex}', exc_info=True)
                if error_counter > self.IO_WORKER_ERROR_LIMIT:
                    _logger.error('Too many errors, stopping!')
                    break

            else:
                error_counter = 0

        _logger.info('IO worker thread is stopping')
        self._ch.close()

    def _process_received_item(self, item: typing.Union[ReceivedFrame, StandardMessageBase]) -> None:
        if isinstance(item, StandardMessageBase):
            message = item
        elif isinstance(item, ReceivedFrame):
            if self._codec is None:
                _logger.warning('Cannot decode application-specific frame because the codec is not yet initialized: %r',
                                item)
                return
            # noinspection PyBroadException
            try:
                message = self._codec.decode(item)
            except Exception:
                _logger.warning('Could not decode frame: %r', item, exc_info=True)
                return
        else:
            raise TypeError(f"Don't know how to handle this item: {item}")

        at_least_one_match = False
        for predicate, future in self._pending_requests:
            if not future.done() and predicate(message):
                at_least_one_match = True
                _logger.debug('Matching response: %r %r', item, future)
                future.set_result(message)

        if not at_least_one_match:
            self._message_queue.put_nowait(message)

    async def _do_send(self, message_or_type: typing.Union[Message, StandardMessageBase, StandardMessageType]):
        """
        This function is made async, but the current implementation does not require awaiting -
        we simply dump the message into the channel's queue non-blockingly and then return immediately.
        This implementation detail may be changed in the future, but the API won't be affected.
        """
        if isinstance(message_or_type, Message):
            if self._codec is None:
                raise CommunicatorException('Codec is not yet initialized, cannot send application-specific message')

            frame_type_code, payload = self._codec.encode(message_or_type)
            self._ch.send_application_specific(frame_type_code, payload)

        elif isinstance(message_or_type, (StandardMessageBase, type)):
            self._ch.send_standard(message_or_type)

        raise TypeError(f'Invalid message or message type: {type(message_or_type)}')

    @staticmethod
    def _match_message(reference: typing.Union[Message,
                                               MessageType,
                                               StandardMessageBase,
                                               StandardMessageType],
                       candidate: AnyMessage) -> bool:
        # Eliminate prototypes
        if isinstance(reference, StandardMessageBase):
            reference = type(reference)
        elif isinstance(reference, Message):
            reference = reference.type

        if isinstance(candidate, Message) and isinstance(reference, MessageType):
            return candidate.type == reference
        elif isinstance(candidate, StandardMessageBase) and isinstance(reference, type):
            return isinstance(candidate, reference)

    def set_protocol_version(self, major_minor: typing.Tuple[int, int]):
        """
        Sets the current protocol version, which defines which message formats to use.
        The protocol versions can be swapped at any time.
        By default, no protocol version is set, so only standard messages can be used.
        The user is required to assign a protocol version before using application-specific messages.
        """
        self._codec = Codec(major_minor)

    async def send(self, message: AnyMessage):
        """
        Simply emits the specified message asynchronously.
        """
        await self._do_send(message)

    async def request(self,
                      message_or_type: typing.Union[Message, StandardMessageBase, StandardMessageType],
                      timeout: typing.Union[float, int],
                      predicate: typing.Optional[typing.Callable[[AnyMessage], bool]]=None) ->\
            typing.Optional[AnyMessage]:
        """
        Sends a message, then awaits for a matching response.
        If no matching response was received before the timeout has expired, returns None.
        """
        if timeout <= 0:
            raise ValueError('A positive timeout is required')

        await self._do_send(message_or_type)

        predicate = predicate if predicate is not None else (lambda *_: True)

        def super_predicate(item: AnyMessage) -> bool:
            if self._match_message(message_or_type, item):
                try:
                    return predicate(item)
                except Exception as ex:
                    _logger.error('Unhandled exception in response predicate for message %r: %r',
                                  message_or_type, ex, exc_info=True)

        future = asyncio.Future()
        entry = super_predicate, future
        try:
            self._pending_requests.add(entry)
            return await asyncio.wait_for(future, timeout, self._event_loop)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_requests.remove(entry)

    async def receive(self) -> AnyMessage:
        return await self._message_queue.get()

    async def receive_log(self) -> str:
        return await self._log_queue.get()

    async def close(self):
        await asyncio.gather(self._event_loop.run_in_executor(None, self._thread_handle.join),
                             self._event_loop.run_in_executor(None, self._ch.close),
                             loop=self._event_loop)

    @property
    def is_open(self):
        return self._ch.is_open


def _unittest_communicator_message_matcher():
    pass


def _unittest_communicator_loopback():
    loop = asyncio.get_event_loop()
    com = Communicator(LOOPBACK_PORT_NAME, loop)

    async def sender():
        pass

    async def receiver():
        pass

    async def run():
        await asyncio.gather(sender(), receiver(), loop=loop)
        assert com.is_open
        await com.close()
        assert not com.is_open

    loop.run_until_complete(run())
