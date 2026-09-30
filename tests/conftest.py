import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test runs offline: any attempt to open a connection fails loudly."""
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


# Synthetic values for tests only; they are not the user's preferences.
FILLED = """\
version: 1
search:
  target_titles: ["Widget Engineer"]
  related_titles: ["Gadget Developer"]
  responsibilities: ["build widgets"]
  companies_preferred: [{name: "Acme"}]
hard:
  locations: [{place: "Testville", mode: onsite}, {place: "Freedonia", mode: remote}]
  must_skills: ["Python"]
preferred:
  skills: ["Rust"]
"""
