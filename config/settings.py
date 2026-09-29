"""
Django settings for the USA Fuel-Optimal Route API.

See README.md for the project overview. Environment-driven settings are read
via django-environ; copy .env.example to .env to configure (an ORS API key is
optional — the service falls back to the keyless OSRM provider).

Defaults are production-safe: ``DEBUG`` is off unless a ``.env`` (or the
environment) turns it on, and the production path is *guarded* -- it refuses to
boot with the insecure dev SECRET_KEY or an unset ALLOWED_HOSTS, and switches on
the usual transport-security settings (see the guard blocks below). Local
development uses ``.env.example`` (``DEBUG=True``); the test suite uses
``config.settings_test``.
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
    DEBUG=(bool, False),
    SECRET_KEY=(str, INSECURE_SECRET_KEY),
    ALLOWED_HOSTS=(list, []),
    LOG_LEVEL=(str, "INFO"),
    # --- Caching / throttling (shared in prod via REDIS_URL) ---
    REDIS_URL=(str, ""),
    ANON_THROTTLE_RATE=(str, "30/min"),
    # Trusted reverse-proxy hops in front of the app. 0 = identify clients by the
    # socket address and ignore X-Forwarded-For (which any client can forge).
    NUM_PROXIES=(int, 0),
    # Production transport security (only read when DEBUG=False).
    SECURE_SSL_REDIRECT=(bool, True),
    SECURE_HSTS_SECONDS=(int, 60 * 60 * 24 * 365),
    # Preload is a long-term commitment that is hard to undo; opt in deliberately.
    SECURE_HSTS_PRELOAD=(bool, False),
    # Trust X-Forwarded-Proto from a TLS-terminating proxy. Set False when the app
    # is exposed directly, or a client could spoof "https" to skip the redirect.
    TRUST_X_FORWARDED_PROTO=(bool, True),
    # --- Routing provider ---
    ROUTING_PROVIDER=(str, ""),  # "ors" | "osrm" | "" (auto: ors if key else osrm)
    ORS_API_KEY=(str, ""),
    # Minimum Pelias confidence to accept a fuzzy place-name match (0..1).
    ORS_GEOCODE_MIN_CONFIDENCE=(float, 0.5),
    ORS_BASE_URL=(str, "https://api.openrouteservice.org"),
    OSRM_BASE_URL=(str, "https://router.project-osrm.org"),
    ROUTING_TIMEOUT_SECONDS=(float, 8.0),
    # Wall-clock budget for ALL upstream work in one request (geocodes, the
    # routing call, its retry and the fallback provider). Keep it under the
    # web server's worker timeout (gunicorn defaults to 30 s).
    ROUTING_TOTAL_DEADLINE_SECONDS=(float, 20.0),
    # After a provider FAILURE (outage, quota, unusable response) try the other
    # provider once, if usable: ORS -> OSRM always; OSRM -> ORS when a key is set.
    ROUTING_FALLBACK=(bool, True),
    # How long a provider *failure* for a given trip is remembered before the
    # provider is asked again (0 disables). Unroutable trips are cached like results.
    ROUTING_FAILURE_CACHE_SECONDS=(int, 30),
    # --- Algorithm tunables ---
    VEHICLE_RANGE_MILES=(float, 500.0),
    VEHICLE_MPG=(float, 10.0),
    CORRIDOR_BUFFER_MILES=(float, 7.0),
    # --- Storage / data files ---
    DATABASE_PATH=(str, str(BASE_DIR / "db.sqlite3")),  # put it on a volume in containers
    FUEL_CSV_PATH=(str, str(DATA_DIR / "fuel-prices-for-be-assessment.csv")),
    US_CITIES_CSV_PATH=(str, str(DATA_DIR / "uscities.csv")),
    US_BOUNDARY_GEOJSON_PATH=(str, str(DATA_DIR / "us_boundary_ne10m.geojson")),
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
        "NAME": env("DATABASE_PATH"),
    }
}

# Cache for route geometry + plan responses. A shared Redis backend is used when
# REDIS_URL is set (so the "one routing call per route" budget and DRF throttle
# counters are shared across processes/workers); otherwise an in-process LocMem
# cache that is per-worker (fine for single-process dev).
if env("REDIS_URL"):
    CACHES = {
        "default": {
            # RedisCache subclass that degrades to "no cache" when Redis is down
            # instead of turning every request into a 500 (see config/cache.py).
            "BACKEND": "config.cache.SafeRedisCache",
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

# Django's defaults: the admin is enabled, so admin accounts get real password rules.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    # The browsable HTML API is a development aid: it costs 20-40x a JSON
    # response to render and widens the surface, so it is off in production.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        *(["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    ],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # One error envelope {"error", "code", ...} for every failure (see trips/api_errors.py).
    "EXCEPTION_HANDLER": "trips.api_errors.api_exception_handler",
    # A public, read-only API: no authentication scheme at all. (DRF's default
    # Session+Basic auth would run password hashing for any forged Basic header
    # *before* throttling -- an unthrottled password-guessing oracle for admin
    # accounts.) The Django admin keeps its own session login.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    # Per-IP rate limiting, applied to every DRF view -- the JSON endpoint AND
    # the map page, which trigger the same external routing call and CPU-bound
    # work on a cache miss. Counters live in the default cache (shared via
    # REDIS_URL).
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": env("ANON_THROTTLE_RATE")},
    # With 0, the throttle keys on REMOTE_ADDR only; a forged X-Forwarded-For
    # header cannot mint a fresh quota. Set to the real number of proxy hops.
    "NUM_PROXIES": env("NUM_PROXIES"),
}

# Structured logging so provider failures, endpoint-resolution errors, and the
# routing-call budget are observable instead of silent. Every record passes
# through a redaction filter: the ORS geocoder takes the API key as a query
# parameter, so a logged HTTP exception (which embeds the URL) would otherwise
# write the key to the logs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "filters": {
        "redact_secrets": {"()": "routing.redaction.RedactSecretsFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["redact_secrets"],
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "routing": {"handlers": ["console"], "level": env("LOG_LEVEL"), "propagate": False},
        "trips": {"handlers": ["console"], "level": env("LOG_LEVEL"), "propagate": False},
        "stations": {"handlers": ["console"], "level": env("LOG_LEVEL"), "propagate": False},
    },
}

# Production transport-security hardening (no-ops in DEBUG).
if not DEBUG:
    if env("TRUST_X_FORWARDED_PROTO"):  # TLS terminated by a trusted proxy
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")
    SECURE_HSTS_SECONDS = env("SECURE_HSTS_SECONDS")
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = env("SECURE_HSTS_PRELOAD")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Load-balancer probes hit /healthz over plain HTTP; a 301 would mark us down.
    SECURE_REDIRECT_EXEMPT = [r"^healthz/?$"]
    # HSTS preload is deliberately opt-in (SECURE_HSTS_PRELOAD); `check --deploy`
    # would otherwise nag about it on every run.
    SILENCED_SYSTEM_CHECKS = ["security.W021"]

# --- Project-specific settings (consumed across apps) ---
ROUTING_PROVIDER = env("ROUTING_PROVIDER")
ORS_API_KEY = env("ORS_API_KEY")
ORS_GEOCODE_MIN_CONFIDENCE = env("ORS_GEOCODE_MIN_CONFIDENCE")
ORS_BASE_URL = env("ORS_BASE_URL")
OSRM_BASE_URL = env("OSRM_BASE_URL")
ROUTING_TIMEOUT_SECONDS = env("ROUTING_TIMEOUT_SECONDS")
ROUTING_TOTAL_DEADLINE_SECONDS = env("ROUTING_TOTAL_DEADLINE_SECONDS")
ROUTING_FALLBACK = env("ROUTING_FALLBACK")
ROUTING_FAILURE_CACHE_SECONDS = env("ROUTING_FAILURE_CACHE_SECONDS")

VEHICLE_RANGE_MILES = env("VEHICLE_RANGE_MILES")
VEHICLE_MPG = env("VEHICLE_MPG")
CORRIDOR_BUFFER_MILES = env("CORRIDOR_BUFFER_MILES")

FUEL_CSV_PATH = env("FUEL_CSV_PATH")
US_CITIES_CSV_PATH = env("US_CITIES_CSV_PATH")
US_BOUNDARY_GEOJSON_PATH = env("US_BOUNDARY_GEOJSON_PATH")
