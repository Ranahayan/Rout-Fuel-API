"""Request validation shared by the JSON endpoint and the map page.

Every user-supplied value is bounded here, before any geocoding or routing work:
place names are capped in length (they are looked up offline and, with ORS,
forwarded to a third-party geocoder), coordinates must be finite and in range,
and ``buffer_miles`` must be a finite number inside the allowed window.
"""

from __future__ import annotations

import math

from rest_framework import serializers

PLACE_NAME_MAX_LENGTH = 100  # "Rancho Santa Margarita, CA" is 26; 100 leaves ample room
BUFFER_MILES_MIN, BUFFER_MILES_MAX = 0.5, 50.0


class EndpointField(serializers.Field):
    """Accepts either a "City, ST" string or a {"lat":.., "lng":..} object."""

    def to_internal_value(self, data):
        if isinstance(data, dict):
            try:
                lat, lng = float(data["lat"]), float(data["lng"])
            except (KeyError, TypeError, ValueError) as exc:
                raise serializers.ValidationError(
                    "Coordinate object must have numeric 'lat' and 'lng'."
                ) from exc
            if not (math.isfinite(lat) and math.isfinite(lng)):
                raise serializers.ValidationError("'lat' and 'lng' must be finite numbers.")
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                raise serializers.ValidationError(
                    "'lat' must be within [-90, 90] and 'lng' within [-180, 180]."
                )
            return {"lat": lat, "lng": lng}
        if isinstance(data, str) and data.strip():
            text = data.strip()
            if len(text) > PLACE_NAME_MAX_LENGTH:
                raise serializers.ValidationError(
                    f"Place name is too long (max {PLACE_NAME_MAX_LENGTH} characters)."
                )
            # A numeric "lat,lng" string means coordinates in a JSON body exactly as
            # it does in a query string (same range/finite checks apply).
            parsed = parse_query_endpoint(text)
            return self.to_internal_value(parsed) if isinstance(parsed, dict) else text
        raise serializers.ValidationError(
            "Provide a 'City, ST' string or a {'lat':.., 'lng':..} object."
        )

    def to_representation(self, value):
        return value


class RouteFuelPlanRequestSerializer(serializers.Serializer):
    start = EndpointField()
    finish = EndpointField()
    buffer_miles = serializers.FloatField(
        required=False, min_value=BUFFER_MILES_MIN, max_value=BUFFER_MILES_MAX
    )

    def validate_buffer_miles(self, value):
        # Defense in depth: DRF's FloatField rejects the strings "nan"/"inf", but a
        # NaN float compares False against every bound, so the min/max validators
        # would let one through if it ever arrived (e.g. via a lenient JSON parser).
        if not math.isfinite(value):
            raise serializers.ValidationError("buffer_miles must be a finite number.")
        return value


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


def query_payload(query_params) -> dict:
    """Build the serializer input from GET query parameters, omitting absent keys
    so the serializer reports them as required. Values are passed through as
    strings; ``EndpointField`` parses "lat,lng" the same way for GET and POST."""
    raw = {}
    for key in ("start", "finish"):
        value = query_params.get(key)
        if value:
            raw[key] = value
    buffer_miles = query_params.get("buffer_miles")
    if buffer_miles:
        raw["buffer_miles"] = buffer_miles
    return raw


def describe_errors(errors) -> str:
    """Flatten DRF field errors into one human-readable sentence (for the map page)."""
    return " ".join(
        f"{field}: {' '.join(str(m) for m in messages)}" for field, messages in errors.items()
    )
