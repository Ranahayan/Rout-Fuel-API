"""The ORS API key must never reach the logs, whatever code path logs it.

The geocoder sends the key as a query parameter, so a logged HTTP exception
(whose text embeds the URL) would leak it. Every record on the console handler
passes through ``routing.redaction.RedactSecretsFilter``.
"""

import io
import logging
import sys

import pytest
import requests
import responses
from django.conf import settings
from django.test import override_settings

from routing.providers import ORSProvider
from routing.redaction import REDACTED, RedactSecretsFilter, redact_secrets

FAKE_KEY = "FAKEKEY_abcdefghijklmnop0123456789"


def _console_handler() -> logging.StreamHandler:
    return next(
        h for h in logging.getLogger("routing").handlers if isinstance(h, logging.StreamHandler)
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "GET /geocode/search?api_key=SECRET123&text=Nowhere",
            f"GET /geocode/search?api_key={REDACTED}&text=Nowhere",
        ),
        ("apikey=abc&x=1", f"apikey={REDACTED}&x=1"),
        ("?access_token=tok.en-1", f"?access_token={REDACTED}"),
        ("(password=hunter2)", f"(password={REDACTED})"),
        ("no secrets here", "no secrets here"),
    ],
)
def test_redact_secrets_masks_credential_query_params(text, expected):
    assert redact_secrets(text) == expected


def test_redact_secrets_masks_the_configured_key_wherever_it_appears():
    with override_settings(ORS_API_KEY=FAKE_KEY):
        out = redact_secrets(f"Authorization: {FAKE_KEY} ... and again {FAKE_KEY}")
    assert out == f"Authorization: {REDACTED} ... and again {REDACTED}"


def test_filter_scrubs_message_args_and_traceback():
    try:
        raise requests.ConnectionError(
            f"Max retries exceeded with url: /geocode/search?api_key={FAKE_KEY}&text=x"
        )
    except requests.ConnectionError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "routing",
        logging.WARNING,
        __file__,
        1,
        "Routing %s failed: %s",
        ("GET", exc_info[1]),
        exc_info,
    )
    with override_settings(ORS_API_KEY=FAKE_KEY):
        assert RedactSecretsFilter().filter(record) is True
    message = record.getMessage()
    assert FAKE_KEY not in message and REDACTED in message
    assert record.args == ()  # pre-formatted, so '%' in URLs can no longer break formatting
    assert FAKE_KEY not in record.exc_text and REDACTED in record.exc_text


def test_filter_survives_a_broken_format_string():
    record = logging.LogRecord("routing", logging.INFO, __file__, 1, "%d items %s", ("x",), None)
    assert RedactSecretsFilter().filter(record) is True  # must not raise


def test_console_handler_is_wired_with_the_filter():
    assert "redact_secrets" in settings.LOGGING["handlers"]["console"]["filters"]
    assert any(isinstance(f, RedactSecretsFilter) for f in _console_handler().filters)


def test_geocode_connection_failure_writes_no_key_to_the_configured_log_stream():
    """End to end through the real configured handler: the retry warning for a
    failed geocode call (whose URL carries the key) is written with the key masked."""
    handler = _console_handler()
    logger = logging.getLogger("routing")
    stream = io.StringIO()
    old_stream, old_level = handler.setStream(stream), logger.level
    logger.setLevel(logging.WARNING)
    try:
        provider = ORSProvider(api_key=FAKE_KEY, base_url="http://ors.test")
        with responses.RequestsMock() as rsps, override_settings(ORS_API_KEY=FAKE_KEY):
            rsps.add(
                responses.GET,
                "http://ors.test/geocode/search",
                body=requests.ConnectionError(
                    "HTTPConnectionPool(host='ors.test', port=80): Max retries exceeded with "
                    f"url: /geocode/search?api_key={FAKE_KEY}&text=Nowhere%2C+ZZ"
                ),
            )
            assert provider.geocode("Nowhere, ZZ") is None
            assert len(rsps.calls) == 1  # geocode lookups are never retried
    finally:
        handler.setStream(old_stream)
        logger.setLevel(old_level)
    logged = stream.getvalue()
    assert "Routing GET failed" in logged
    assert FAKE_KEY not in logged
    assert f"api_key={REDACTED}" in logged
