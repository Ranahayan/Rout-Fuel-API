"""Single-call routing client.

Resolves start/finish to coordinates (offline first, provider geocoder only as a
last resort), validates they are in the USA, and makes exactly ONE routing call
per uncached request. Route geometry is cached so repeated requests cost zero
provider calls, and concurrent identical requests share a single in-flight call
("single-flight"): the first one fetches, the others wait for its cached result.

Failures are remembered too: an unroutable pair is deterministic and is cached
like a success (for the normal cache TTL); a provider error is transient and is
cached only for ``ROUTING_FAILURE_CACHE_SECONDS`` so a flapping provider is not
hammered by retries of the same trip.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from stations.boundary import in_usa
from stations.geocoding import get_default_geocoder
from trips.geo import METERS_PER_MILE

from .exceptions import (
    EndpointResolutionError,
    OutsideUSAError,
    RouteProviderError,
    SameEndpointError,
    UnroutableError,
)
from .providers import ORSProvider, OSRMProvider, RouteProvider, get_provider

logger = logging.getLogger(__name__)

Coord = tuple[float, float]  # (lat, lng)

# Single-flight lock lifetime: the leader's whole upstream budget plus a margin,
# so a live leader is never overtaken, yet a crashed leader only stalls followers
# briefly.
_LOCK_MARGIN_SECONDS = 2.0
_WAIT_POLL_SECONDS = 0.05


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


@dataclass(frozen=True)
class _CachedFailure:
    """A remembered routing failure for a (start, finish, provider) key."""

    unroutable: bool  # True: no drivable route (deterministic); False: provider error
    message: str

    def reraise(self) -> None:
        if self.unroutable:
            raise UnroutableError(self.message)
        raise RouteProviderError(self.message)


def get_provider_chain() -> list[RouteProvider]:
    """The configured provider first, then -- when ``ROUTING_FALLBACK`` is on --
    the other real provider if it is usable (OSRM needs no key; ORS needs
    ``ORS_API_KEY``). The next provider is tried only after a provider *failure*,
    never for an unroutable trip."""
    primary = get_provider()
    chain: list[RouteProvider] = [primary]
    if settings.ROUTING_FALLBACK:
        if primary.name == "ors":
            chain.append(OSRMProvider())
        elif primary.name == "osrm" and settings.ORS_API_KEY:
            chain.append(ORSProvider())
    return chain


def resolve_endpoint(
    value, provider: RouteProvider, deadline: float | None = None
) -> ResolvedEndpoint:
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
    geocoder = get_default_geocoder()
    coords = geocoder.geocode_place_name(text)
    if coords is not None:
        return ResolvedEndpoint(coords[0], coords[1])  # exact offline match

    # A bare city that exists in several states is ambiguous, not unknown: say so
    # (and spend no provider call guessing).
    if "," not in text:
        states = geocoder.states_for_city(text)
        if len(states) > 1:
            raise EndpointResolutionError(
                f"'{text}' is a city in several states ({', '.join(states)}); "
                f"add the state, e.g. '{text}, {states[0]}'."
            )

    hit = provider.geocode(text, deadline=deadline)  # confident fuzzy match, or None
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
    return "route:" + hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


def _cached_route(key: str) -> RouteResult | None:
    """Cached RouteResult for ``key``, None on a miss; re-raises a cached failure."""
    cached = cache.get(key)
    if isinstance(cached, _CachedFailure):
        cached.reraise()
    return cached


def _fetch_and_cache(
    providers: list[RouteProvider],
    key: str,
    start_ep: ResolvedEndpoint,
    finish_ep: ResolvedEndpoint,
    deadline: float,
) -> RouteResult:
    """The routing call(s), with the outcome (success or failure) cached.

    Providers are tried in order; the next one is used only after a provider
    *failure* (outage, quota, unusable response) and only while the time budget
    lasts. An unroutable trip is final: the road network is the same everywhere.
    """
    start_coord, finish_coord = start_ep.coord, finish_ep.coord
    pr = None
    used = providers[0]
    failure: RouteProviderError | None = None
    for i, provider in enumerate(providers):
        logger.info("Routing call via %s: %s -> %s", provider.name, start_coord, finish_coord)
        try:
            pr = provider.route(start_coord, finish_coord, deadline=deadline)
            used = provider
            break
        except UnroutableError as exc:
            cache.set(key, _CachedFailure(unroutable=True, message=str(exc)))
            raise
        except RouteProviderError as exc:
            failure = exc
            has_next = i + 1 < len(providers)
            if not (has_next and time.monotonic() < deadline):
                break  # nothing left to try, or no time left to try it
            logger.warning(
                "Provider %s failed (%s); falling back to %s",
                provider.name,
                exc,
                providers[i + 1].name,
            )
    if pr is None:
        assert failure is not None
        cache.set(
            key,
            _CachedFailure(unroutable=False, message=str(failure)),
            timeout=settings.ROUTING_FAILURE_CACHE_SECONDS,
        )
        raise failure

    result = RouteResult(
        geometry={"type": "LineString", "coordinates": pr.coordinates},
        points=[(lat, lng) for lng, lat in pr.coordinates],
        total_distance_miles=pr.distance_meters / METERS_PER_MILE,
        duration_minutes=pr.duration_seconds / 60.0,
        provider=used.name,
        start=start_coord,
        finish=finish_coord,
        start_resolved_from=start_ep.resolved_from,
        finish_resolved_from=finish_ep.resolved_from,
    )
    cache.set(key, result)
    return result


def get_route(start, finish) -> RouteResult:
    """Resolve endpoints, validate USA bounds, and fetch the route (1 call).

    Everything upstream -- geocode fallbacks, the routing call, its retry and a
    fallback provider -- shares one wall-clock budget,
    ``ROUTING_TOTAL_DEADLINE_SECONDS``, so a request can never outlive the web
    server's worker timeout.
    """
    deadline = time.monotonic() + settings.ROUTING_TOTAL_DEADLINE_SECONDS
    providers = get_provider_chain()
    primary = providers[0]
    start_ep = resolve_endpoint(start, primary, deadline)
    finish_ep = resolve_endpoint(finish, primary, deadline)
    start_coord, finish_coord = start_ep.coord, finish_ep.coord

    for label, coord in (("start", start_coord), ("finish", finish_coord)):
        if not in_usa(*coord):
            raise OutsideUSAError(f"The {label} location is outside the USA.")

    if _rounded(start_coord) == _rounded(finish_coord):
        raise SameEndpointError("Start and finish must be different locations.")

    key = _cache_key(start_coord, finish_coord, primary.name)
    cached = _cached_route(key)
    if cached is not None:
        logger.debug("Route cache hit (%s)", key)
        return cached

    # Single-flight: whoever wins the lock fetches; everyone else waits for the
    # cached outcome instead of making their own provider call.
    lock_key = key + ":lock"
    lock_ttl = settings.ROUTING_TOTAL_DEADLINE_SECONDS + _LOCK_MARGIN_SECONDS
    if cache.add(lock_key, 1, timeout=lock_ttl):
        try:
            return _fetch_and_cache(providers, key, start_ep, finish_ep, deadline)
        finally:
            cache.delete(lock_key)

    while time.monotonic() < deadline:
        time.sleep(_WAIT_POLL_SECONDS)
        cached = _cached_route(key)  # raises if the leader cached a failure
        if cached is not None:
            logger.debug("Route cache hit after waiting for an in-flight call (%s)", key)
            return cached
        if cache.get(lock_key) is None:
            break  # the leader is gone without caching anything (e.g. its process died)

    cached = _cached_route(key)
    if cached is not None:
        return cached
    # Last resort: fetch ourselves with whatever budget is left.
    return _fetch_and_cache(providers, key, start_ep, finish_ep, deadline)
