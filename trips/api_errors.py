"""One error envelope for the whole HTTP layer.

Every failed response -- a domain error, a validation error, a throttle, a
wrong method or media type, malformed JSON -- looks the same to a client::

    {"error": "<human-readable message>", "code": "<machine-readable code>", ...}

with optional extras (``fields`` for validation, ``infeasible_segment`` and
``hint`` for an unplannable route). The domain layers (``routing``,
``trips.optimizer``, ``trips.services``) raise plain exceptions and know nothing
about HTTP; ``DOMAIN_ERRORS`` below is the single place where they are given a
status and a code. ``api_exception_handler`` is wired in via
``REST_FRAMEWORK["EXCEPTION_HANDLER"]``; the map page reuses ``error_response``
so both entry points always agree.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from routing.exceptions import (
    EndpointResolutionError,
    OutsideUSAError,
    RouteProviderError,
    SameEndpointError,
    UnroutableError,
)

from .optimizer import InfeasibleRouteError
from .serializers import BUFFER_MILES_MAX, describe_errors
from .services import NoStationsLoadedError

logger = logging.getLogger(__name__)

# Domain exception -> (HTTP status, error code). Order matters only for subclasses.
DOMAIN_ERRORS: dict[type[Exception], tuple[int, str]] = {
    EndpointResolutionError: (status.HTTP_400_BAD_REQUEST, "unresolvable_endpoint"),
    OutsideUSAError: (status.HTTP_400_BAD_REQUEST, "outside_usa"),
    SameEndpointError: (status.HTTP_400_BAD_REQUEST, "same_endpoint"),
    # The provider worked but there is no drivable route: the request cannot be
    # fulfilled -- the client's situation (422), not a gateway fault.
    UnroutableError: (status.HTTP_422_UNPROCESSABLE_ENTITY, "unroutable"),
    InfeasibleRouteError: (status.HTTP_422_UNPROCESSABLE_ENTITY, "infeasible"),
    # Not the client's problem: the server has no station data at all.
    NoStationsLoadedError: (status.HTTP_503_SERVICE_UNAVAILABLE, "no_stations_loaded"),
    RouteProviderError: (status.HTTP_502_BAD_GATEWAY, "provider_error"),
}

BUFFER_HINT = "Increase buffer_miles to admit more stations near the route."


def error_response(exc: Exception) -> tuple[dict, int] | None:
    """(payload, status) for a known domain exception, else None."""
    for exc_type, (status_code, code) in DOMAIN_ERRORS.items():
        if not isinstance(exc, exc_type):
            continue
        payload: dict = {"error": str(exc), "code": code}
        if isinstance(exc, InfeasibleRouteError):
            payload["infeasible_segment"] = {
                "from_mile": round(exc.segment_start, 1),
                "to_mile": round(exc.segment_end, 1),
            }
            if exc.buffer_miles is None or exc.buffer_miles < BUFFER_MILES_MAX:
                payload["hint"] = BUFFER_HINT  # only suggest what can still be tried
        elif isinstance(exc, RouteProviderError):
            payload["error"] = f"Routing provider error: {exc}"
        return payload, status_code
    return None


def api_exception_handler(exc: Exception, context: dict) -> Response | None:
    """DRF exception handler: domain errors first, then DRF's own exceptions
    re-shaped into the common envelope. Returns None for anything unexpected so
    Django still produces a 500 (and logs the traceback)."""
    domain = error_response(exc)
    if domain is not None:
        payload, status_code = domain
        if status_code >= 500:
            request = context.get("request")
            path = request.get_full_path() if request is not None else "?"
            logger.warning("%s for %s: %s", payload["code"], path, exc)
        return Response(payload, status=status_code)

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, ValidationError):
        fields = (
            response.data
            if isinstance(response.data, dict)
            else {"non_field_errors": response.data}
        )
        response.data = {
            "error": describe_errors(fields),
            "code": "invalid_input",
            "fields": fields,
        }
    else:
        detail = (
            response.data.get("detail", response.data)
            if isinstance(response.data, dict)
            else response.data
        )
        code = getattr(detail, "code", None) or getattr(exc, "default_code", "error")
        response.data = {"error": str(detail), "code": str(code)}
    return response


__all__ = ["DOMAIN_ERRORS", "APIException", "api_exception_handler", "error_response"]
