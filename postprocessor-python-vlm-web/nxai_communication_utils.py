"""
Pure-Python replacement for nxai_communication_utils.py
Removes dependency on libnxai-c-utilities-shared.so
Wire protocol: uint32 LE length prefix + msgpack payload
"""
import os
import sys
import socket
import struct
import msgpack
from typing import Union

script_location = os.path.dirname(os.path.realpath(__file__))

# ── Exceptions ────────────────────────────────────────────────────────────────

class SocketError(Exception):
    pass

class SocketTimeout(Exception):
    pass

class SharedMemoryError(Exception):
    pass

class ExitSignal:
    pass

# ── Socket helpers ────────────────────────────────────────────────────────────

_ACCEPT_TIMEOUT = 1.0   # seconds; lets the main loop check shutdown_event

def _recv_exact(sock, n):
    """Read exactly n bytes from sock."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocketError("Connection closed while reading")
        buf += chunk
    return buf

def _read_framed(sock):
    """Read one length-prefixed message (uint32 LE + payload)."""
    header = _recv_exact(sock, 4)
    msg_len = struct.unpack('<I', header)[0]
    return _recv_exact(sock, msg_len)

def _write_framed(sock, data):
    """Write one length-prefixed message (uint32 LE + payload)."""
    sock.sendall(struct.pack('<I', len(data)) + data)


class SocketConnection:
    def __init__(self, sock, socket_path):
        self._sock = sock
        self._socket_path = socket_path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, data: Union[str, bytes]) -> None:
        if isinstance(data, str):
            data = data.encode('utf-8')
        try:
            _write_framed(self._sock, data)
        except OSError as e:
            raise SocketError(f"Send failed: {e}")

    def receive(self) -> bytes:
        try:
            return _read_framed(self._sock)
        except SocketError:
            raise SocketTimeout("Timed out waiting for message")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class SocketListener:
    def __init__(self, socket_path: str):
        self._socket_path = socket_path
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(socket_path)
        self._server.listen(5)
        self._server.settimeout(_ACCEPT_TIMEOUT)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def accept(self):
        try:
            conn, _ = self._server.accept()
        except socket.timeout:
            raise SocketTimeout
        conn.settimeout(10.0)
        try:
            msg = _read_framed(conn)
        except SocketError as e:
            conn.close()
            raise SocketError(f"Failed to read initial message: {e}")
        return SocketConnection(conn, self._socket_path), msg

    def close(self) -> None:
        try:
            self._server.close()
        except Exception:
            pass
        try:
            os.unlink(self._socket_path)
        except Exception:
            pass


class SocketClient(SocketConnection):
    def __init__(self, socket_path: str):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(socket_path)
        except OSError as e:
            raise ConnectionRefusedError(f"Failed to connect to {socket_path}: {e}")
        super().__init__(sock, socket_path)


# ── Message helpers (kept compatible with original) ────────────────────────────

def send_message(socket_path: str, data: Union[str, bytes]) -> None:
    if isinstance(data, str):
        data = data.encode('utf-8')
    with SocketClient(socket_path) as c:
        c.send(data)


def send_receive_message(socket_path: str, data: Union[str, bytes]) -> bytes:
    if isinstance(data, str):
        data = data.encode('utf-8')
    with SocketClient(socket_path) as c:
        c.send(data)
        return c.receive()


# ── Inference result serialisation ────────────────────────────────────────────

def parseInferenceResults(message: bytes):
    parsed = msgpack.unpackb(message)
    if 'EXIT' in parsed:
        return ExitSignal()
    if 'BBoxes_xyxy' in parsed:
        for key, value in parsed['BBoxes_xyxy'].items():
            parsed['BBoxes_xyxy'][key] = list(
                struct.unpack('f' * (len(value) // 4), value)
            )
    if 'Identity' in parsed:
        parsed['Identity'] = list(
            struct.unpack('f' * (len(parsed['Identity']) // 4), parsed['Identity'])
        )
    return parsed


def writeInferenceResults(obj: dict) -> bytes:
    if 'BBoxes_xyxy' in obj:
        for key, value in obj['BBoxes_xyxy'].items():
            obj['BBoxes_xyxy'][key] = struct.pack('f' * len(value), *value)
    if 'Identity' in obj:
        obj['Identity'] = struct.pack('f' * len(obj['Identity']), *obj['Identity'])  # fixed: *unpacked
    return msgpack.packb(obj)
