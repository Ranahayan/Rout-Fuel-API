"""
Django settings for the USA Fuel-Optimal Route API.

See README.md for the project overview. Environment-driven settings are read
via django-environ; copy .env.example to .env to configure (an ORS API key is
optional — the service falls back to the keyless OSRM provider).

Defaults are dev-friendly so the project runs with zero configuration. The
production path (``DEBUG=False``) is *guarded*: it refuses to boot with the
insecure dev SECRET_KEY or an unset ALLOWED_HOSTS, and it switches on the usual
transport-security settings — see the guard blocks below.
"""

from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Sentinel dev key. Usable only while DEBUG=True; the guard below refuses to let
# a production process boot with it.
INSECURE_SECRET_KEY = "django-insecure-dev-key-change-me-in-production"

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, INSECURE_SECRET_KEY),
    ALLOWED_HOSTS=(list, []),
    LOG_LEVEL=(str, "INFO"),
    # --- Caching / throttling (shared in prod via REDIS_URL) ---
    REDIS_URL=(str, ""),
    ANON_THROTTLE_RATE=(str, "30/min"),
    USER_THROTTLE_RATE=(str, "120/min"),
    # --- Routing provider ---
    ROUTING_PROVIDER=(str, ""),  # "ors" | "osrm" | "" (auto: ors if key else osrm)
    ORS_API_KEY=(str, ""),
    # Minimum Pelias confidence to accept a fuzzy place-name match (0..1).
    ORS_GEOCODE_MIN_CONFIDENCE=(float, 0.5),
    ORS_BASE_URL=(str, "https://api.openrouteservice.org"),
    OSRM_BASE_URL=(str, "https://router.project-osrm.org"),
    ROUTING_TIMEOUT_SECONDS=(float, 8.0),
    # --- Algorithm tunables ---
    VEHICLE_RANGE_MILES=(float, 500.0),
    VEHICLE_MPG=(float, 10.0),
    CORRIDOR_BUFFER_MILES=(float, 7.0),
    # --- Data files ---
    FUEL_CSV_PATH=(str, str(DATA_DIR / "fuel-prices-for-be-assessment.csv")),
    US_CITIES_CSV_PATH=(str, str(DATA_DIR / "uscities.csv")),
)

# Load .env if present (optional).
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(str(env_file))

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

if DEBUG:
    # Local-dev convenience: always allow loopback hosts when none are given.
    if not ALLOWED_HOSTS:
        ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
else:
    # Production guards — fail loudly rather than boot insecurely.
    if SECRET_KEY == INSECURE_SECRET_KEY:
        raise ImproperlyConfigured(
            "SECRET_KEY must be set to a unique secret value when DEBUG=False."
        )
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("ALLOWED_HOSTS must be set explicitly when DEBUG=False.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "stations",
    "routing",
    "trips",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Cache for route geometry + plan responses. A shared Redis backend is used when
# REDIS_URL is set (so the "one routing call per route" budget and DRF throttle
# counters are shared across processes/workers); otherwise an in-process LocMem
# cache that is per-worker (fine for single-process dev).
if env("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": env("REDIS_URL"),
            "TIMEOUT": 60 * 60 * 6,  # 6 hours; CSV prices are static
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "fuel-route-cache",
            "TIMEOUT": 60 * 60 * 6,  # 6 hours; CSV prices are static
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # Per-IP (anon) / per-user rate limiting. The endpoint triggers an external
    # routing call and CPU-bound work on a cache miss, so it must not be
    # unbounded. Counters live in the default cache (shared via REDIS_URL).
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("ANON_THROTTLE_RATE"),
        "user": env("USER_THROTTLE_RATE"),
    },
}

# Structured logging so provider failures, endpoint-resolution errors, and the
# routing-call budget are observable instead of silent.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "routing": {"handlers": ["console"], "level": env("LOG_LEVEL"), "propagate": False},
        "trips": {"handlers": ["console"], "level": env("LOG_LEVEL"), "propagate": False},
        "stations": {"handlers": ["console"], "level": env("LOG_LEVEL"), "propagate": False},
    },
}

# Production transport-security hardening (no-ops in DEBUG; assumes TLS is
# terminated upstream and forwarded via X-Forwarded-Proto).
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 365)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# --- Project-specific settings (consumed across apps) ---
ROUTING_PROVIDER = env("ROUTING_PROVIDER")
ORS_API_KEY = env("ORS_API_KEY")
ORS_GEOCODE_MIN_CONFIDENCE = env("ORS_GEOCODE_MIN_CONFIDENCE")
ORS_BASE_URL = env("ORS_BASE_URL")
OSRM_BASE_URL = env("OSRM_BASE_URL")
ROUTING_TIMEOUT_SECONDS = env("ROUTING_TIMEOUT_SECONDS")

VEHICLE_RANGE_MILES = env("VEHICLE_RANGE_MILES")
VEHICLE_MPG = env("VEHICLE_MPG")
CORRIDOR_BUFFER_MILES = env("CORRIDOR_BUFFER_MILES")

FUEL_CSV_PATH = env("FUEL_CSV_PATH")
US_CITIES_CSV_PATH = env("US_CITIES_CSV_PATH")
