"""Free routing providers behind a uniform interface.

Each provider makes one HTTP request per route (plus at most one retry on a
transport error) and returns the route as a list of [lng, lat] coordinates plus
total distance/duration in metric units. The client layer converts to miles.

- OpenRouteService (ORS): robust, requires a free API key, returns GeoJSON.
- OSRM public server: keyless fallback so the app runs with zero setup.

Nothing in a provider response is trusted. Every payload goes through
``_build_route``, which rejects anything the corridor/optimizer cannot use
(fewer than two points, non-numeric or out-of-range coordinates, a non-positive
distance) and turns it into a ``RouteProviderError`` (HTTP 502). A provider that
worked but found no drivable route raises ``UnroutableError`` (HTTP 422) instead,
so the caller is not told to "retry later" for an impossible trip.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import requests
from django.conf import settings

from .exceptions import RouteProviderError, UnroutableError

logger = logging.getLogger(__name__)

Coord = tuple[float, float]  # (lat, lng)

# Exceptions raised while digging expected keys/indices out of a decoded body.
_PARSE_ERRORS = (ValueError, KeyError, IndexError, TypeError, AttributeError)

# Provider result codes meaning "no drivable route between these points".
_OSRM_UNROUTABLE_CODES = frozenset({"NoRoute", "NoSegment"})
_ORS_UNROUTABLE_CODES = frozenset({2009, 2010})  # route not found / point not routable

UNROUTABLE_MESSAGE = (
    "No drivable route exists between the start and finish "
    "(for example, they are separated by water or off the road network)."
)


@dataclass
class ProviderRoute:
    coordinates: list[list[float]]  # [[lng, lat], ...] (GeoJSON order)
    distance_meters: float
    duration_seconds: float


@dataclass
class GeocodeHit:
    lat: float
    lng: float
    label: str  # human-readable resolved place, e.g. "Toronto, OH, USA"
    confidence: float


# Pelias layers we refuse to treat as a routing endpoint:
#   - too coarse: an unknown town collapsing to a state/country centroid;
#   - a POI ("venue"): a named business, not a place — e.g. "Nowhere, ZZ"
#     matching a bar called "Nowhere" in NYC. Real cities are layer=locality.
# Place/address layers (locality, borough, localadmin, county, address, street,
# postalcode, ...) are accepted.
_REJECTED_GEOCODE_LAYERS = {
    "region",
    "macroregion",
    "country",
    "dependency",
    "macrohood",
    "coarse",
    "venue",
}


class RoutingDeadlineExceeded(requests.Timeout):
    """The request's total upstream time budget ran out before this call could start."""


def _request(
    method: str,
    url: str,
    *,
    deadline: float | None = None,
    retry: bool = True,
    retry_on_read_timeout: bool = True,
    **kwargs,
):
    """HTTP request with at most one transport-level retry, inside a time budget.

    ``deadline`` is a ``time.monotonic()`` timestamp: each attempt's timeout is
    the smaller of ``ROUTING_TIMEOUT_SECONDS`` and the time left, and no retry
    starts with less than a second left. Only connect/timeout errors are
    retried -- HTTP error responses are returned to the caller. A read timeout
    is not retried when ``retry_on_read_timeout`` is False (a POST the server
    may already have processed, e.g. ORS directions, which counts against a
    quota).
    """
    attempts = 2 if retry else 1
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        timeout = settings.ROUTING_TIMEOUT_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (attempt > 1 and remaining < 1.0):
                break
            timeout = min(timeout, remaining)
        try:
            return requests.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            logger.warning("Routing %s failed (attempt %d/%d): %s", method, attempt, attempts, exc)
            if isinstance(exc, requests.ReadTimeout) and not retry_on_read_timeout:
                break
    if last_exc is None:
        raise RoutingDeadlineExceeded("routing time budget exhausted before the call could start")
    raise last_exc


