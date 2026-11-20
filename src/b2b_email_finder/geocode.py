"""Step 1a: turn a place name ("Köln", "Bayern") into an Overpass area id.

We ask Nominatim (OpenStreetMap's free geocoder) for the administrative boundary
with that name. Overpass identifies an area by `3600000000 + relation id`.

Nominatim usage policy: at most 1 request per second and an identifying
User-Agent (https://operations.osmfoundation.org/policies/nominatim/). We make one
request per run.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
AREA_ID_OFFSET = 3_600_000_000
GERMANY_RELATION_ID = 51477
_GERMANY_ALIASES = {"germany", "deutschland", "de"}


class AreaNotFound(Exception):
    pass


GERMANY_BBOX = (5.87, 47.27, 15.04, 55.06)


@dataclass(frozen=True)
class Area:
    query: str
    display_name: str
    area_id: int
    bbox: tuple[float, float, float, float] | None = None  # (west, south, east, north)


def _bbox(hit: dict) -> tuple[float, float, float, float] | None:
    box = hit.get("boundingbox")  # Nominatim: [south, north, west, east] as strings
    if not box or len(box) != 4:
        return None
    south, north, west, east = (float(v) for v in box)
    return (west, south, east, north)


async def resolve_area(client: httpx.AsyncClient, name: str) -> Area:
    if name.strip().lower() in _GERMANY_ALIASES:
        return Area(name, "Deutschland", AREA_ID_OFFSET + GERMANY_RELATION_ID, GERMANY_BBOX)

    response = await client.get(
        NOMINATIM_URL,
        params={"q": name, "countrycodes": "de", "format": "jsonv2", "limit": 10},
    )
    response.raise_for_status()
    for hit in response.json():
        # Only administrative boundaries (relations) work as Overpass search areas.
        if hit.get("osm_type") == "relation" and hit.get("category") == "boundary":
            return Area(name, hit["display_name"], AREA_ID_OFFSET + int(hit["osm_id"]), _bbox(hit))
    raise AreaNotFound(
        f"No German administrative area called {name!r} found. "
        "Try a city, district or state name, e.g. 'Köln', 'Landkreis Harburg', 'Bayern'."
    )
