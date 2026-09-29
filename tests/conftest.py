"""Shared pytest fixtures.

Two guarantees for every test:

* **No network.** Any attempt to open a real socket connection fails loudly.
  The developer ``.env`` may select ORS with a real key, and the OSRM public
  server is fair-use, so an unpatched test must never be able to reach either.
  Everything upstream is mocked with ``responses`` or ``unittest.mock``.
* **Isolation.** The cache (routes, plans, throttle counters, single-flight
  locks) and the in-memory spatial index are cleared around each test.
"""

import socket

import pytest
from django.core.cache import cache


class NetworkAccessBlocked(RuntimeError):
    pass


def _blocked(self, *args, **kwargs):
    raise NetworkAccessBlocked(f"Real network access attempted from a test: {args!r}")


@pytest.fixture(autouse=True, scope="session")
def block_network():
    originals = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)
    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]
    yield
    socket.socket.connect, socket.socket.connect_ex, socket.create_connection = originals  # type: ignore[method-assign,assignment]


@pytest.fixture(autouse=True)
def clear_caches():
    """Isolate tests from the route/plan cache and the spatial-index singleton."""
    from trips.spatial_index import reset_index

    cache.clear()
    reset_index()
    yield
    cache.clear()
    reset_index()
