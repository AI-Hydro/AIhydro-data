"""
Test-suite conftest.

Disables the transient-retry helper unless the user explicitly opts in
for a live run. Without this, offline tests that exercise the real
fallback chain (e.g. test_batch_returns_expected_structure) would burn
~3 minutes per test attempting to retry network failures that will
never succeed in an offline env.

Live tests still need retries because real upstreams are flaky. They
opt back in by setting `AIHYDRO_DATA_NO_RETRY=0` (or unsetting it) in
their own fixtures — we do NOT touch the env var when `-m live` is
selected, so the user's env wins.
"""
from __future__ import annotations

import os
import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Default to no-retry. Live runs can opt back in via env or fixtures."""
    # Only disable retries if the user hasn't explicitly set the var.
    if "AIHYDRO_DATA_NO_RETRY" not in os.environ:
        # If the user is selecting only live tests, don't kill retries —
        # they need them for upstream flakiness.
        markexpr = (config.getoption("-m") or "").strip()
        if markexpr != "live":
            os.environ["AIHYDRO_DATA_NO_RETRY"] = "1"


# ── Offline guard ───────────────────────────────────────────────────────────
# Tests not marked ``live`` must never reach the network: a stalled upstream
# turns an offline suite into a hang. Outbound connects/DNS lookups to
# non-loopback hosts fail fast, naming the offending test. Unix sockets and
# localhost / 127.0.0.1 / ::1 stay allowed (local servers, subprocess IPC).
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


class NetworkAccessBlocked(RuntimeError):
    """Raised when a non-``live`` test attempts outbound network access."""


def _host_of(address):
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
        return host.decode() if isinstance(host, bytes) else host
    return None  # str/bytes path (AF_UNIX) or unknown -> not network


def _is_local(host) -> bool:
    return host is None or str(host).lower() in _LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def _block_network_for_offline_tests(request, monkeypatch):
    """Fail fast if a test not marked ``live`` touches the network."""
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    import socket

    test_id = request.node.nodeid
    attempts: list[str] = []

    def _refuse(host, port=None):
        msg = (
            f"{test_id}: outbound network access to {host!r}"
            f"{'' if port is None else f' port {port}'} blocked. Tests not "
            "marked `live` must run offline: mock the network at the lowest "
            "layer, or mark the test `live`."
        )
        attempts.append(msg)
        raise NetworkAccessBlocked(msg)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        host = _host_of(address)
        if not _is_local(host):
            _refuse(host, address[1] if len(address) > 1 else None)
        return real_connect(self, address)

    def connect_ex(self, address):
        host = _host_of(address)
        if not _is_local(host):
            _refuse(host, address[1] if len(address) > 1 else None)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        host = _host_of(address)
        if not _is_local(host):
            _refuse(host, address[1] if len(address) > 1 else None)
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host, port, *args, **kwargs):
        if not _is_local(host):
            _refuse(host, port)
        return real_getaddrinfo(host, port, *args, **kwargs)

    real_create_connection = socket.create_connection
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    # Library code often wraps/swallows the connect error (retry chains,
    # fallbacks); an attempt that was caught is still an offline violation.
    if attempts:
        pytest.fail(f"{len(attempts)} blocked network attempt(s); first: {attempts[0]}", pytrace=False)


@pytest.fixture
def offline_backends(monkeypatch):
    """Simulate every backend being unreachable, at the lowest pipeline layer.

    Replaces ``_pipeline._fetch_one`` (the single per-product backend call)
    with a stub raising ``SourceUnavailable``. Returns the list of product ids
    the router attempted, so tests can assert routing without any network.
    """
    import aihydro_data._pipeline as pipeline
    from aihydro_data.exceptions import SourceUnavailable

    attempted: list[str] = []

    def _unreachable(spec, *args, **kwargs):
        attempted.append(spec.id)
        raise SourceUnavailable(
            code="TEST_OFFLINE",
            message=f"{spec.id}: backend unreachable (offline test).",
            recovery="n/a",
        )

    monkeypatch.setattr(pipeline, "_fetch_one", _unreachable)
    return attempted
