"""F3/F4: find websites for places that don't list one.

Two ways to get candidate websites:
  F4 guess:  build likely domains from the name ("Kiosk Ali Baba" -> alibaba.de,
             ali-baba.de, kiosk-ali-baba.de, ...) and keep those that exist in DNS
  F3 search: ask a web search API (Brave Search, free tier) for "<name> <street> <city>"

A candidate is only accepted if its own pages prove it is this shop: the shop's
distinctive name words AND its street or postcode (or, for names with at least two
distinctive words, the city) must appear on the crawled pages. This avoids
attaching a random website to a shop with a common name.

The search API key is read from the BRAVE_API_KEY environment variable and never
written anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import httpx

from .conflate import distinctive_tokens, fold
from .models import Business
from .segments import CHAIN_EMAIL_DOMAINS
from .utils import THIRD_PARTY_DOMAINS, is_third_party, site_key

log = logging.getLogger(__name__)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_ENV = "BRAVE_API_KEY"
SEARCH_INTERVAL_S = 1.1  # free tier: 1 query per second

# Results on these sites are never the shop's own website.
_NOT_OWN_SITE = THIRD_PARTY_DOMAINS | CHAIN_EMAIL_DOMAINS | frozenset(
    {
        "wikipedia.org", "openstreetmap.org", "cylex.de", "branchenbuch.de", "meinestadt.de",
        "golocal.de", "werkenntdenbesten.de", "koeln.de", "stadtbranchenbuch.com", "firmenwissen.de",
        "northdata.de", "wolt.com", "ubereats.com", "flink.com", "foursquare.com", "tiktok.com",
        "kennstdueinen.de", "yably.de", "oeffnungszeitenbuch.de", "branchen-info.net",
    }
)
_SEGMENT_PREFIX = {
    "kiosk": "kiosk", "convenience": "spaeti", "fuel": "tankstelle", "vape": "vape",
    "tobacco": "tabak", "sweets": "", "shisha_bar": "shisha", "food": "",
}


@dataclass(frozen=True)
class CandidateSite:
    url: str
    origin: str  # "guess" | "search"


def slug(tokens: list[str]) -> str:
    return re.sub(r"[^a-z0-9-]+", "", "-".join(tokens))


def guess_domains(business: Business, city: str | None, max_guesses: int = 6) -> list[str]:
    tokens = distinctive_tokens(business.name)
    if not tokens or len("".join(tokens)) < 4:
        return []  # too generic to guess ("Kiosk", "Ali")
    dashed, joined = slug(tokens), slug(tokens).replace("-", "")
    prefix = _SEGMENT_PREFIX.get(business.category, "")
    city_slug = slug([fold(city)]) if city else ""
    names = [dashed, joined]
    if prefix:
        names += [f"{prefix}-{dashed}", f"{prefix}{joined}"]
    if city_slug:
        names += [f"{dashed}-{city_slug}"]
    unique = list(dict.fromkeys(n for n in names if n))
    return [f"{name}.de" for name in unique][:max_guesses]


class DomainChecker:
    """Does a domain exist (has an A or AAAA record)? Cached, async."""

    def __init__(self, lifetime: float = 4.0):
        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.lifetime = lifetime
        self._cache: dict[str, bool] = {}

    async def exists(self, domain: str) -> bool:
        if domain not in self._cache:
            self._cache[domain] = await self._lookup(domain)
        return self._cache[domain]

    async def _lookup(self, domain: str) -> bool:
        for record_type in ("A", "AAAA"):
            try:
                await self._resolver.resolve(domain, record_type)
                return True
            except dns.exception.DNSException:
                continue
        return False


class BraveSearch:
    def __init__(self, client: httpx.AsyncClient, api_key: str, max_queries: int):
        self.client = client
        self._api_key = api_key
        self.max_queries = max_queries
        self.used = 0
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, client: httpx.AsyncClient, max_queries: int) -> BraveSearch | None:
        key = os.environ.get(BRAVE_ENV, "").strip()
        return cls(client, key, max_queries) if key else None

    async def search(self, query: str, count: int = 5) -> list[str]:
        async with self._lock:  # one query at a time, at most 1 per second
            if self.used >= self.max_queries:
                return []
            self.used += 1
            await asyncio.sleep(SEARCH_INTERVAL_S)
            response = await self.client.get(
                BRAVE_URL,
                params={"q": query, "country": "de", "search_lang": "de", "count": count},
                headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
            )
        if response.status_code != 200:
            log.warning("Search API returned HTTP %s", response.status_code)
            return []
        results = (response.json().get("web") or {}).get("results") or []
        return [r["url"] for r in results if r.get("url")]


def search_query(business: Business, city: str | None) -> str:
    parts = [f'"{business.name}"', business.street or "", city or ""]
    return " ".join(p for p in parts if p).strip()


def own_site_candidates(urls: list[str], limit: int = 3) -> list[str]:
    """Keep search results that could be the shop's own website (one per site)."""
    seen, result = set(), []
    for url in urls:
        key = site_key(url)
        if not key or key in _NOT_OWN_SITE or is_third_party(url) or key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result[:limit]


def verify_match(business: Business, city: str | None, texts: list[str]) -> str | None:
    """Return evidence if the pages belong to this business, else None."""
    tokens = distinctive_tokens(business.name)
    if not tokens or not texts:
        return None
    text = fold(" ".join(texts))
    if not all(re.search(rf"\b{re.escape(t)}\b", text) for t in tokens):
        return None
    if business.street:
        street = fold(business.street).replace("strasse", "str").rstrip(".")
        if street and street in text.replace("strasse", "str"):
            return "name+street"
    if business.postcode and business.postcode in text:
        return "name+postcode"
    if city and len(tokens) >= 2 and fold(city) in text:
        return "name+city"
    return None
