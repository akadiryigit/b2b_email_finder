"""Business segments and how they are tagged in OpenStreetMap.

OpenStreetMap describes every place with key=value tags. A segment groups one or
more tags (e.g. "Tabak / Lotto / Presse" = shop=tobacco, shop=lottery, shop=newsagent).
To add a tag, look it up on https://wiki.openstreetmap.org and add it below.

The order of CATEGORIES is also the colour order in the UI: a segment keeps its
colour forever, so never reorder — append new segments at the end.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    osm_filters: tuple[tuple[str, str], ...]
    notes: str = ""


CATEGORIES: dict[str, Category] = {
    "kiosk": Category(
        "kiosk", "Kiosk", (("shop", "kiosk"),),
        "Büdchen / Trinkhalle. Mostly owner-run; few have a website.",
    ),
    "fuel": Category(
        "fuel", "Tankstelle", (("amenity", "fuel"),),
        "Most belong to chains; see the Ownership column. Head-office emails are never used.",
    ),
    "convenience": Category(
        "convenience", "Späti / Convenience", (("shop", "convenience"),),
        "Spätis, corner shops, many international markets.",
    ),
    "vape": Category(
        "vape", "Vape & Shisha-Shop", (("shop", "e-cigarette"), ("shop", "hookah")),
        "E-cigarette (Elf Bar etc.) and shisha supply shops. Under-mapped in OSM.",
    ),
    "tobacco": Category(
        "tobacco", "Tabak / Lotto / Presse",
        (("shop", "tobacco"), ("shop", "lottery"), ("shop", "newsagent")),
    ),
    "sweets": Category(
        "sweets", "Süßwaren", (("shop", "confectionery"), ("shop", "chocolate")),
        "Candy / American candy / chocolate shops.",
    ),
    "shisha_bar": Category(
        "shisha_bar", "Shisha-Bar", (("amenity", "hookah_lounge"),),
    ),
    "food": Category(
        "food", "Lebensmittel",
        (("shop", "supermarket"), ("shop", "beverages"), ("shop", "greengrocer"), ("shop", "deli")),
        "Supermarkets (discounters excluded), drinks shops, greengrocers, delis.",
    ),
}

SUBTYPE_LABELS = {
    ("shop", "kiosk"): "Kiosk",
    ("amenity", "fuel"): "Tankstelle",
    ("shop", "convenience"): "Späti / Convenience",
    ("shop", "e-cigarette"): "Vape-Shop",
    ("shop", "hookah"): "Shisha-Shop",
    ("shop", "tobacco"): "Tabak",
    ("shop", "lottery"): "Lotto",
    ("shop", "newsagent"): "Presse",
    ("shop", "confectionery"): "Süßwaren",
    ("shop", "chocolate"): "Schokolade",
    ("amenity", "hookah_lounge"): "Shisha-Bar",
    ("shop", "supermarket"): "Supermarkt",
    ("shop", "beverages"): "Getränkemarkt",
    ("shop", "greengrocer"): "Obst & Gemüse",
    ("shop", "deli"): "Feinkost",
}


def match_tags(tags: dict[str, str], categories: list[Category]) -> tuple[str, str] | None:
    """Return (category key, subtype label) for a place, or None.

    `amenity` tags win over `shop` tags: a gas station that also carries
    shop=kiosk for its shop is a Tankstelle, not a kiosk.
    """
    for wanted_key in ("amenity", "shop"):
        for category in categories:
            for key, value in category.osm_filters:
                if key == wanted_key and tags.get(key) == value:
                    return category.key, SUBTYPE_LABELS.get((key, value), value)
    return None


def category_for_tags(tags: dict[str, str], categories: list[Category]) -> str | None:
    match = match_tags(tags, categories)
    return match[0] if match else None
