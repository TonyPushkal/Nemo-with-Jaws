import socket

import pytest

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test runs offline: any attempt to open a connection to a non-loopback address fails loudly.
    Loopback is allowed so the local service can be tested over HTTP on 127.0.0.1."""
    real_connect, real_create = socket.socket.connect, socket.create_connection

    def connect(self, address, *args, **kwargs):
        if isinstance(address, tuple) and address and address[0] in LOOPBACK:
            return real_connect(self, address, *args, **kwargs)
        raise AssertionError("network access attempted in an offline test")

    def create_connection(address, *args, **kwargs):
        if address and address[0] in LOOPBACK:
            return real_create(address, *args, **kwargs)
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
