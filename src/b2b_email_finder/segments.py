"""Who runs a place, and is it an international market? Heuristics on OSM tags.

Ownership values:
  independent  no known chain brand
  merchant     chain brand, but run by an independent merchant ("EDEKA Müller")
  chain        known chain (fuel brands, REWE/EDEKA without merchant name, ...)
  discounter   Aldi, Lidl, Penny, ... -> excluded from results entirely

Head-office emails of chains are never used as a place's contact (see
CHAIN_EMAIL_DOMAINS and pipeline.py). The chain's local station or store
itself stays in the results.
"""

from __future__ import annotations

import re

DISCOUNTER_BRANDS = frozenset(
    {"aldi", "aldi nord", "aldi süd", "lidl", "penny", "netto", "netto marken-discount", "norma", "kaufland",
     # drugstore chains: central purchasing, never a buyer for us
     "dm", "dm-drogerie markt", "rossmann", "müller"}
)
# Places from Overture often have no brand, so their names are checked too.
_DISCOUNTER_NAME_PREFIXES = (
    "aldi", "lidl", "penny", "netto marken", "netto filiale", "norma", "kaufland",
    "dm-drogerie", "dm drogerie", "rossmann", "müller drogerie",
)
# Brands whose stores are often run by independent merchants (selbstständige Kaufleute).
MERCHANT_RUN_BRANDS = frozenset(
    {"edeka", "rewe", "nah & gut", "nahkauf", "marktkauf", "e center", "e aktiv markt", "hit", "spar", "spar express"}
)
CHAIN_BRANDS = frozenset(
    {
        # fuel
        "aral", "shell", "esso", "jet", "totalenergies", "total", "star", "avia", "agip", "eni",
        "hem", "westfalen", "markant", "raiffeisen", "omv", "baywa", "q1", "hoyer", "orlen",
        "classic", "tamoil", "team",
        # food / convenience
        "globus", "tegut", "real", "combi", "famila", "rewe to go", "servicestore db",
        "k kiosk", "ditsch", "mix markt",
    }
) | MERCHANT_RUN_BRANDS

# Emails on these domains belong to a chain's head office, never to a single store.
CHAIN_EMAIL_DOMAINS = frozenset(
    {
        "aral.de", "bp.com", "shell.de", "shell.com", "esso.de", "exxonmobil.com", "jet.de",
        "jet-mobility.com", "totalenergies.de", "totalenergies.com", "total.de", "star.de",
        "orlen-deutschland.de", "orlen.pl", "avia.de", "eni.com", "eni.de", "agip.de", "hem-tankstelle.de",
        "westfalen.com", "omv.de", "omv.com", "baywa.de", "hoyer.de", "q1.eu", "deutschebahn.com",
        "rewe.de", "rewe-group.com", "edeka.de", "lidl.de", "aldi-nord.de", "aldi-sued.de",
        "penny.de", "netto-online.de", "kaufland.de", "norma-online.de", "globus.de", "tegut.de",
        "marktkauf.de", "mixmarkt.eu",
        "dm.de", "rossmann.de", "mueller.de", "hussel.de", "gourvita.com", "as24.com", "teegschwendner.com",
        "nespresso.com", "jacques.de",
    }
)

_GENERIC_STORE_WORDS = frozenset(
    {"markt", "market", "center", "city", "xpress", "express", "aktiv", "e", "to", "go", "nah", "gut",
     "&", "und", "frische", "frischemarkt", "supermarkt", "-", "filiale"}
)


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def ownership(tags: dict[str, str]) -> str:
    brand = _norm(tags.get("brand"))
    if _norm(tags.get("name")).startswith(_DISCOUNTER_NAME_PREFIXES):
        return "discounter"
    if not brand:
        return "independent"
    if brand in DISCOUNTER_BRANDS:
        return "discounter"
    if brand in MERCHANT_RUN_BRANDS and _looks_merchant_run(tags, brand):
        return "merchant"
    if brand in CHAIN_BRANDS:
        return "chain"
    return "independent"  # unknown brand: services like "Lotto" or "DHL" are not owners


def _looks_merchant_run(tags: dict[str, str], brand: str) -> bool:
    operator = _norm(tags.get("operator"))
    if operator and brand not in operator and "rewe markt gmbh" not in operator:
        return True  # e.g. operator="Müller oHG"
    leftover = [w for w in re.split(r"\s+", _norm(tags.get("name"))) if w and w not in _GENERIC_STORE_WORDS]
    leftover = [w for w in leftover if w not in brand.split()]
    return bool(leftover)  # "EDEKA Müller" -> ["müller"]


# Words that strongly suggest a Turkish, Arabic or other international market.
_INTERNATIONAL_WORDS = (
    "gıda", "gida", "bakkal", "helal", "halal", "istanbul", "ankara", "izmir", "antalya", "anadolu",
    "anatolia", "karadeniz", "türk", "turk", "bosporus", "kapadokya", "bazaar", "bazar", "basar",
    "orient", "arab", "damaskus", "damascus", "syrien", "syria", "beirut", "libanon", "lebanon",
    "bagdad", "baghdad", "irak", "marokko", "maroc", "marrakesch", "afghan", "kabul", "persisch",
    "persia", "teheran", "kurd", "asia", "asien", "afro", "afrika", "africa", "polski", "polska",
    "balkan", "mix markt", "russ", "thai", "vietnam", "india", "indisch", "sri lanka", "ethno",
)
_INTERNATIONAL_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _INTERNATIONAL_WORDS) + r")", re.IGNORECASE)


def international_hint(tags: dict[str, str]) -> str | None:
    """Why this place is probably an international market, or None. A guess, not a fact."""
    if tags.get("diet:halal") in ("yes", "only"):
        return "diet:halal=" + tags["diet:halal"]
    text = " ".join(tags.get(k, "") for k in ("name", "brand", "operator", "cuisine"))
    match = _INTERNATIONAL_RE.search(text)
    return f'name: "{match.group(1).lower()}"' if match else None
