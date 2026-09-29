"""Keep secrets out of the logs.

The ORS geocoder authenticates with the API key as a *query parameter*, so any
logged ``requests`` exception (whose text embeds the failing URL) would leak the
key. ``RedactSecretsFilter`` is attached to the console log handler in
``config.settings.LOGGING`` and scrubs every record that passes through it:
key-like query parameters and the literal configured ORS key, in the message
and in any attached traceback.
"""

from __future__ import annotations

import logging
import re
import traceback

from django.conf import settings

REDACTED = "[REDACTED]"

# name=value pairs whose name looks like a credential (query strings, log lines).
_KEY_PARAM = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)=([^&\s'\"<>)\]]+)"
)


def redact_secrets(text: str) -> str:
    """Mask credential-looking query parameters and the configured ORS key."""
    out = _KEY_PARAM.sub(rf"\1={REDACTED}", text)
    key = getattr(settings, "ORS_API_KEY", "") or ""
    if len(key) >= 8:  # never blank out tiny/common strings
        out = out.replace(key, REDACTED)
    return out


class RedactSecretsFilter(logging.Filter):
    """Format the record once, then scrub secrets from the message and traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must never break logging itself
            message = str(record.msg)
        record.msg = redact_secrets(message)
        record.args = ()  # already formatted; '%' characters in the text are now literal
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_secrets("".join(traceback.format_exception(*record.exc_info)))
        return True
