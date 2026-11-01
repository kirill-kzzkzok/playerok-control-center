"""Offline safety boundary for the whole suite: local servers remain available."""

import ipaddress
import os
import socket
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    # Child processes and Config.load must not inherit real shell credentials.
    for key in tuple(os.environ):
        if key.startswith(("TELEGRAM_", "TG_", "PLAYEROK_")) or key in {
            "DATABASE_PATH",
            "OWNER_IDS",
            "FIRST_START_CLAIM",
            "PAID_OPERATIONS_ENABLED",
        }:
            monkeypatch.delenv(key, raising=False)
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def allowed(address):
        if not isinstance(address, tuple):
            return
        host = str(address[0])
        if host == "localhost":
            return
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise AssertionError("OFFLINE_TEST_EXTERNAL_NETWORK_FORBIDDEN")

    def guarded(sock, address):
        allowed(address)
        return connect(sock, address)

    def guarded_ex(sock, address):
        allowed(address)
        return connect_ex(sock, address)

    def isolated_env(path=".env", *args, **kwargs):
        if Path(path).resolve() == ROOT / ".env":
            return {}
        return dotenv_values(path, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_ex)
    for module in ("app.config", "app.localvpn", "app.gifts.settings"):
        monkeypatch.setattr(module + ".dotenv_values", isolated_env)
