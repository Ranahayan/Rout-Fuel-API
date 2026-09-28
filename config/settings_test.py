"""Settings for the test suite (``pytest.ini`` points here).

The base settings default to production-safe values (``DEBUG=False``, which
requires a real ``SECRET_KEY`` and ``ALLOWED_HOSTS`` and turns on the HTTPS
redirect), so tests would not even boot without a ``.env``. This module fills
in test-only defaults *before* importing the base settings -- existing
environment variables still win -- and forces the in-process cache so a test
run can never flush a shared Redis via ``cache.clear()``.
"""

import os

os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production-0123456789")
os.environ.setdefault("ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")

from .settings import *  # noqa: E402, F403

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-route-tests",
        "TIMEOUT": 60 * 60 * 6,
    }
}
