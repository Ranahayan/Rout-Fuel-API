"""US state constants shared by the loader and endpoint validation."""

# 50 states + DC. Used to drop the ~620 Canadian-province rows in the fuel CSV
# (AB/BC/ON/SK/MB/...) and to keep the data USA-only per the assignment.
US_STATES = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    }
)

# Generous bounding box covering the contiguous US, Alaska, and Hawaii. Used to
# validate that resolved route endpoints fall within the USA.
US_BBOX = {
    "min_lat": 18.0,  # south of Hawaii
    "max_lat": 72.0,  # northern Alaska
    "min_lng": -180.0,  # Aleutians wrap
    "max_lng": -66.0,  # eastern Maine
}


def in_usa(lat: float, lng: float) -> bool:
    return (
        US_BBOX["min_lat"] <= lat <= US_BBOX["max_lat"]
        and US_BBOX["min_lng"] <= lng <= US_BBOX["max_lng"]
    )
