"""Free routing providers behind a uniform interface.

Each provider makes exactly ONE HTTP request per route and returns the route as
a list of [lng, lat] coordinates plus total distance/duration in metric units.
The client layer converts to miles.

- OpenRouteService (ORS): robust, requires a free API key, returns GeoJSON.
- OSRM public server: keyless fallback so the app runs with zero setup.

Every external response is parsed defensively: a malformed-but-HTTP-200 body
(e.g. an HTML error page from a proxy) is turned into a RouteProviderError so it
surfaces as a clean 502 rather than an uncaught 500.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

from .exceptions import RouteProviderError

logger = logging.getLogger(__name__)

Coord = tuple[float, float]  # (lat, lng)

# Exceptions raised while parsing a 200 response body into our dataclass.
_PARSE_ERRORS = (ValueError, KeyError, IndexError, TypeError)


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
    "region", "macroregion", "country", "dependency", "macrohood", "coarse", "venue",
}


def _request(method: str, url: str, **kwargs):
    """HTTP request with one transport-level retry.

    Retries only on connect/timeout errors (transient); valid HTTP error
    responses are returned to the caller, never retried.
    """
    timeout = settings.ROUTING_TIMEOUT_SECONDS
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            return requests.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            logger.warning("Routing %s failed (attempt %d/2): %s", method, attempt, exc)
    raise last_exc  # type: ignore[misc]


class RouteProvider:
    name = "base"

    def route(self, start: Coord, end: Coord) -> ProviderRoute:  # pragma: no cover
        raise NotImplementedError

    # ORS offers a geocoder; OSRM does not. Default: no geocoding capability.
    def geocode(self, text: str) -> GeocodeHit | None:
        return None


class OSRMProvider(RouteProvider):
    name = "osrm"

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")

    def route(self, start: Coord, end: Coord) -> ProviderRoute:
        slat, slng = start
        elat, elng = end
        url = f"{self.base_url}/route/v1/driving/{slng},{slat};{elng},{elat}"
        try:
            resp = _request("GET", url, params={"overview": "full", "geometries": "geojson"})
        except requests.RequestException as exc:
            raise RouteProviderError(f"OSRM request failed: {exc}") from exc
        if resp.status_code != 200:
            raise RouteProviderError(f"OSRM HTTP {resp.status_code}")
        try:
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                raise RouteProviderError(f"OSRM returned no route: {data.get('code')}")
            route = data["routes"][0]
            return ProviderRoute(
                coordinates=route["geometry"]["coordinates"],
                distance_meters=float(route["distance"]),
                duration_seconds=float(route["duration"]),
            )
        except _PARSE_ERRORS as exc:
            raise RouteProviderError(f"OSRM returned an unparseable response: {exc}") from exc


class ORSProvider(RouteProvider):
    name = "ors"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else settings.ORS_API_KEY
        self.base_url = (base_url or settings.ORS_BASE_URL).rstrip("/")

    def _headers(self):
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def route(self, start: Coord, end: Coord) -> ProviderRoute:
        slat, slng = start
        elat, elng = end
        url = f"{self.base_url}/v2/directions/driving-car/geojson"
        body = {"coordinates": [[slng, slat], [elng, elat]]}
        try:
            resp = _request("POST", url, json=body, headers=self._headers())
        except requests.RequestException as exc:
            raise RouteProviderError(f"ORS request failed: {exc}") from exc
        if resp.status_code != 200:
            raise RouteProviderError(f"ORS HTTP {resp.status_code}")
        try:
            data = resp.json()
            features = data.get("features") or []
            if not features:
                raise RouteProviderError("ORS returned no route")
            feature = features[0]
            summary = feature["properties"]["summary"]
            return ProviderRoute(
                coordinates=feature["geometry"]["coordinates"],
                distance_meters=float(summary["distance"]),
                duration_seconds=float(summary["duration"]),
            )
        except _PARSE_ERRORS as exc:
            raise RouteProviderError(f"ORS returned an unparseable response: {exc}") from exc

    def geocode(self, text: str) -> GeocodeHit | None:
        """Pelias geocode fallback for endpoints not resolvable offline.

        Only confident, locality-level matches are accepted: a low-confidence or
        ``fallback`` result (e.g. an unknown town collapsing to a state centroid)
        is rejected so the caller returns a clean 400 instead of silently routing
        from the wrong place. ``ORS_GEOCODE_MIN_CONFIDENCE`` tunes the bar.

        NOTE: ORS's Pelias geocoder authenticates via the ``api_key`` *query*
        parameter (the Authorization header is not honored on this endpoint), so
        the key must travel in the query string. We never log the URL/params for
        this call to keep the key out of logs.
        """
        if not self.api_key:
            return None
        url = f"{self.base_url}/geocode/search"
        try:
            resp = _request(
                "GET",
                url,
                params={"api_key": self.api_key, "text": text, "boundary.country": "US", "size": 1},
            )
        except requests.RequestException:
            logger.warning("ORS geocode request failed for %r", text)
            return None
        if resp.status_code != 200:
            logger.warning("ORS geocode HTTP %s for %r", resp.status_code, text)
            return None
        try:
            features = (resp.json() or {}).get("features") or []
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
                    text, confidence, match_type, layer,
                )
                return None
            lng, lat = features[0]["geometry"]["coordinates"]
            return GeocodeHit(lat=lat, lng=lng, label=props.get("label", text), confidence=confidence)
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
