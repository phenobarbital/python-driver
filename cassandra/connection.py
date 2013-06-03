from collections import defaultdict, deque
import errno
from functools import partial, wraps
import logging
import socket
from threading import RLock, Event, Lock, Thread
import traceback
from Queue import Queue

import pyev

from cassandra import ConsistencyLevel
from cassandra.marshal import (int8_unpack, int32_unpack)
from cassandra.decoder import (OptionsMessage, ReadyMessage, AuthenticateMessage,
                               StartupMessage, ErrorMessage, CredentialsMessage,
                               QueryMessage, ResultMessage, decode_response)

log = logging.getLogger(__name__)

locally_supported_compressions = {}
try:
    import snappy
except ImportError:
    pass
else:
    # work around apparently buggy snappy decompress
    def decompress(byts):
        if byts == '\x00':
            return ''
        return snappy.decompress(byts)
    locally_supported_compressions['snappy'] = (snappy.compress, decompress)


MAX_STREAM_PER_CONNECTION = 128

PROTOCOL_VERSION = 0x01
PROTOCOL_VERSION_MASK = 0x7f

HEADER_DIRECTION_FROM_CLIENT = 0x00
HEADER_DIRECTION_TO_CLIENT = 0x80
HEADER_DIRECTION_MASK = 0x80

NONBLOCKING = (errno.EAGAIN, errno.EWOULDBLOCK)


class ConnectionException(Exception):

    def __init__(self, message, host=None):
        Exception.__init__(self, message)
        self.host = host


class ConnectionBusy(Exception):
    pass


class ProgrammingError(Exception):
    pass


class ProtocolError(Exception):
    pass


_loop = pyev.default_loop(pyev.EVBACKEND_SELECT)

_loop_notifier = _loop.async(lambda *a, **kw: None)
_loop_notifier.start()

# prevent _loop_notifier from keeping the loop from returning
_loop.unref()

_loop_started = None
_loop_lock = Lock()

def _run_loop():
    while True:
        end_condition = _loop.start()
        # there are still active watchers, no deadlock
        with _loop_lock:
            if end_condition:
                log.debug("Restarting event loop")
                continue
            else:
                # all Connections have been closed, no active watchers
                log.debug("All Connections currently closed, event loop ended")
                global _loop_started
                _loop_started = False
                break

def _start_loop():
    global _loop_started
    should_start = False
    with _loop_lock:
        if not _loop_started:
            log.debug("Starting pyev event loop")
            _loop_started = True
            should_start = True

    if should_start:
        t = Thread(target=_run_loop, name="async_event_loop")
        t.daemon = True
        t.start()

    return should_start


def defunct_on_error(f):

    @wraps(f)
    def wrapper(self, *args, **kwargs):
        try:
            return f(self, *args, **kwargs)
        except Exception, exc:
            self.defunct(exc)

    return wrapper


