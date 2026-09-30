import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test runs offline: any attempt to open a connection fails loudly."""
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