def _json_dict(resp: requests.Response) -> dict[str, Any] | None:
    """The response body as a dict, or None if it is not JSON or not an object
    (e.g. ``null``, a list, or an HTML error page from a proxy)."""
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _build_route(coordinates: Any, distance: Any, duration: Any, provider: str) -> ProviderRoute:
    """Validate and normalise a provider's raw route payload.

    Accepts a list of at least two ``[lng, lat]`` (or ``[lng, lat, elevation]``)
    numeric pairs, finite and within range, plus a positive finite distance and
    a non-negative finite duration. Anything else is a ``RouteProviderError``.
    """
    try:
        pts = np.asarray(coordinates, dtype=float)
    except (TypeError, ValueError) as exc:
        raise RouteProviderError(f"{provider} returned non-numeric route geometry") from exc
    if pts.ndim != 2 or pts.shape[1] < 2:
        raise RouteProviderError(f"{provider} returned malformed route geometry")
    if pts.shape[0] < 2:
        raise RouteProviderError(f"{provider} returned fewer than two route points")
    pts = pts[:, :2]  # drop elevation if present
    if (
        not np.isfinite(pts).all()
        or (np.abs(pts[:, 0]) > 180.0).any()
        or (np.abs(pts[:, 1]) > 90.0).any()
    ):
        raise RouteProviderError(f"{provider} returned out-of-range route coordinates")

    try:
        dist, dur = float(distance), float(duration)
    except (TypeError, ValueError) as exc:
        raise RouteProviderError(f"{provider} returned a non-numeric distance/duration") from exc
    if not (math.isfinite(dist) and dist > 0.0 and math.isfinite(dur) and dur >= 0.0):
        raise RouteProviderError(
            f"{provider} returned an invalid distance/duration ({distance!r}, {duration!r})"
        )
    return ProviderRoute(coordinates=pts.tolist(), distance_meters=dist, duration_seconds=dur)


class RouteProvider:
    """``deadline`` (a ``time.monotonic()`` timestamp) bounds every HTTP call."""

    name = "base"

    def route(self, start: Coord, end: Coord, deadline: float | None = None) -> ProviderRoute:
        raise NotImplementedError  # pragma: no cover

    # ORS offers a geocoder; OSRM does not. Default: no geocoding capability.
    def geocode(self, text: str, deadline: float | None = None) -> GeocodeHit | None:
        return None


class OSRMProvider(RouteProvider):
    name = "osrm"

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")

    def route(self, start: Coord, end: Coord, deadline: float | None = None) -> ProviderRoute:
        slat, slng = start
        elat, elng = end
        url = f"{self.base_url}/route/v1/driving/{slng},{slat};{elng},{elat}"
        try:
            resp = _request(  # an idempotent GET: safe to retry on any transport error
                "GET",
                url,
                deadline=deadline,
                params={"overview": "full", "geometries": "geojson"},
            )
        except requests.RequestException as exc:
            logger.warning("OSRM request failed: %s", exc)
            raise RouteProviderError(f"OSRM request failed ({type(exc).__name__})") from exc

        # OSRM reports "no route" as HTTP 400 with a code, so read the body first.
        data = _json_dict(resp)
        code = data.get("code") if data else None
        if code in _OSRM_UNROUTABLE_CODES:
            logger.info("OSRM found no route (%s): %s", code, data.get("message") if data else None)
            raise UnroutableError(UNROUTABLE_MESSAGE)
        if resp.status_code != 200:
            detail = f" ({code})" if code else ""
            raise RouteProviderError(f"OSRM HTTP {resp.status_code}{detail}")
        routes = data.get("routes") if data else None
        if code != "Ok" or not isinstance(routes, list) or not routes:
            raise RouteProviderError(f"OSRM returned no route (code={code!r})")
        try:
            route = routes[0]
            return _build_route(
                route["geometry"]["coordinates"], route["distance"], route["duration"], "OSRM"
            )
        except _PARSE_ERRORS as exc:
            logger.warning("OSRM returned an unparseable response: %r", exc)
            raise RouteProviderError("OSRM returned an unparseable response") from exc


