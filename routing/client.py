"""Single-call routing client.

Resolves start/finish to coordinates (offline first, provider geocoder only as a
last resort), validates they are in the USA, and makes exactly ONE routing call
per uncached request. Route geometry is cached so repeated requests cost zero
provider calls.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from django.core.cache import cache

from stations.constants import in_usa
from stations.geocoding import get_default_geocoder
from trips.geo import METERS_PER_MILE

from .exceptions import EndpointResolutionError, OutsideUSAError, SameEndpointError
from .providers import RouteProvider, get_provider

logger = logging.getLogger(__name__)

Coord = tuple[float, float]  # (lat, lng)


@dataclass
class ResolvedEndpoint:
    lat: float
    lng: float
    # Human-readable place set ONLY when a fuzzy provider geocode was used (so
    # the response can disclose where a place name actually resolved to). None
    # for explicit coordinates or an exact offline (City, ST) match.
    resolved_from: str | None = None

    @property
    def coord(self) -> Coord:
        return (self.lat, self.lng)


@dataclass
class RouteResult:
    geometry: dict  # GeoJSON LineString {"type": "LineString", "coordinates": [[lng,lat],...]}
    points: list[tuple[float, float]]  # [(lat, lng), ...] in travel order
    total_distance_miles: float
    duration_minutes: float
    provider: str
    start: Coord
    finish: Coord
    start_resolved_from: str | None = None
    finish_resolved_from: str | None = None


def resolve_endpoint(value, provider: RouteProvider) -> ResolvedEndpoint:
    """Resolve an endpoint.

    ``value`` may be a {"lat":..,"lng":..} mapping (0 calls) or a "City, ST"
    string (offline geocode, 0 calls; confident provider geocode fallback, 1 call).
    """
    if isinstance(value, dict):
        try:
            return ResolvedEndpoint(float(value["lat"]), float(value["lng"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise EndpointResolutionError(
                "Endpoint coordinates must be {'lat':.., 'lng':..}"
            ) from exc

    text = str(value).strip()
    coords = get_default_geocoder().geocode_place_name(text)
    if coords is not None:
        return ResolvedEndpoint(coords[0], coords[1])  # exact offline match

    hit = provider.geocode(text)  # confident fuzzy match, or None (no-op for OSRM)
    if hit is not None:
        return ResolvedEndpoint(hit.lat, hit.lng, resolved_from=hit.label)

    raise EndpointResolutionError(
        f"Could not resolve '{text}'. Provide coordinates or a US 'City, ST'."
    )


def _rounded(coord: Coord) -> tuple[float, float]:
    """Coordinates rounded to ~11 m, used to detect identical endpoints and to
    key the cache consistently."""
    return (round(coord[0], 4), round(coord[1], 4))


def _cache_key(start: Coord, finish: Coord, provider_name: str) -> str:
    raw = (
        f"{provider_name}|{round(start[0], 4)},{round(start[1], 4)}|"
        f"{round(finish[0], 4)},{round(finish[1], 4)}"
    )
    return "route:" + hashlib.md5(raw.encode()).hexdigest()


def get_route(start, finish) -> RouteResult:
    """Resolve endpoints, validate USA bounds, and fetch the route (1 call)."""
    provider = get_provider()
    start_ep = resolve_endpoint(start, provider)
    finish_ep = resolve_endpoint(finish, provider)
    start_coord, finish_coord = start_ep.coord, finish_ep.coord

    for label, coord in (("start", start_coord), ("finish", finish_coord)):
        if not in_usa(*coord):
            raise OutsideUSAError(f"The {label} location is outside the USA.")

    if _rounded(start_coord) == _rounded(finish_coord):
        raise SameEndpointError("Start and finish must be different locations.")

    key = _cache_key(start_coord, finish_coord, provider.name)
    cached = cache.get(key)
    if cached is not None:
        logger.debug("Route cache hit (%s)", key)
        return cached

    logger.info("Routing call via %s: %s -> %s", provider.name, start_coord, finish_coord)
    pr = provider.route(start_coord, finish_coord)
    coords_latlng = [(lat, lng) for lng, lat in pr.coordinates]
    result = RouteResult(
        geometry={"type": "LineString", "coordinates": pr.coordinates},
        points=coords_latlng,
        total_distance_miles=pr.distance_meters / METERS_PER_MILE,
        duration_minutes=pr.duration_seconds / 60.0,
        provider=provider.name,
        start=start_coord,
        finish=finish_coord,
        start_resolved_from=start_ep.resolved_from,
        finish_resolved_from=finish_ep.resolved_from,
    )
    cache.set(key, result)
    return result
