"""Neutral synthetic helpers shared by Home Assistant tests."""
import asyncio
import json
import time
from types import SimpleNamespace


class Owner:
    """Public owned transport contract, without borrowed wrapper aliases."""

    def __init__(self, api, lock):
        self.api = api
        self.lock = lock
        self.current = True

    def is_current(self):
        return self.current


class FakeSocket:
    """One injected TCP connection to a FakeDevice; thread-safe."""

    def __init__(self, device):
        import threading
        self.device = device
        self.timeout = None
        self.closed = False
        self.peer_closed = False
        self.buffer = bytearray()
        self.cond = threading.Condition()
        self.session_key = None
        self.local_nonce = self.remote_nonce = None

    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def fileno(self):
        return -1 if self.closed else 1000

    def connect(self, address):
        self.device.addresses.append(address)
        if self.device.refuse:
            raise ConnectionRefusedError('synthetic refusal')

    def sendall(self, data):
        with self.device.io():
            if self.closed or self.peer_closed:
                raise BrokenPipeError('synthetic closed')
            self.device.from_client(self, bytes(data))

    def recv(self, size):
        import socket
        with self.device.io():
            gate = self.device.take_gate()
            if gate is not None:
                self.device.entered.set()
                gate.wait(10)
            error = self.device.recv_error
            if error is not None:
                self.device.recv_error = None
                raise error
            deadline = time.monotonic() + min(self.timeout or 0.05, 0.05)
            with self.cond:
                while not self.buffer:
                    if self.closed:
                        raise OSError('synthetic closed')
                    if self.peer_closed:
                        return b''
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout('timed out')
                    if not self.cond.wait(remaining):
                        raise socket.timeout('timed out')
                chunk = bytes(self.buffer[:size])
                del self.buffer[:size]
                return chunk

    def push(self, data):
        with self.cond:
            self.buffer.extend(data)
            self.cond.notify_all()

    def drop(self):
        with self.cond:
            self.peer_closed = True
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def _frame(dps):
    return SimpleNamespace(payload=json.dumps({'dps': dps}).encode(), crc_good=True)


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition not reached'
        await asyncio.sleep(0.01)
