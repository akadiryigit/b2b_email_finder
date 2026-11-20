"""F1/F2: more places, websites and emails from other open place databases.

* Overture Maps places (https://docs.overturemaps.org/guides/places/) — CDLA-Permissive 2.0.
  Downloaded for the search area's bounding box with the `overturemaps` package.
* Foursquare Open Source Places (https://docs.foursquare.com/data-products/docs/access-fsq-os-places)
  — Apache 2.0. Distributed as Parquet files; access needs a (free) Hugging Face login,
  so we read whatever path FSQ_PLACES_PARQUET points to (a local file or an hf:// glob).

Both are optional: if a source is unavailable the search continues without it.
"""

from __future__ import annotations

import logging
import os
import re
import struct
from dataclasses import dataclass, field

from .extract import clean_email
from .segments import international_hint, ownership
from .utils import normalize_phone, normalize_website

log = logging.getLogger(__name__)

# (keyword in the source's category text, our segment, subtype). First match wins,
# so specific keywords come before general ones ("hookah_bar" before "hookah").
_CATEGORY_RULES: tuple[tuple[str, str, str], ...] = (
    ("gas_station", "fuel", "Tankstelle"), ("gas station", "fuel", "Tankstelle"), ("fuel", "fuel", "Tankstelle"),
    ("hookah_bar", "shisha_bar", "Shisha-Bar"), ("hookah bar", "shisha_bar", "Shisha-Bar"),
    ("hookah lounge", "shisha_bar", "Shisha-Bar"), ("shisha", "shisha_bar", "Shisha-Bar"),
    ("vape", "vape", "Vape-Shop"), ("e_cigarette", "vape", "Vape-Shop"), ("e-cigarette", "vape", "Vape-Shop"),
    ("hookah", "vape", "Shisha-Shop"), ("smoke_shop", "tobacco", "Tabak"), ("smoke shop", "tobacco", "Tabak"),
    ("tobacco", "tobacco", "Tabak"), ("lottery", "tobacco", "Lotto"),
    ("newsstand", "tobacco", "Presse"), ("newspaper", "tobacco", "Presse"),
    ("candy", "sweets", "Süßwaren"), ("confection", "sweets", "Süßwaren"), ("chocolat", "sweets", "Schokolade"),
    ("kiosk", "kiosk", "Kiosk"),
    ("convenience", "convenience", "Späti / Convenience"),
    ("supermarket", "food", "Supermarkt"), ("grocery", "food", "Supermarkt"),
    ("beverage", "food", "Getränkemarkt"), ("liquor", "food", "Getränkemarkt"), ("beer", "food", "Getränkemarkt"),
    ("fruit", "food", "Obst & Gemüse"), ("greengrocer", "food", "Obst & Gemüse"),
    ("delicatessen", "food", "Feinkost"),
)
_EXCLUDE_WORDS = (
    "restaurant", "cafe", "café", "bakery", "butcher", "pharmacy", "car_wash", "charging",
    "bar", "pub", "garden", "brewery", "delivery",
)


def map_category(*labels: str | None) -> tuple[str, str] | None:
    """Map a source's category labels to (our segment key, subtype), or None.

    Labels are tried in order (main category first), so a "convenience_store" whose
    secondary label is "kiosk" stays a convenience store.
    """
    for label in labels:
        mapped = _map_one(label.lower()) if label else None
        if mapped:
            return mapped
    return None


def _map_one(text: str) -> tuple[str, str] | None:
    for keyword, category, subtype in _CATEGORY_RULES:
        if keyword in text:
            if category in ("food", "kiosk", "convenience") and any(w in text for w in _EXCLUDE_WORDS):
                return None  # e.g. a deli restaurant
            return category, subtype
    return None


@dataclass
class ExternalPlace:
    source: str  # "overture" | "foursquare"
    id: str
    name: str | None
    lat: float
    lon: float
    category: str
    subtype: str
    category_label: str
    brand: str | None = None
    websites: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)

    @property
    def ownership(self) -> str:
        return ownership({"brand": self.brand or "", "name": self.name or ""})

    @property
    def international(self) -> str | None:
        if self.category in ("fuel", "shisha_bar"):
            return None
        label = self.category_label.lower()
        for word in ("international", "asian", "turkish", "halal", "african", "indian", "russian", "ethnic"):
            if word in label:
                return f"category: {self.category_label}"
        return international_hint({"name": self.name or "", "brand": self.brand or ""})