class ORSProvider(RouteProvider):
    name = "ors"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else settings.ORS_API_KEY
        self.base_url = (base_url or settings.ORS_BASE_URL).rstrip("/")

    def _headers(self):
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def route(self, start: Coord, end: Coord, deadline: float | None = None) -> ProviderRoute:
        slat, slng = start
        elat, elng = end
        url = f"{self.base_url}/v2/directions/driving-car/geojson"
        # radiuses -1 = snap each endpoint to the nearest road however far it is.
        # ORS's default snapping radius is 350 m, which rejects small-town
        # centroids (our offline geocoder's coordinates) that sit a few hundred
        # metres off the road network as "not routable" (error 2010). OSRM snaps
        # without a limit, so this makes the two providers behave the same.
        body = {"coordinates": [[slng, slat], [elng, elat]], "radiuses": [-1, -1]}
        try:
            resp = _request(  # retry only if the request never reached the server
                "POST",
                url,
                deadline=deadline,
                retry_on_read_timeout=False,
                json=body,
                headers=self._headers(),
            )
        except requests.RequestException as exc:
            logger.warning("ORS request failed: %s", exc)
            raise RouteProviderError(f"ORS request failed ({type(exc).__name__})") from exc

        data = _json_dict(resp)
        if resp.status_code != 200:
            # ORS error bodies look like {"error": {"code": 2009, "message": ...}}.
            err = data.get("error") if data else None
            code = err.get("code") if isinstance(err, dict) else None
            if code in _ORS_UNROUTABLE_CODES:
                logger.info("ORS found no route (error %s): %s", code, err.get("message"))
                raise UnroutableError(UNROUTABLE_MESSAGE)
            detail = f" (error {code})" if code else ""
            raise RouteProviderError(f"ORS HTTP {resp.status_code}{detail}")
        features = data.get("features") if data else None
        if not isinstance(features, list) or not features:
            raise RouteProviderError("ORS returned no route")
        try:
            feature = features[0]
            summary = feature["properties"]["summary"]
            return _build_route(
                feature["geometry"]["coordinates"], summary["distance"], summary["duration"], "ORS"
            )
        except _PARSE_ERRORS as exc:
            logger.warning("ORS returned an unparseable response: %r", exc)
            raise RouteProviderError("ORS returned an unparseable response") from exc

    def geocode(self, text: str, deadline: float | None = None) -> GeocodeHit | None:
        """Pelias geocode fallback for endpoints not resolvable offline.

        Only confident, locality-level matches are accepted: a low-confidence or
        ``fallback`` result (e.g. an unknown town collapsing to a state centroid)
        is rejected so the caller returns a clean 400 instead of silently routing
        from the wrong place. ``ORS_GEOCODE_MIN_CONFIDENCE`` tunes the bar.

        NOTE: ORS's Pelias geocoder authenticates via the ``api_key`` *query*
        parameter (the Authorization header is not honored on this endpoint), so
        the key must travel in the query string. Any logged HTTP exception embeds
        that URL, which is why every log record is scrubbed by
        ``routing.redaction.RedactSecretsFilter`` (see ``LOGGING`` in settings).
        """
        if not self.api_key:
            return None
        url = f"{self.base_url}/geocode/search"
        try:
            resp = _request(  # a miss is only a 400 for the caller: not worth a retry
                "GET",
                url,
                deadline=deadline,
                retry=False,
                params={"api_key": self.api_key, "text": text, "boundary.country": "US", "size": 1},
            )
        except requests.RequestException:
            logger.warning("ORS geocode request failed for %r", text)
            return None
        if resp.status_code != 200:
            logger.warning("ORS geocode HTTP %s for %r", resp.status_code, text)
            return None
        try:
            data = _json_dict(resp) or {}
            features = data.get("features") or []
            if not features:
                return None
            props = features[0].get("properties", {})
            confidence = float(props.get("confidence") or 0.0)
            match_type = props.get("match_type")
            layer = props.get("layer")
            if (
                match_type == "fallback"
                or layer in _REJECTED_GEOCODE_LAYERS
                or confidence < settings.ORS_GEOCODE_MIN_CONFIDENCE
            ):
                logger.info(
                    "ORS geocode rejected %r (confidence=%s match_type=%s layer=%s)",
                    text,
                    confidence,
                    match_type,
                    layer,
                )
                return None
            lng, lat = (float(v) for v in features[0]["geometry"]["coordinates"][:2])
            return GeocodeHit(
                lat=lat, lng=lng, label=props.get("label", text), confidence=confidence
            )
        except _PARSE_ERRORS:
            logger.warning("ORS geocode returned an unparseable response for %r", text)
            return None


def get_provider() -> RouteProvider:
    """Pick the provider from settings: explicit override, else ORS if a key is
    configured, else the keyless OSRM public server."""
    name = (settings.ROUTING_PROVIDER or "").strip().lower()
    if not name:
        name = "ors" if settings.ORS_API_KEY else "osrm"
    if name == "ors":
        return ORSProvider()
    return OSRMProvider()
