"""Step 2b: pull email addresses and contact-page links out of one HTML page.

Sources, in order of reliability:
  1. `mailto:` links — the site owner explicitly published the address as a link
  2. schema.org JSON-LD (`"email": ...`) — structured data meant for machines
  3. plain text matching an email pattern

Addresses that the site owner deliberately obfuscated ("info [at] kiosk.de",
JavaScript-encoded, Cloudflare email protection) are NOT decoded: that is a
signal the owner does not want the address collected automatically.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .utils import site_key

EMAIL_PATTERN = re.compile(
    r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,24}", re.IGNORECASE
)

# "logo@2x.png" looks like an email to the regex.
_FILE_EXTENSIONS = frozenset(
    {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "css", "js", "pdf", "mp4", "woff", "woff2"}
)
# Template / tracking addresses that appear on many websites.
_IGNORED_DOMAINS = (
    "example.com", "example.org", "example.net", "example.de", "beispiel.de",
    "domain.de", "domain.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
)
# Privacy policies and imprints name third parties: data-protection authorities,
# dispute-resolution bodies, hosting / website-builder companies, legal-text generators.
# Their emails are never the shop's.
_THIRD_PARTY_EMAIL_DOMAINS = (
    "google.com", "facebook.com", "meta.com", "instagram.com", "europa.eu", "cloudflare.com",
    "jimdo.com", "wix.com", "ionos.de", "ionos.com", "1und1.de", "strato.de", "godaddy.com",
    "squarespace.com", "wordpress.com", "shopify.com", "e-recht24.de", "datenschutz-generator.de",
    "verbraucher-schlichter.de", "paypal.com", "klarna.com", "lieferando.de", "wolt.com",
    # German federal / state government domains (data-protection authorities live here)
    "bund.de", "nrw.de", "bayern.de", "hessen.de", "berlin.de", "sachsen.de", "niedersachsen.de",
    "rlp.de", "bwl.de", "saarland.de", "brandenburg.de", "sachsen-anhalt.de", "thueringen.de",
    "bremen.de", "hamburg.de", "mv-regierung.de", "landsh.de", "datenschutzzentrum.de",
)
_AUTHORITY_LOCAL_PARTS = frozenset({"poststelle", "mailbox"})
_PLACEHOLDER_LOCAL_PARTS = frozenset(
    {"max.mustermann", "mustermann", "vorname.nachname", "ihre.email", "ihre-email", "your.email", "youremail"}
)

# Pages where German businesses publish contact data. The Impressum is legally
# required (§ 5 DDG) and must contain an email address.
_CONTACT_KEYWORDS = {
    "impressum": 0, "imprint": 0, "legal-notice": 0,
    "kontakt": 1, "contact": 1,
    "ueber-uns": 2, "uber-uns": 2, "über uns": 2, "about": 2,
    # The privacy policy must name the data controller incl. contact (Art. 13 GDPR).
    "datenschutz": 3, "privacy": 3,
}
MAX_TEXT_CHARS = 30_000
CONTACT_PATH_HINTS = ("impressum", "imprint", "kontakt", "contact")


@dataclass
class PageInfo:
    emails: list[str]
    contact_links: list[str]
    text: str = ""  # visible text, used to check that a found website belongs to the shop


def clean_email(raw: str) -> str | None:
    """Normalise one candidate string; return None if it is not a usable address."""
    candidate = unquote(raw).strip().strip(".,;:()<>[]\"'").lower()
    if candidate.startswith("mailto:"):
        candidate = candidate[len("mailto:"):]
    if not EMAIL_PATTERN.fullmatch(candidate):
        return None
    local, _, domain = candidate.rpartition("@")
    if domain.rsplit(".", 1)[-1] in _FILE_EXTENSIONS:
        return None
    if any(domain == d or domain.endswith("." + d) for d in _IGNORED_DOMAINS + _THIRD_PARTY_EMAIL_DOMAINS):
        return None
    if local in _PLACEHOLDER_LOCAL_PARTS or local in _AUTHORITY_LOCAL_PARTS:
        return None
    return candidate


def is_contact_page(url: str) -> bool:
    path = unquote(urlsplit(url).path).lower()
    return any(hint in path for hint in CONTACT_PATH_HINTS)


def analyse_page(html: str, page_url: str, max_links: int = 4) -> PageInfo:
    soup = BeautifulSoup(html, "html.parser")
    links = _contact_links(soup, page_url, max_links)
    emails, text = _emails_and_text(soup)
    return PageInfo(emails=emails, contact_links=links, text=text[:MAX_TEXT_CHARS])


def _emails_and_text(soup: BeautifulSoup) -> tuple[list[str], str]:
    raw: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:"):
            address = href[len("mailto:"):].split("?", 1)[0]  # drop ?subject=...
            raw.extend(address.split(","))
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw.extend(_json_ld_emails(json.loads(script.string or "")))
        except ValueError:
            continue
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    raw.extend(EMAIL_PATTERN.findall(text))

    unique: dict[str, None] = {}  # dict keeps insertion order
    for candidate in raw:
        email = clean_email(candidate)
        if email:
            unique.setdefault(email)
    return list(unique), text


def _json_ld_emails(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "email" and isinstance(value, str):
                yield value
            else:
                yield from _json_ld_emails(value)
    elif isinstance(node, list):
        for item in node:
            yield from _json_ld_emails(item)


def _contact_links(soup: BeautifulSoup, page_url: str, max_links: int) -> list[str]:
    page_site = site_key(page_url)
    found: dict[str, int] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(page_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or site_key(absolute) != page_site:
            continue  # stay on the business's own website
        haystack = f"{unquote(parts.path).lower()} {anchor.get_text(' ', strip=True).lower()}"
        priorities = [p for keyword, p in _CONTACT_KEYWORDS.items() if keyword in haystack]
        if not priorities:
            continue
        url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        found[url] = min(found.get(url, 99), min(priorities))
    ordered = sorted(found.items(), key=lambda item: item[1])
    return [url for url, _ in ordered[:max_links]]