def _clean_websites(values) -> list[str]:
    result = []
    for value in values or []:
        url = normalize_website(value)
        if url and url not in result:
            result.append(url)
    return result


def _clean_emails(values) -> list[str]:
    result = []
    for value in values or []:
        email = clean_email(value)
        if email and email not in result:
            result.append(email)
    return result


def _clean_phones(values) -> list[str]:
    result = []
    for value in values or []:
        phone = normalize_phone(value)
        if phone and phone not in result:
            result.append(phone)
    return result


def _point_from_wkb(wkb: bytes) -> tuple[float, float] | None:
    """(lon, lat) of a WKB point — Overture stores geometry as WKB."""
    if not wkb or len(wkb) < 21:
        return None
    byte_order = "<" if wkb[0] == 1 else ">"
    geometry_type = struct.unpack(byte_order + "I", wkb[1:5])[0]
    if geometry_type % 1000 != 1:  # 1 = Point (also ISO Z/M variants 1001, 2001, 3001)
        return None
    return struct.unpack(byte_order + "dd", wkb[5:21])


# ---------------------------------------------------------------- Overture
# Exact Overture taxonomy categories (docs.overturemaps.org/schema/reference/places/place).
# Exact names, not keywords: broad parents like "food_and_beverage_store" must not match
# ("beverage" would turn drugstores and fish shops into drinks shops).
OVERTURE_TAXONOMY: dict[str, tuple[str, str]] = {
    "kiosk": ("kiosk", "Kiosk"),
    "convenience_store": ("convenience", "Späti / Convenience"),
    "gas_station": ("fuel", "Tankstelle"),
    "fueling_station": ("fuel", "Tankstelle"),
    "truck_stop": ("fuel", "Tankstelle"),
    "smoke_and_vape_store": ("vape", "Vape-Shop"),
    "vape_shop": ("vape", "Vape-Shop"),
    "e_cigarette_store": ("vape", "Vape-Shop"),
    "hookah_supplies_store": ("vape", "Shisha-Shop"),
    "tobacco_shop": ("tobacco", "Tabak"),
    "lottery_vendor": ("tobacco", "Lotto"),
    "newsstand": ("tobacco", "Presse"),
    "newspaper_and_magazines_store": ("tobacco", "Presse"),
    "candy_store": ("sweets", "Süßwaren"),
    "chocolatier": ("sweets", "Schokolade"),
    "confectionery_store": ("sweets", "Süßwaren"),
    "hookah_bar": ("shisha_bar", "Shisha-Bar"),
    "grocery_store": ("food", "Supermarkt"),
    "supermarket": ("food", "Supermarkt"),
    "international_grocery_store": ("food", "Supermarkt"),
    "asian_grocery_store": ("food", "Supermarkt"),
    "middle_eastern_grocery_store": ("food", "Supermarkt"),
    "liquor_store": ("food", "Getränkemarkt"),
    "beer_wine_spirits_store": ("food", "Getränkemarkt"),
    "beverage_store": ("food", "Getränkemarkt"),
    "produce_store": ("food", "Obst & Gemüse"),
    "delicatessen": ("food", "Feinkost"),
}
# A kiosk is sometimes filed as a parcel shop first (most kiosks are Hermes/DHL points):
# for these primary categories the alternate categories are checked too.
_NEUTRAL_PRIMARY = frozenset(
    {"shipping_center", "post_office", "shipping_or_delivery_service", "food_and_beverage_store",
     "specialty_store", "shopping", "retail", None}
)
_CLOSED = frozenset({"permanently_closed", "temporarily_closed"})
_NEAR_MISS_WORDS = ("store", "shop", "market", "kiosk", "station", "vendor")


def overture_category(row: dict) -> tuple[tuple[str, str], str] | None:
    """((segment, subtype), label) for an Overture place, or None if it is not ours."""
    taxonomy = row.get("taxonomy") or {}
    primary = taxonomy.get("primary")
    if primary in OVERTURE_TAXONOMY:
        return OVERTURE_TAXONOMY[primary], primary
    if primary in _NEUTRAL_PRIMARY:
        for alternate in taxonomy.get("alternates") or []:
            if alternate in OVERTURE_TAXONOMY:
                return OVERTURE_TAXONOMY[alternate], alternate
    if primary is None:  # older releases: "categories" instead of "taxonomy"
        categories = row.get("categories") or {}
        mapped = map_category(categories.get("primary"), *(categories.get("alternate") or []))
        if mapped:
            return mapped, categories.get("primary") or ""
    return None


