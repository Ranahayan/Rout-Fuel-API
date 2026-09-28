"""Request validation for the route-fuel-plan endpoint."""

from __future__ import annotations

from rest_framework import serializers


class EndpointField(serializers.Field):
    """Accepts either a "City, ST" string or a {"lat":.., "lng":..} object."""

    def to_internal_value(self, data):
        if isinstance(data, dict):
            try:
                return {"lat": float(data["lat"]), "lng": float(data["lng"])}
            except (KeyError, TypeError, ValueError) as exc:
                raise serializers.ValidationError(
                    "Coordinate object must have numeric 'lat' and 'lng'."
                ) from exc
        if isinstance(data, str) and data.strip():
            return data.strip()
        raise serializers.ValidationError(
            "Provide a 'City, ST' string or a {'lat':.., 'lng':..} object."
        )

    def to_representation(self, value):
        return value


class RouteFuelPlanRequestSerializer(serializers.Serializer):
    start = EndpointField()
    finish = EndpointField()
    buffer_miles = serializers.FloatField(required=False, min_value=0.5, max_value=50.0)


def parse_query_endpoint(text: str):
    """Parse a query-string endpoint: numeric "lat,lng" -> dict, else place name."""
    text = (text or "").strip()
    parts = [p.strip() for p in text.split(",")]
    if len(parts) == 2:
        try:
            return {"lat": float(parts[0]), "lng": float(parts[1])}
        except ValueError:
            pass
    return text
