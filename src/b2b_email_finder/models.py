from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SourcedEmail:
    """An email that a place database (OSM, Overture, Foursquare) attaches to a place."""

    email: str
    source: str  # "osm" | "overture" | "foursquare"
    source_url: str


@dataclass
class Business:
    """One place, from OpenStreetMap or another open place database."""

    osm_type: str  # "node" | "way" | "relation" for OSM; "overture" | "foursquare" otherwise
    osm_id: int | str
    category: str
    subtype: str | None = None  # e.g. "Getränkemarkt" inside category "food"
    ownership: str = "independent"  # see segments.ownership
    international: str | None = None  # reason it is probably an international market
    name: str | None = None
    brand: str | None = None
    operator: str | None = None
    street: str | None = None
    housenumber: str | None = None
    postcode: str | None = None
    city: str | None = None  # addr:city as tagged — see admin_areas for the resolved city
    suburb: str | None = None  # addr:suburb / addr:district
    lat: float | None = None
    lon: float | None = None
    phone: str | None = None
    website: str | None = None  # main website, normalised (utils.normalize_website)
    extra_websites: list[str] = field(default_factory=list)  # from other sources / discovery
    website_origin: str | None = None  # where the main website came from: osm, overture, guess, search, ...
    osm_emails: list[str] = field(default_factory=list)  # emails from the OSM tags
    sourced_emails: list[SourcedEmail] = field(default_factory=list)  # emails from other databases
    sources: list[str] = field(default_factory=lambda: ["osm"])
    opening_hours: str | None = None

    @property
    def key(self) -> tuple[str, int | str]:
        return (self.osm_type, self.osm_id)

    @property
    def is_osm(self) -> bool:
        return self.osm_type in ("node", "way", "relation")

    @property
    def osm_url(self) -> str:
        if self.is_osm:
            return f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"
        return f"https://www.openstreetmap.org/?mlat={self.lat}&mlon={self.lon}#map=19/{self.lat}/{self.lon}"

    @property
    def all_websites(self) -> list[str]:
        websites = [self.website] if self.website else []
        return websites + [w for w in self.extra_websites if w not in websites]