def parse_overture_row(row: dict) -> ExternalPlace | None:
    if row.get("operating_status") in _CLOSED:
        return None
    found = overture_category(row)
    if found is None:
        return None
    mapped, label = found
    point = _point_from_wkb(row.get("geometry"))
    if point is None:
        bbox = row.get("bbox") or {}
        if not bbox:
            return None
        point = ((bbox["xmin"] + bbox["xmax"]) / 2, (bbox["ymin"] + bbox["ymax"]) / 2)
    names = row.get("names") or {}
    brand = ((row.get("brand") or {}).get("names") or {}).get("primary")
    return ExternalPlace(
        source="overture",
        id=str(row.get("id")),
        name=names.get("primary"),
        lon=point[0],
        lat=point[1],
        category=mapped[0],
        subtype=mapped[1],
        category_label=label,
        brand=brand,
        websites=_clean_websites(row.get("websites")),
        emails=_clean_emails(row.get("emails")),
        phones=_clean_phones(row.get("phones")),
    )


def load_overture(bbox: tuple[float, float, float, float]) -> tuple[list[ExternalPlace], dict[str, int]]:
    """Places in (west, south, east, north). Returns (mapped places, counts of unmapped categories)."""
    try:
        from overturemaps import core
    except ImportError:
        log.warning("Overture skipped: install the extra with  uv pip install -e '.[sources]'")
        return [], {}
    log.info("Downloading Overture places for the area's bounding box …")
    reader = core.record_batch_reader("place", bbox)
    if reader is None:
        return [], {}
    places, unmapped = [], {}
    for batch in reader:
        for row in batch.to_pylist():
            place = parse_overture_row(row)
            if place is not None:
                places.append(place)
            elif row.get("operating_status") in _CLOSED:
                unmapped["(closed)"] = unmapped.get("(closed)", 0) + 1
            else:
                label = (row.get("taxonomy") or {}).get("primary") or "(none)"
                if any(w in label for w in _NEAR_MISS_WORDS):  # only report labels worth a look
                    unmapped[label] = unmapped.get(label, 0) + 1
    log.info("Overture: %d places in our segments", len(places))
    return places, unmapped


# ---------------------------------------------------------------- Foursquare
FSQ_ENV = "FSQ_PLACES_PARQUET"


def parse_foursquare_row(row: dict) -> ExternalPlace | None:
    labels = row.get("fsq_category_labels") or []
    mapped = map_category(*labels)
    if mapped is None or row.get("latitude") is None or row.get("longitude") is None:
        return None
    return ExternalPlace(
        source="foursquare",
        id=str(row.get("fsq_place_id")),
        name=row.get("name"),
        lat=float(row["latitude"]),
        lon=float(row["longitude"]),
        category=mapped[0],
        subtype=mapped[1],
        category_label=(labels[0] if labels else ""),
        websites=_clean_websites([row.get("website")]),
        emails=_clean_emails([row.get("email")]),
        phones=_clean_phones([row.get("tel")]),
    )


def load_foursquare(bbox: tuple[float, float, float, float]) -> list[ExternalPlace]:
    path = os.environ.get(FSQ_ENV)
    if not path:
        log.info("Foursquare skipped: set %s to its Parquet files (see README)", FSQ_ENV)
        return []
    try:
        import duckdb
    except ImportError:
        log.warning("Foursquare skipped: install the extra with  uv pip install -e '.[sources]'")
        return []
    if not re.fullmatch(r"[\w./:*=\-]+", path):  # the path goes into SQL text below
        log.warning("Foursquare skipped: unexpected characters in %s", FSQ_ENV)
        return []
    west, south, east, north = bbox
    connection = duckdb.connect()
    if path.startswith("hf://"):
        # Uses the token saved by `huggingface-cli login`; we never handle it ourselves.
        connection.execute("CREATE SECRET hf (TYPE huggingface, PROVIDER credential_chain)")
    query = f"""
        SELECT fsq_place_id, name, latitude, longitude, website, email, tel, fsq_category_labels
        FROM read_parquet('{path}')
        WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?
          AND date_closed IS NULL
    """
    log.info("Reading Foursquare places for the area's bounding box …")
    cursor = connection.execute(query, [south, north, west, east])
    columns = [d[0] for d in cursor.description]
    places = [p for p in (parse_foursquare_row(dict(zip(columns, r, strict=True))) for r in cursor.fetchall()) if p]
    log.info("Foursquare: %d places in our segments", len(places))
    return places
