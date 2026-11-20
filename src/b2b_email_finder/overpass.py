"""Step 1b: download businesses from OpenStreetMap through the Overpass API.

Overpass is a free, read-only query API over the whole OSM database. A query
selects elements by tag inside an area, e.g. every `shop=kiosk` in Köln.
Try queries interactively at https://overpass-turbo.eu — paste the output of
`build_query()` there to see the results on a map.

Data licence: OpenStreetMap data is ODbL. You may reuse it (also commercially)
if you credit "© OpenStreetMap contributors" and share derived databases under
the same licence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import httpx

from .categories import Category, match_tags
from .models import Business
from .segments import international_hint, ownership
from .utils import normalize_phone, normalize_website

log = logging.getLogger(__name__)

# Public instances, see https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
RETRY_STATUSES = (429, 502, 503, 504)  # rate limited / overloaded / gateway problems
WEBSITE_KEYS = ("website", "contact:website", "url")
EMAIL_KEYS = ("email", "contact:email")
PHONE_KEYS = ("phone", "contact:phone", "contact:mobile")


class OverpassError(Exception):
    pass


def build_query(
    area_id: int,
    categories: list[Category],
    *,
    require_website: bool = False,
    limit: int | None = None,
    timeout: int = 180,
) -> str:
    # `nwr` = nodes, ways and relations: a kiosk can be mapped as a point (node)
    # or as a building outline (way). `out center` gives ways a single coordinate.
    website_filter = '[~"^(website|contact:website|url)$"~"."]' if require_website else ""
    lines = [f"[out:json][timeout:{timeout}];", f"area(id:{area_id})->.searchArea;", "("]
    for category in categories:
        for key, value in category.osm_filters:
            lines.append(f'  nwr["{key}"="{value}"]{website_filter}(area.searchArea);')
    lines.append(");")
    lines.append(f"out center tags{f' {limit}' if limit else ''};")
    return "\n".join(lines)


async def fetch_overpass(
    client: httpx.AsyncClient,
    query: str,
    *,
    cache_dir: Path | None = None,
    endpoints: tuple[str, ...] = OVERPASS_ENDPOINTS,
    attempts: int = 4,
) -> dict:
    """Run a query. Results are cached on disk so re-runs don't load the public server."""
    cache_file = None
    if cache_dir is not None:
        digest = hashlib.sha1(query.encode()).hexdigest()[:16]
        cache_file = cache_dir / f"overpass-{digest}.json"
        if cache_file.exists():
            log.info("Using cached Overpass result %s", cache_file)
            return json.loads(cache_file.read_text(encoding="utf-8"))

    response = await _post_with_retries(client, query, endpoints, attempts)
    if response.status_code >= 400:
        raise OverpassError(f"Overpass rejected the query (HTTP {response.status_code}):\n{response.text[:500]}")
    data = response.json()
    remark = data.get("remark") or ""
    if "error" in remark.lower():  # e.g. the query timed out on the server
        raise OverpassError(remark)

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data


async def _post_with_retries(
    client: httpx.AsyncClient, query: str, endpoints: tuple[str, ...], attempts: int
) -> httpx.Response:
    for attempt in range(1, attempts + 1):
        # Alternate between public instances: if one is overloaded, the next may not be.
        endpoint = endpoints[(attempt - 1) % len(endpoints)]
        try:
            response = await client.post(endpoint, data={"data": query}, timeout=httpx.Timeout(240.0))
        except httpx.TransportError as exc:
            problem = type(exc).__name__
        else:
            if response.status_code not in RETRY_STATUSES:
                return response
            problem = f"HTTP {response.status_code}"
        if attempt < attempts:
            wait = 10 * attempt
            log.warning("Overpass %s at %s, retrying in %ss", problem, endpoint, wait)
            await asyncio.sleep(wait)
    raise OverpassError(f"Overpass unavailable ({problem}) after {attempts} attempts, try again later")


def _first(tags: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return None


def parse_elements(data: dict, categories: list[Category]) -> list[Business]:
    businesses = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        match = match_tags(tags, categories)
        if match is None:
            continue
        category, subtype = match
        center = element if "lat" in element else (element.get("center") or {})
        emails = []
        for key in EMAIL_KEYS:
            emails += [part.strip() for part in (tags.get(key) or "").split(";") if part.strip()]
        website = normalize_website(_first(tags, WEBSITE_KEYS))
        businesses.append(
            Business(
                osm_type=element["type"],
                osm_id=element["id"],
                category=category,
                subtype=subtype,
                ownership=ownership(tags),
                international=international_hint(tags) if category not in ("fuel", "shisha_bar") else None,
                name=tags.get("name"),
                brand=tags.get("brand"),
                operator=tags.get("operator"),
                street=tags.get("addr:street"),
                housenumber=tags.get("addr:housenumber"),
                postcode=tags.get("addr:postcode"),
                city=tags.get("addr:city"),
                suburb=tags.get("addr:suburb") or tags.get("addr:district"),
                lat=center.get("lat"),
                lon=center.get("lon"),
                phone=normalize_phone(_first(tags, PHONE_KEYS)),
                website=website,
                website_origin="osm" if website else None,
                osm_emails=emails,
                opening_hours=tags.get("opening_hours"),
            )
        )
    return businesses
