"""A Redis cache backend that degrades instead of failing every request.

Django's built-in ``RedisCache`` propagates connection errors. With Redis down
that would turn every request into a 500 -- DRF's throttle touches the cache
before any view runs. This subclass catches Redis errors on each operation,
logs them (at most once per ``_LOG_INTERVAL_SECONDS`` so an outage does not
flood the log), and then behaves like an empty cache: reads miss, writes are
dropped, and ``add`` reports success so a single-flight leader is still elected
within the process. Nothing in the app depends on the cache for correctness;
only efficiency does (upstream call count, throttle accuracy).
"""

from __future__ import annotations

import logging
import time

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import RedisCache

try:
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - the system check reports the missing package
    RedisError = OSError  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

_LOG_INTERVAL_SECONDS = 30.0
_last_logged_at = 0.0


class SafeRedisCache(RedisCache):
    """``RedisCache`` whose operations never raise on a Redis outage."""

    def _guard(self, operation, fallback, *args, **kwargs):
        global _last_logged_at
        try:
            return operation(*args, **kwargs)
        except (RedisError, OSError) as exc:
            now = time.monotonic()
            if now - _last_logged_at >= _LOG_INTERVAL_SECONDS:
                _last_logged_at = now
                logger.warning(
                    "Redis unavailable (%s: %s); serving without cache", type(exc).__name__, exc
                )
            return fallback

    def get(self, key, default=None, version=None):
        return self._guard(super().get, default, key, default, version)

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guard(super().set, None, key, value, timeout, version)

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guard(super().add, True, key, value, timeout, version)

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guard(super().touch, False, key, timeout, version)

    def delete(self, key, version=None):
        return self._guard(super().delete, False, key, version)

    def get_many(self, keys, version=None):
        return self._guard(super().get_many, {}, keys, version)

    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guard(super().set_many, [], data, timeout, version)

    def delete_many(self, keys, version=None):
        return self._guard(super().delete_many, None, keys, version)

    def has_key(self, key, version=None):
        return self._guard(super().has_key, False, key, version)

    def incr(self, key, delta=1, version=None):
        return self._guard(super().incr, delta, key, delta, version)

    def clear(self):
        return self._guard(super().clear, None)
