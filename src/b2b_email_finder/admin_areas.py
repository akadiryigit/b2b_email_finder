"""Step 1c: in which city / district (Stadtbezirk) / Stadtteil is each business?

Address tags rarely say which district a place is in, but OpenStreetMap knows the
administrative boundaries. Overpass's `is_in` statement returns every area that
contains a point, so for each business we ask "which administrative areas contain
you?" and keep the names per `admin_level`.

German admin levels (https://wiki.openstreetmap.org/wiki/DE:Grenze#Verwaltungsgrenzen):
    4  Bundesland                  (Berlin, Hamburg are city-states at this level)
    6  Landkreis / kreisfreie Stadt
    8  Gemeinde / Stadt
    9  Stadtbezirk                 (Köln-Ehrenfeld, Berlin-Mitte)
    10 Stadtteil / Ortsteil        (Neuehrenfeld, Moabit)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .models import Business
from .cache import DiskCache
from .overpass import OverpassError, fetch_overpass

log = logging.getLogger(__name__)

LEVELS = ("4", "6", "8", "9", "10")
CITY_STATES = frozenset({"Berlin", "Hamburg"})
CHUNK_SIZE = 250  # businesses per Overpass request, keeps each query fast

Key = tuple[str, int | str]
_AREA_FILTER = f'["boundary"="administrative"]["admin_level"~"^({"|".join(LEVELS)})$"]'


@dataclass(frozen=True)
class Place:
    city: str | None
    district: str | None
    suburb: str | None


def build_admin_query(businesses: list[Business], timeout: int = 180) -> str:
    """OSM places are looked up by id, places from other sources by coordinates."""
    ids: dict[str, list[str]] = {"node": [], "way": [], "relation": []}
    points = []
    for business in businesses:
        if business.is_osm:
            ids[business.osm_type].append(str(business.osm_id))
        elif business.lat is not None and business.lon is not None:
            points.append(business)

    lines = [f"[out:json][timeout:{timeout}];"]
    if any(ids.values()):
        lines += ["("]
        lines += [f"  {t}(id:{','.join(v)});" for t, v in ids.items() if v]
        lines += [
            ")->.items;",
            # For every business: print its id, then the admin areas that contain it.
            "foreach.items->.it(",
            "  .it out ids;",
            # is_in works on nodes; for buildings (ways) use the way's nodes.
            "  (node.it; node(w.it); way(r.it); node(w);)->.points;",
            "  .points is_in->.containing;",
            f"  area.containing{_AREA_FILTER};",
            "  out tags;",
            ");",
        ]
    for business in points:
        marker = f"{business.osm_type}|{business.osm_id}".replace('"', "")
        lines += [
            f"is_in({business.lat:.7f},{business.lon:.7f})->.containing;",
            f'make pt idx="{marker}"; out;',  # marker element: "the next areas belong to ..."
            f"area.containing{_AREA_FILTER}; out tags;",
        ]
    return "\n".join(lines)


def parse_admin_response(data: dict) -> dict[Key, dict[str, str]]:
    """The response is a flat list: business, its areas, next business, its areas, ...

    Per business: {admin_level: name, ..., "id:<area id>": name, ...}. The "id:" entries
    let the pipeline check whether a place lies inside the searched area.
    """
    result: dict[Key, dict[str, str]] = {}
    current: Key | None = None
    for element in data.get("elements", []):
        if element["type"] in ("node", "way", "relation"):
            current = (element["type"], element["id"])
            result.setdefault(current, {})
        elif element["type"] == "pt":
            source, _, place_id = (element.get("tags") or {}).get("idx", "").partition("|")
            current = (source, place_id)
            result.setdefault(current, {})
        elif element["type"] == "area" and current is not None:
            tags = element.get("tags") or {}
            level, name = tags.get("admin_level"), tags.get("name")
            if level and name:
                result[current].setdefault(level, name)
                result[current][f"id:{element['id']}"] = name
    return result


def _cache_key(business: Business) -> str:
    if business.is_osm:
        return f"{business.osm_type}/{business.osm_id}"
    return f"point/{business.lat:.6f},{business.lon:.6f}"


def in_area(levels: dict[str, str], area_id: int) -> bool:
    return f"id:{area_id}" in levels


async def fetch_admin_levels(
    client: httpx.AsyncClient, businesses: list[Business], cache_dir=None
) -> dict[Key, dict[str, str]]:
    """Looks places up in chunks. A failed chunk never loses the others: it is retried
    once at the end, and places still missing simply have no entry in the result."""
    # Saved per place (boundaries rarely change), so a different mix of places in a
    # later run never repeats a lookup.
    cache = DiskCache(cache_dir / "admin" if cache_dir else None)
    result: dict[Key, dict[str, str]] = {}
    todo = []
    for business in businesses:
        cached = cache.get(_cache_key(business), max_age_days=90)
        if cached is not None:
            result[business.key] = cached
        else:
            todo.append(business)
    if len(todo) < len(businesses):
        log.info("City/district: %d places from cache, %d to look up", len(businesses) - len(todo), len(todo))
    chunks = [todo[i:i + CHUNK_SIZE] for i in range(0, len(todo), CHUNK_SIZE)]

    async def lookup(chunk: list[Business], attempts: int) -> bool:
        try:
            data = await fetch_overpass(client, build_admin_query(chunk), cache_dir=cache_dir, attempts=attempts)
        except (OverpassError, httpx.HTTPError) as exc:
            log.warning("City/district lookup for %d places failed: %s", len(chunk), exc)
            return False
        # An empty answer (no areas at all) comes from an overloaded server, not from a
        # place outside every boundary: it is treated as "unknown" and never saved.
        found = {key: levels for key, levels in parse_admin_response(data).items() if levels}
        for business in chunk:
            if business.key in found:
                cache.set(_cache_key(business), found[business.key])
        result.update(found)
        return True

    failed = []
    for number, chunk in enumerate(chunks, start=1):
        if not await lookup(chunk, attempts=2):
            failed.append(chunk)
        if number % 4 == 0 or number == len(chunks):
            log.info("City/district lookup: %d/%d parts done, %d failed", number, len(chunks), len(failed))
    still_failed = [chunk for chunk in failed if not await lookup(chunk, attempts=3)]
    if still_failed:
        log.warning("No city/district for %d places (map server unavailable); re-run later to fill them in",
                    sum(len(c) for c in still_failed))
    return result


def _is_county(name: str) -> bool:
    lowered = name.lower()
    return "kreis" in lowered or lowered.startswith(("region ", "städteregion "))


def resolve_place(levels: dict[str, str], addr_city: str | None, addr_suburb: str | None) -> Place:
    """Pick the most useful city / district / Stadtteil names for one business."""
    city = levels.get("8") or addr_city
    if not city and levels.get("6") and not _is_county(levels["6"]):
        city = levels["6"]  # kreisfreie Stadt without a separate level-8 boundary
    if not city and levels.get("4") in CITY_STATES:
        city = levels["4"]
    suburb = levels.get("10") or addr_suburb
    district = levels.get("9") or suburb
    return Place(city=city, district=district, suburb=suburb)