class Connection(object):

    in_buffer_size = 4096
    out_buffer_size = 4096

    cql_version = None

    keyspace = None
    compression = True
    compressor = None
    decompressor = None

    last_error = None
    in_flight = 0
    is_defunct = False
    is_closed = False
    lock = None

    _buf = ""
    _total_reqd_bytes = 0
    _read_watcher = None
    _write_watcher = None
    _socket = None

    @classmethod
    def factory(cls, *args, **kwargs):
        conn = cls(*args, **kwargs)
        conn.connected_event.wait()
        if conn.last_error:
            raise conn.last_error
        else:
            return conn

    def __init__(self, host='127.0.0.1', port=9042, credentials=None, sockopts=None, compression=True, cql_version=None):
        self.host = host
        self.port = port
        self.credentials = credentials
        self.compression = compression
        self.cql_version = cql_version

        self.connected_event = Event()

        self._id_queue = Queue(MAX_STREAM_PER_CONNECTION)
        for i in range(MAX_STREAM_PER_CONNECTION):
            self._id_queue.put_nowait(i)

        self._callbacks = {}
        self._push_watchers = defaultdict(set)
        self.lock = RLock()
        self.id_lock = Lock()

        self.deque = deque()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.connect((host, port))
        self._socket.setblocking(0)

        if sockopts:
            for args in sockopts:
                self._socket.setsockopt(*args)

        self._read_watcher = pyev.Io(self._socket._sock, pyev.EV_READ, _loop, self.handle_read)
        self._write_watcher = pyev.Io(self._socket._sock, pyev.EV_WRITE, _loop, self.handle_write)
        with _loop_lock:
            self._read_watcher.start()
            self._write_watcher.start()

        log.debug("Sending initial options message for new Connection to %s" % (host,))
        self.send_msg(OptionsMessage(), self._handle_options_response)

        # start the global event loop if needed
        if not _start_loop():
            # if the loop was already started, notify it
            with _loop_lock:
                _loop_notifier.send()

    def close(self):
        with self.lock:
            if self.is_closed:
                return
            self.is_closed = True

        log.debug("Closing connection to %s" % (self.host,))
        if self._read_watcher:
            self._read_watcher.stop()
        if self._write_watcher:
            self._write_watcher.stop()
        self._socket.close()
        with _loop_lock:
            _loop_notifier.send()

        # don't leave in-progress operations hanging
        if not self.is_defunct:
            self._error_all_callbacks(
                ConnectionException("Connection to %s was closed" % self.host))

    def __del__(self):
        self.close()

    def defunct(self, exc):
        log.debug("Defuncting connection to %s: %s %s" %
                  (self.host, exc, traceback.format_exc(exc)))
        self.last_error = exc
        self.is_defunct = True
        self._error_all_callbacks(exc)
        self.connected_event.set()
        return exc

    def _error_all_callbacks(self, exc):
        for cb in self._callbacks.values():
            cb(exc)

    def handle_write(self, watcher, revents):
        try:
            next_msg = self.deque.popleft()
        except IndexError:
            self._write_watcher.stop()
            return

        try:
            sent = self._socket.send(next_msg)
        except socket.error, err:
            if (err.args[0] in NONBLOCKING):
                self.deque.appendleft(next_msg)
            else:
                self.defunct(err)
            return
        else:
            if sent < len(next_msg):
                self.deque.appendleft(next_msg[sent:])

            if not self.deque:
                self._write_watcher.stop()

    def handle_read(self, watcher, revents):
        try:
            buf = self._socket.recv(self.in_buffer_size)
        except socket.error, err:
            if err.args[0] not in NONBLOCKING:
                self.defunct(err)
            return

        if buf:
            self._buf += buf
            while True:
                if len(self._buf) < 8:
                    # we don't have a complete header yet
                    break
                elif self._total_reqd_bytes and len(self._buf) < self._total_reqd_bytes:
                    # we already saw a header, but we don't have a complete message yet
                    break
                else:
                    body_len = int32_unpack(self._buf[4:8])
                    if len(self._buf) - 8 >= body_len:
                        msg = self._buf[:8 + body_len]
                        self._buf = self._buf[8 + body_len:]
                        self._total_reqd_bytes = 0
                        self.process_msg(msg, body_len)
                    else:
                        self._total_reqd_bytes = body_len + 8
        else:
            logging.debug("connection closed by server")
            self.close()

    @defunct_on_error
    def process_msg(self, msg, body_len):
        version, flags, stream_id, opcode = map(int8_unpack, msg[:4])
        if stream_id < 0:
            callback = None
        else:
            callback = self._callbacks.pop(stream_id)
            self._id_queue.put_nowait(stream_id)

        # check that the protocol version is supported
        given_version = version & PROTOCOL_VERSION_MASK
        if given_version != PROTOCOL_VERSION:
            raise ProtocolError("Unsupported CQL protocol version: %d" % given_version)

        # check that the header direction is correct
        if version & HEADER_DIRECTION_MASK != HEADER_DIRECTION_TO_CLIENT:
            raise ProtocolError(
                "Header direction in response is incorrect; opcode %04x, stream id %r"
                % (opcode, stream_id))

        if body_len > 0:
            body = msg[8:]
        elif body_len == 0:
            body = ""
        else:
            raise ProtocolError("Got negative body length: %r" % body_len)

        response = decode_response(stream_id, flags, opcode, body, self.decompressor)

        try:
            if stream_id < 0:
                self.handle_pushed(response)
            elif callback is not None:
                callback(response)
        except:
            log.exception("Callback handler errored, ignoring:")

    def handle_pushed(self, response):
        for cb in self._push_watchers[response.type]:
            try:
                cb(response)
            except:
                log.exception("Pushed event handler errored, ignoring:")

    def push(self, data):
        sabs = self.out_buffer_size
        with self.lock:
            if len(data) > sabs:
                for i in xrange(0, len(data), sabs):
                    self.deque.append(data[i:i + sabs])
            else:
                self.deque.append(data)

            if not self._write_watcher.active:
                with _loop_lock:
                    self._write_watcher.start()
                    _loop_notifier.send()

    def send_msg(self, msg, cb):
        if self.is_defunct:
            raise ConnectionException("Connection to %s is defunct" % self.host)
        elif self.is_closed:
            raise ConnectionException("Connection to %s is closed" % self.host)

        try:
            request_id = self._id_queue.get_nowait()
        except Queue.EMPTY:
            raise ConnectionBusy(
                "Connection to %s is at the max number of requests" % self.host)

        self._callbacks[request_id] = cb
        self.push(msg.to_string(request_id, compression=self.compressor))
        return request_id

    def wait_for_response(self, msg):
        return self.wait_for_responses(msg)[0]

    def wait_for_responses(self, *msgs):
        waiter = ResponseWaiter(len(msgs))
        for i, msg in enumerate(msgs):
            self.send_msg(msg, partial(waiter.got_response, index=i))

        return waiter.deliver()

    def register_watcher(self, event_type, callback):
        self._push_watchers[event_type].add(callback)

    def register_watchers(self, type_callback_dict):
        for event_type, callback in type_callback_dict.items():
            self.register_watcher(event_type, callback)

    @defunct_on_error
    def _handle_options_response(self, options_response):
        if self.is_defunct:
            return
        log.debug("Received options response on new Connection from %s" % self.host)
        self.supported_cql_versions = options_response.cql_versions
        self.remote_supported_compressions = options_response.options['COMPRESSION']

        if self.cql_version:
            if self.cql_version not in self.supported_cql_versions:
                raise ProtocolError(
                    "cql_version %r is not supported by remote (w/ native "
                    "protocol). Supported versions: %r"
                    % (self.cql_version, self.supported_cql_versions))
        else:
            self.cql_version = self.supported_cql_versions[0]

        opts = {}
        self._compressor = None
        if self.compression:
            overlap = (set(locally_supported_compressions.keys()) &
                       set(self.remote_supported_compressions))
            if len(overlap) == 0:
                log.debug("No available compression types supported on both ends."
                          " locally supported: %r. remotely supported: %r"
                          % (locally_supported_compressions.keys(),
                             self.remote_supported_compressions))
            else:
                compression_type = iter(overlap).next() # choose any
                opts['COMPRESSION'] = compression_type
                # set the decompressor here, but set the compressor only after
                # a successful Ready message
                self._compressor, self.decompressor = \
                    locally_supported_compressions[compression_type]

        sm = StartupMessage(cqlversion=self.cql_version, options=opts)
        self.send_msg(sm, cb=self._handle_startup_response)

    @defunct_on_error
    def _handle_startup_response(self, startup_response):
        if self.is_defunct:
            return
        if isinstance(startup_response, ReadyMessage):
            log.debug("Got ReadyMessage on new Connection from %s" % self.host)
            if self._compressor:
                self.compressor = self._compressor
            self.connected_event.set()
        elif isinstance(startup_response, AuthenticateMessage):
            log.debug("Got AuthenticateMessage on new Connection from %s" % self.host)

            if self.credentials is None:
                raise ProgrammingError('Remote end requires authentication.')

            self.authenticator = startup_response.authenticator
            cm = CredentialsMessage(creds=self.credentials)
            self.send_msg(cm, cb=self._handle_startup_response)
        elif isinstance(startup_response, ErrorMessage):
            log.debug("Received ErrorMessage on new Connection from %s: %s"
                      % (self.host, startup_response.summary_msg()))
            raise ConnectionException(
                "Failed to initialize new connection to %s: %s"
                % (self.host, startup_response.summary_msg()))
        else:
            msg = "Unexpected response during Connection setup: %r" % (startup_response,)
            log.error(msg)
            raise ProtocolError(msg)

    def set_keyspace(self, keyspace):
        if not keyspace:
            return

        with self.lock:
            if keyspace == self.keyspace:
                return

            query = 'USE "%s"' % (keyspace,)
            try:
                result = self.wait_for_response(
                    QueryMessage(query=query, consistency_level=ConsistencyLevel.ONE))
                if isinstance(result, ResultMessage):
                    self.keyspace = keyspace
                else:
                    raise self.defunct(ConnectionException(
                        "Problem while setting keyspace: %r" % (result,), self.host))
            except Exception, exc:
                raise self.defunct(ConnectionException(
                    "Problem while setting keyspace: %r" % (exc,), self.host))


class ResponseWaiter(object):

    def __init__(self, num_responses):
        self.pending = num_responses
        self.error = None
        self.responses = [None] * num_responses
        self.event = Event()

    def got_response(self, response, index):
        if isinstance(response, Exception):
            self.error = response
            self.event.set()
        else:
            self.responses[index] = response
            self.pending -= 1
            if not self.pending:
                self.event.set()

    def deliver(self):
        self.event.wait()
        if self.error:
            raise self.error
        else:
            return self.responses
