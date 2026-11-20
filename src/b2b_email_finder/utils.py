"""Small URL / domain helpers shared by the other modules."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

# Public suffixes with two labels. A full solution is the Public Suffix List
# (e.g. the `tldextract` package); for German businesses this short list is enough.
_MULTI_PART_SUFFIXES = frozenset(
    {"co.uk", "org.uk", "com.tr", "com.au", "co.at", "or.at", "gv.at", "com.pl", "com.br"}
)

# Websites that are not the business's own site: social networks, link hubs and
# business directories. Crawling them would return the platform's emails, and
# their terms of service forbid scraping — so we never crawl them.
THIRD_PARTY_DOMAINS = frozenset(
    {
        "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com",
        "tiktok.com", "youtube.com", "linkedin.com", "xing.com", "google.com",
        "google.de", "goo.gl", "g.page", "wa.me", "whatsapp.com", "linktr.ee",
        "yelp.de", "yelp.com", "tripadvisor.de", "tripadvisor.com",
        "gelbeseiten.de", "dasoertliche.de", "dastelefonbuch.de", "11880.com",
        "lieferando.de", "ebay.de", "ebay-kleinanzeigen.de", "kleinanzeigen.de",
    }
)


def normalize_website(raw: str | None) -> str | None:
    """Turn an OSM website tag into a clean absolute URL, or None if unusable.

    OSM values are typed by humans: "www.kiosk.de", "https://a.de;https://b.de", ...
    """
    if not raw:
        return None
    url = raw.split(";")[0].strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def registrable_domain(host: str) -> str:
    """"shop.kiosk-mueller.de" -> "kiosk-mueller.de" (the part a company registers)."""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def site_key(url: str) -> str:
    """The registrable domain of a URL — "is this the same organisation's website?"."""
    return registrable_domain(urlsplit(url).hostname or "")


def host_key(url: str) -> str:
    """Hostname without "www." — "is this the same website?". Used to crawl each site once.

    Not the registrable domain: website builders host many unrelated businesses on
    subdomains (kiosk-a.metro.biz, kiosk-b.metro.biz), which must not be merged.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_third_party(url: str) -> bool:
    return site_key(url) in THIRD_PARTY_DOMAINS


def normalize_phone(raw: str | None) -> str | None:
    """German phone numbers in one format: "0221 / 12 34-56" -> "+49221123456".

    Numbers typed by humans come in many shapes; one format makes them comparable
    and usable for call lists and messenger links.
    """
    if not raw:
        return None
    first = raw.split(";")[0].split(",")[0].strip()
    digits = "".join(c for c in first if c.isdigit())
    if first.startswith("+"):
        number = "+" + digits
    elif digits.startswith("00"):
        number = "+" + digits[2:]
    elif digits.startswith("0"):
        number = "+49" + digits[1:]
    else:
        return None
    if number.startswith("+490"):  # "+49 (0)221 ..."
        number = "+49" + number[4:]
    return number if 9 <= len(number) <= 16 else None


def is_mobile(phone: str | None) -> bool:
    """German mobile numbers start with +4915, +4916 or +4917."""
    return bool(phone) and phone.startswith(("+4915", "+4916", "+4917"))
