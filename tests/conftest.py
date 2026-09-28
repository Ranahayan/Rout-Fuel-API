"""Shared pytest fixtures."""

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def clear_caches():
    """Isolate tests from the route/plan cache and the spatial-index singleton."""
    cache.clear()
    from trips.spatial_index import reset_index

    reset_index()
    yield
    cache.clear()
    reset_index()
