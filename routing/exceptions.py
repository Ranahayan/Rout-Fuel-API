"""Exceptions raised by the routing layer. They know nothing about HTTP; the
status code and error code for each lives in ``trips.api_errors.DOMAIN_ERRORS``."""


class RoutingError(Exception):
    """Base class for routing failures."""


class EndpointResolutionError(RoutingError):
    """A start/finish location could not be resolved to coordinates."""


class OutsideUSAError(RoutingError):
    """A resolved endpoint falls outside the USA."""


class SameEndpointError(RoutingError):
    """Start and finish resolve to the same location."""


class UnroutableError(RoutingError):
    """The provider found no drivable route between the endpoints (e.g. an island
    to the mainland). The provider worked; the request cannot be fulfilled."""


class RouteProviderError(RoutingError):
    """The external routing provider failed or returned an unusable response."""
