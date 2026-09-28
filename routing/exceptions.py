"""Exceptions raised by the routing layer, mapped to HTTP responses in views."""


class RoutingError(Exception):
    """Base class for routing failures."""


class EndpointResolutionError(RoutingError):
    """A start/finish location could not be resolved to coordinates."""


class OutsideUSAError(RoutingError):
    """A resolved endpoint falls outside the USA."""


class SameEndpointError(RoutingError):
    """Start and finish resolve to the same location."""


class RouteProviderError(RoutingError):
    """The external routing provider failed or returned an unusable response."""
