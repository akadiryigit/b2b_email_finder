"""Merge places from several databases into one list without duplicates ("conflation").

Two records are the same shop when they are close together and their names are
similar, or when they are close and share the same website. A matched external
record adds its websites and emails to the existing place; an unmatched one
becomes a new place.
"""

from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher

from .models import Business, SourcedEmail
from .sources import ExternalPlace
from .utils import host_key, is_third_party

MAX_DISTANCE_M = 75  # name match
MAX_DISTANCE_SAME_WEBSITE_M = 150
MAX_DISTANCE_NAMELESS_M = 20  # both names generic ("Kiosk" vs "Kiosk")
MIN_NAME_SIMILARITY = 0.6

GENERIC_WORDS = frozenset(
    {
        "kiosk", "büdchen", "budchen", "buedchen", "trinkhalle", "späti", "spati", "spaeti", "spätkauf",
        "tankstelle", "station", "markt", "market", "supermarkt", "shop", "store", "getränke", "getraenke",
        "lotto", "tabak", "tabakwaren", "presse", "zeitschriften", "shisha", "bar", "lounge", "vape",
        "gmbh", "ug", "ek", "e.k.", "ohg", "kg", "und", "&", "-", "the", "der", "die", "das", "am", "an", "im",
        "köln", "koeln", "cologne", "inh", "inh.", "inhaber",
    }
)


def fold(text: str) -> str:
    """Lowercase, umlauts -> ae/oe/ue, ß -> ss, accents removed: "Gıda Köln" -> "gida koeln"."""
    text = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).replace("ı", "i")
    return "".join(c for c in text if not unicodedata.combining(c))


def distinctive_tokens(name: str | None) -> list[str]:
    """The words of a name that identify this shop ("Kiosk Ali Baba" -> ["ali", "baba"])."""
    if not name:
        return []
    words = re.findall(r"[\w']+", name.lower())
    generic = {fold(w) for w in GENERIC_WORDS}
    return [fold(w) for w in words if fold(w) not in generic and len(w) >= 2]


def name_similarity(a: str | None, b: str | None) -> float:
    ta, tb = " ".join(distinctive_tokens(a)), " ".join(distinctive_tokens(b))
    if not ta or not tb:
        return 0.0
    return SequenceMatcher(None, ta, tb).ratio()


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6_371_000 * math.hypot(dlat, dlon)


class _Grid:
    """Spatial index: a lookup only compares places in the 3x3 cells around a point.

    A cell is 0.0025° = ~280 m north-south and ~175 m east-west in Germany, so the
    neighbouring cells always cover the largest match distance (150 m).
    """

    CELL = 0.0025

    def __init__(self, businesses: list[Business]):
        self.cells: dict[tuple[int, int], list[Business]] = {}
        for business in businesses:
            self.add(business)

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(lat // self.CELL), int(lon // self.CELL))

    def add(self, business: Business) -> None:
        if business.lat is not None and business.lon is not None:
            self.cells.setdefault(self._cell(business.lat, business.lon), []).append(business)

    def near(self, lat: float, lon: float):
        cy, cx = self._cell(lat, lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                yield from self.cells.get((cy + dy, cx + dx), [])


def _match_score(business: Business, place: ExternalPlace) -> float | None:
    if business.lat is None or business.lon is None:
        return None
    distance = distance_m(business.lat, business.lon, place.lat, place.lon)
    hosts = {host_key(w) for w in business.all_websites if not is_third_party(w)}
    same_site = any(host_key(w) in hosts for w in place.websites if not is_third_party(w))
    similarity = name_similarity(business.name, place.name)
    nameless = not distinctive_tokens(business.name) and not distinctive_tokens(place.name)

    if same_site and distance <= MAX_DISTANCE_SAME_WEBSITE_M:
        return 2.0 - distance / 1000
    if similarity >= MIN_NAME_SIMILARITY and distance <= MAX_DISTANCE_M:
        return 1.0 + similarity - distance / 1000
    if nameless and business.category == place.category and distance <= MAX_DISTANCE_NAMELESS_M:
        return 0.5 - distance / 1000
    return None


def _apply(business: Business, place: ExternalPlace) -> None:
    if place.source not in business.sources:
        business.sources.append(place.source)
    for website in place.websites:
        if website in business.all_websites:
            continue
        # The shop's own site becomes the main website if it only had a social/directory link.
        if not is_third_party(website) and (business.website is None or is_third_party(business.website)):
            if business.website:
                business.extra_websites.append(business.website)
            business.website, business.website_origin = website, place.source
        else:
            business.extra_websites.append(website)
    if business.phone is None and place.phones:
        business.phone = place.phones[0]
    known = {e.email for e in business.sourced_emails}
    for email in place.emails:
        if email not in known:
            business.sourced_emails.append(SourcedEmail(email, place.source, _source_url(place)))


def _source_url(place: ExternalPlace) -> str:
    if place.source == "overture":
        return f"overture:{place.id}"
    return f"https://foursquare.com/v/{place.id}"


def to_business(place: ExternalPlace) -> Business:
    business = Business(
        osm_type=place.source,
        osm_id=place.id,
        category=place.category,
        subtype=place.subtype,
        ownership=place.ownership,
        international=place.international,
        name=place.name,
        brand=place.brand,
        lat=place.lat,
        lon=place.lon,
        sources=[],
    )
    _apply(business, place)
    return business


def conflate(businesses: list[Business], places: list[ExternalPlace]) -> tuple[int, list[Business]]:
    """Merge `places` into `businesses` (in place). Returns (matched count, new businesses)."""
    grid = _Grid(businesses)
    matched, new = 0, []
    for place in places:
        best, best_score = None, None
        for business in grid.near(place.lat, place.lon):
            score = _match_score(business, place)
            if score is not None and (best_score is None or score > best_score):
                best, best_score = business, score
        if best is not None:
            _apply(best, place)
            matched += 1
        else:
            business = to_business(place)
            grid.add(business)  # a later source can match it too
            new.append(business)
    return matched, new
