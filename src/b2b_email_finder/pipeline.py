"""Glue: area -> places from OSM (+ Overture, Foursquare) -> websites (listed, guessed,
searched) -> emails (live pages, Common Crawl archive) -> one checked row per place."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import __version__
from .admin_areas import fetch_admin_levels, in_area, resolve_place
from .categories import CATEGORIES
from .cache import DiskCache
from .commoncrawl import ArchivedEmail, CommonCrawl
from .conflate import conflate, distinctive_tokens
from .crawler import CrawlResult, PoliteCrawler
from .discovery import BraveSearch, CandidateSite, DomainChecker, guess_domains, own_site_candidates, search_query, verify_match
from .extract import clean_email
from .geocode import Area, resolve_area
from .models import Business
from .overpass import build_query, fetch_overpass, parse_elements
from .segments import CHAIN_EMAIL_DOMAINS
from .sources import load_foursquare, load_overture
from .utils import is_mobile, host_key, is_third_party, normalize_website, registrable_domain, site_key
from .verify import NOT_CHECKED, USABLE_STATUSES, MailDomainChecker, classify, confidence, rank

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = f"b2b-email-finder/{__version__} (open-source learning project)"
PLACE_SOURCES = ("overture", "foursquare")
SHARED_EMAIL_MIN_PLACES = 4


@dataclass
class SearchOptions:
    area: str
    categories: list[str]
    limit: int | None = None
    require_website: bool = False
    only_with_email: bool = False
    admin_areas: bool = True  # look up city / district / Stadtteil per place
    sources: list[str] = field(default_factory=lambda: list(PLACE_SOURCES))  # extra place databases (F1/F2)
    crawl: bool = True
    discover: bool = True  # F3/F4: find websites for places without one
    max_search_queries: int = 2000
    # F6: archived pages for sites we cannot open. Off by default: in the Köln test the
    # archive's index server failed 60% of lookups (HTTP 502/504) and found 1 email in 20 min.
    commoncrawl: bool = False
    max_archive_sites: int = 300
    verify: bool = True
    max_pages: int = 5
    delay: float = 1.0
    concurrency: int = 5
    user_agent: str = DEFAULT_USER_AGENT
    cache_dir: Path | None = Path(".cache")
    suppress: set[str] = field(default_factory=set)  # emails or domains never to output


@dataclass
class Candidate:
    email: str
    source: str  # osm | overture | foursquare | website | commoncrawl
    source_url: str


@dataclass
class SearchResult:
    area: Area
    query: str
    rows: list[dict]
    stats: dict


async def run_search(opts: SearchOptions) -> SearchResult:
    headers = {"User-Agent": opts.user_agent, "Accept-Language": "de,en;q=0.8"}
    stats_extra: dict = {}
    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(15.0), follow_redirects=True
    ) as client:
        area = await resolve_area(client, opts.area)
        log.info("Area: %s (Overpass area id %s)", area.display_name, area.area_id)

        categories = [CATEGORIES[key] for key in opts.categories]
        query = build_query(area.area_id, categories, require_website=opts.require_website, limit=opts.limit)
        log.debug("Overpass query:\n%s", query)
        businesses = parse_elements(await fetch_overpass(client, query, cache_dir=opts.cache_dir), categories)
        log.info("OpenStreetMap: %d places", len(businesses))

        businesses += await _add_place_sources(businesses, area, opts, stats_extra)
        discounters = sum(1 for b in businesses if b.ownership == "discounter")
        businesses = [b for b in businesses if b.ownership != "discounter" and b.category in opts.categories]
        log.info("%d places after merging sources (%d discounters excluded)", len(businesses), discounters)

        # The district lookup (slow public map server) runs while the websites are visited.
        admin_task = asyncio.create_task(
            fetch_admin_levels(client, businesses, cache_dir=opts.cache_dir) if opts.admin_areas else _nothing()
        )
        crawl_results: dict[str, CrawlResult] = {}
        archived: dict[str, list] = {}
        try:
            if opts.crawl:
                crawl_results = await _crawl_sites(client, businesses, opts)
        finally:
            admin_levels = await admin_task

        # Bounding boxes are rectangles: drop external places that lie outside the searched
        # area. Places whose lookup failed are kept (and counted) rather than lost.
        before = len(businesses)
        businesses = [
            b for b in businesses
            if b.is_osm or b.key not in admin_levels or in_area(admin_levels[b.key], area.area_id)
        ]
        stats_extra["external_places_outside_area_dropped"] = before - len(businesses)
        if opts.admin_areas:
            stats_extra["places_without_district_lookup"] = sum(1 for b in businesses if b.key not in admin_levels)
        cities = {b.key: resolve_place(admin_levels.get(b.key, {}), b.city, b.suburb).city for b in businesses}

        if opts.crawl:
            if opts.discover:
                stats_extra.update(await _discover(client, businesses, cities, crawl_results, opts))
            if opts.commoncrawl:
                archived = await _archive_lookup(client, businesses, crawl_results, opts)

    candidates = {b.key: _collect_candidates(b, crawl_results, archived, opts.suppress) for b in businesses}
    mail_servers = await _check_domains(candidates, opts.verify)
    shared_emails = _shared_emails(candidates)
    # A phone number listed for many places is a chain hotline, not the shop.
    phone_counts = Counter(b.phone for b in businesses if b.phone)
    for b in businesses:
        if b.phone and phone_counts[b.phone] >= SHARED_EMAIL_MIN_PLACES:
            b.phone = None

    # How many places point to the same website host — >1 means a chain / shared site.
    site_usage = Counter(host_key(b.website) for b in businesses if b.website)
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for b in businesses:
        place = resolve_place(admin_levels.get(b.key, {}), b.city, b.suburb)
        row = _build_row(
            b, candidates[b.key], mail_servers, shared_emails, crawl_results, site_usage, opts.crawl, scraped_at
        )
        rows.append({"city": place.city, "district": place.district, "suburb": place.suburb, **row})
    stats = {**_stats(rows), **stats_extra}
    if opts.only_with_email:
        rows = [row for row in rows if row["best_email"]]
    return SearchResult(area=area, query=query, rows=rows, stats=stats)


# ---------------------------------------------------------------- F1/F2 place sources
async def _add_place_sources(
    businesses: list[Business], area: Area, opts: SearchOptions, stats: dict
) -> list[Business]:
    if not opts.sources or area.bbox is None:
        return []
    new_places: list[Business] = []
    loaders = {"overture": load_overture, "foursquare": load_foursquare}
    for source in opts.sources:
        try:
            loaded = await asyncio.to_thread(loaders[source], area.bbox)
        except Exception as exc:  # an optional source must never stop the search
            log.warning("%s skipped: %s", source.capitalize(), exc)
            continue
        places, unmapped = loaded if isinstance(loaded, tuple) else (loaded, {})
        places = [p for p in places if p.category in opts.categories]
        matched, new = conflate(businesses + new_places, places)
        new_places += new
        log.info("%s: %d matched existing places, %d new places", source.capitalize(), matched, len(new))
        stats[f"{source}_matched"], stats[f"{source}_new"] = matched, len(new)
        if unmapped:
            top = sorted(unmapped.items(), key=lambda item: -item[1])[:15]
            stats[f"{source}_unmapped_categories_top"] = dict(top)
    return new_places


async def _nothing() -> dict:
    return {}


# ---------------------------------------------------------------- crawling
def _crawlable(business: Business) -> list[str]:
    """Websites of this place worth crawling (no social media, no chain head-office sites)."""
    result = []
    for website in business.all_websites:
        if is_third_party(website):
            continue
        if business.ownership == "chain" and site_key(website) in CHAIN_EMAIL_DOMAINS:
            continue
        result.append(website)
    return result


def _new_crawler(client: httpx.AsyncClient, opts: SearchOptions, **overrides) -> PoliteCrawler:
    settings = dict(
        user_agent=opts.user_agent, max_pages=opts.max_pages, delay=opts.delay, concurrency=opts.concurrency,
        cache_dir=opts.cache_dir / "crawl" if opts.cache_dir else None,
    )
    return PoliteCrawler(client, **{**settings, **overrides})


async def _crawl_many(crawler: PoliteCrawler, urls: list[str], label: str) -> dict[str, CrawlResult]:
    results: dict[str, CrawlResult] = {}

    async def crawl_one(url: str) -> None:
        try:
            results[url] = await crawler.crawl(url)
        except Exception as exc:  # one broken website must not stop the whole run
            log.warning("Crawling %s failed: %r", url, exc)
            results[url] = CrawlResult(start_url=url, errors=[repr(exc)])

    tasks = [asyncio.create_task(crawl_one(u)) for u in urls]
    for done, task in enumerate(asyncio.as_completed(tasks), start=1):
        await task
        if done % 25 == 0 or done == len(tasks):
            log.info("%s: %d/%d", label, done, len(tasks))
    return results


async def _crawl_sites(
    client: httpx.AsyncClient, businesses: list[Business], opts: SearchOptions
) -> dict[str, CrawlResult]:
    # Each distinct website URL is crawled once. Not each host: chains put every store on
    # one host (edeka.de/.../store-a, .../store-b) and store A's emails must not go to B.
    urls = list(dict.fromkeys(w for b in businesses for w in _crawlable(b)))
    if not urls:
        return {}
    log.info("Crawling %d websites (max %d pages each)", len(urls), opts.max_pages)
    return await _crawl_many(_new_crawler(client, opts), urls, "Crawled websites")


# ---------------------------------------------------------------- F3/F4 discovery
async def _discover(
    client: httpx.AsyncClient,
    businesses: list[Business],
    cities: dict,
    crawl_results: dict[str, CrawlResult],
    opts: SearchOptions,
) -> dict:
    targets = [
        b for b in businesses
        if not _crawlable(b) and b.ownership != "chain" and distinctive_tokens(b.name)
    ]
    if not targets:
        return {}
    search = BraveSearch.from_env(client, opts.max_search_queries)
    if search is None:
        log.info("Web search skipped (BRAVE_API_KEY not set); only guessing domains")
    log.info("Looking for websites of %d places without one", len(targets))
    domains = DomainChecker()
    crawler = _new_crawler(client, opts, max_pages=3, keep_text=True)
    found = Counter()

    async def try_site(business: Business, site: CandidateSite) -> bool:
        url = normalize_website(site.url)
        if not url:
            return False
        result = crawl_results.get(url) or await crawler.crawl(url)
        evidence = verify_match(business, cities.get(business.key), list(result.texts.values()))
        if not evidence:
            return False
        crawl_results[url] = result
        business.website, business.website_origin = url, f"{site.origin} ({evidence})"
        found[site.origin] += 1
        return True

    async def discover_one(business: Business) -> None:
        city = cities.get(business.key)
        try:
            for domain in guess_domains(business, city):  # F4: free, try first
                if await domains.exists(domain) and await try_site(business, CandidateSite(f"https://{domain}/", "guess")):
                    return
            if search is not None:  # F3: costs a search query
                urls = await search.search(search_query(business, city))
                for url in own_site_candidates(urls):
                    if await try_site(business, CandidateSite(url, "search")):
                        return
        except (httpx.HTTPError, OSError) as exc:
            log.debug("Discovery for %s failed: %r", business.name, exc)

    for start in range(0, len(targets), 50):
        await asyncio.gather(*(discover_one(b) for b in targets[start:start + 50]))
        log.info("Website discovery: %d/%d places checked", min(start + 50, len(targets)), len(targets))
    stats = {"websites_found_by_guess": found["guess"], "websites_found_by_search": found["search"]}
    if search is not None:
        stats["search_queries_used"] = search.used
    log.info("Website discovery found %d (guess) + %d (search) websites", found["guess"], found["search"])
    return stats


# ---------------------------------------------------------------- F6 Common Crawl
async def _archive_lookup(
    client: httpx.AsyncClient, businesses: list[Business], crawl_results: dict[str, CrawlResult], opts: SearchOptions
) -> dict[str, list]:
    """Archived pages for sites that could not be opened, or had no email live."""
    urls = []
    for business in businesses:
        for website in _crawlable(business):
            result = crawl_results.get(website)
            if result is None or result.robots_blocked:
                continue  # robots.txt said no: we also don't use the archive
            if not result.pages or not result.emails:
                urls.append(website)
    urls = list(dict.fromkeys(urls))[: opts.max_archive_sites]
    if not urls:
        return {}
    log.info("Checking the Common Crawl archive for %d websites", len(urls))
    archive, semaphore, results = CommonCrawl(client), asyncio.Semaphore(2), {}
    cache = DiskCache(opts.cache_dir / "commoncrawl" if opts.cache_dir else None)

    errors: Counter = Counter()
    done = 0

    async def lookup(url: str) -> None:
        nonlocal done
        cached = cache.get(url, max_age_days=30)
        if cached is not None:
            results[url] = [ArchivedEmail(**item) for item in cached]
        else:
            async with semaphore:
                try:
                    results[url] = await archive.emails_for(url)
                    cache.set(url, [asdict(item) for item in results[url]])
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    errors[f"HTTP {status}" if status else type(exc).__name__] += 1
        done += 1
        if done % 25 == 0 or done == len(urls):
            log.info("Common Crawl: %d/%d websites checked", done, len(urls))

    await asyncio.gather(*(lookup(u) for u in urls))
    log.info(
        "Common Crawl: emails for %d of %d websites%s", sum(1 for v in results.values() if v), len(urls),
        f" ({sum(errors.values())} lookups failed: {dict(errors)})" if errors else "",
    )
    return results


# ---------------------------------------------------------------- emails
def _collect_candidates(
    business: Business, crawl_results: dict[str, CrawlResult], archived: dict[str, list], suppress: set[str]
) -> list[Candidate]:
    found: list[Candidate] = []
    for raw in business.osm_emails:
        email = clean_email(raw)
        if email:
            found.append(Candidate(email, "osm", business.osm_url))
    found += [Candidate(e.email, e.source, e.source_url) for e in business.sourced_emails]
    for website in _crawlable(business):
        crawl = crawl_results.get(website)
        if crawl:
            found += [Candidate(e, "website", url) for e, url in crawl.emails.items()]
        for item in archived.get(website, []):
            found.append(Candidate(item.email, "commoncrawl", f"{item.url} (archived {item.timestamp[:8]})"))

    unique: dict[str, Candidate] = {}
    for candidate in found:
        domain = candidate.email.rpartition("@")[2]
        if candidate.email in suppress or domain in suppress:
            continue
        if clean_email(candidate.email) is None:
            continue  # cached visits may predate newer filter rules
        unique.setdefault(candidate.email, candidate)
    return list(unique.values())


async def _check_domains(candidates: dict, verify: bool) -> dict[str, str]:
    domains = sorted({c.email.rpartition("@")[2] for cands in candidates.values() for c in cands})
    if not verify or not domains:
        return {d: NOT_CHECKED for d in domains}
    log.info("Checking mail servers of %d domains", len(domains))
    checker = MailDomainChecker()
    results = await asyncio.gather(*(checker.check(d) for d in domains))
    return dict(zip(domains, results, strict=True))


def _shared_emails(candidates: dict[tuple, list[Candidate]]) -> set[str]:
    """Emails that turn up for many different places: a website builder's, a franchise
    head office's or an agency's address — not a single shop's contact."""
    counts = Counter(c.email for cands in candidates.values() for c in cands)
    return {email for email, count in counts.items() if count >= SHARED_EMAIL_MIN_PLACES}


def _crawl_status(business: Business, crawl_results: dict[str, CrawlResult], crawled: bool) -> str:
    if not business.all_websites:
        return "no_website"
    websites = _crawlable(business)
    if not websites:
        if all(is_third_party(w) for w in business.all_websites):
            return "third_party_site"
        return "chain_site"
    results = [crawl_results.get(w) for w in websites]
    if not crawled or all(r is None for r in results):
        return "not_crawled"
    results = [r for r in results if r is not None]
    if any(r.pages for r in results):
        return "ok"
    if any(r.robots_blocked for r in results):
        return "blocked_by_robots"
    if any(r.offsite_redirects for r in results):
        return "redirected_offsite"
    return "unreachable"


def _build_row(
    business: Business,
    candidates: list[Candidate],
    mail_servers: dict[str, str],
    shared_emails: set[str],
    crawl_results: dict[str, CrawlResult],
    site_usage: Counter,
    crawled: bool,
    scraped_at: str,
) -> dict:
    shared_by = site_usage[host_key(business.website)] if business.website else 0
    # A website matched to the place only by name + city is a weaker link than one
    # listed in a place database or matched by street / postcode.
    weak_site = (business.website_origin or "").endswith("(name+city)")
    scored = []
    for candidate in candidates:
        domain = candidate.email.rpartition("@")[2]
        check = classify(candidate.email, mail_servers.get(domain, NOT_CHECKED), business.website)
        if _is_head_office_email(candidate, business, shared_by):
            check = replace(check, status="chain_hq")
        elif candidate.email in shared_emails:
            check = replace(check, status="shared_email")
        scored.append((rank(check, candidate.source, candidate.source_url), candidate, check))
    scored.sort(key=lambda item: item[0], reverse=True)

    best = scored[0] if scored and scored[0][2].status in USABLE_STATUSES else None
    pages = sum(len(crawl_results[w].pages) for w in _crawlable(business) if w in crawl_results)

    return {
        "osm_type": business.osm_type,
        "osm_id": business.osm_id,
        "osm_url": business.osm_url,
        "sources": business.sources,
        "category": business.category,
        "subtype": business.subtype,
        "ownership": business.ownership,
        "international": business.international,
        "name": business.name,
        "brand": business.brand,
        "operator": business.operator,
        "street": business.street,
        "housenumber": business.housenumber,
        "postcode": business.postcode,
        "addr_city": business.city,
        "lat": business.lat,
        "lon": business.lon,
        "phone": business.phone,
        "website": business.website,
        "website_origin": business.website_origin,
        "extra_websites": business.extra_websites,
        "website_shared_by": shared_by,
        "crawl_status": _crawl_status(business, crawl_results, crawled),
        "pages_crawled": pages,
        "best_email": best[1].email if best else None,
        "best_email_status": best[2].status if best else None,
        "best_email_kind": best[2].kind if best else None,
        "best_email_source": best[1].source if best else None,
        "best_email_source_url": best[1].source_url if best else None,
        "best_email_confidence": (
            confidence(best[2], best[1].source, best[1].source_url, weak_site) if best else None
        ),
        "emails": [
            {
                "email": c.email,
                "status": chk.status,
                "kind": chk.kind,
                "mail_server": chk.mail_server,
                "matches_website": chk.matches_website,
                "freemail": chk.freemail,
                "source": c.source,
                "source_url": c.source_url,
                "score": score,
            }
            for score, c, chk in scored
        ],
        "scraped_at": scraped_at,
    }


def _is_head_office_email(candidate: Candidate, business: Business, shared_by: int) -> bool:
    """An email of a chain's head office, which must never count as a store's contact."""
    domain = registrable_domain(candidate.email.rpartition("@")[2])
    if domain in CHAIN_EMAIL_DOMAINS:
        return True
    # Found on a website many places share, and on that website's own domain
    # (e.g. info@jet.de on jet.de). A merchant's own address on a shared chain
    # page (info@edeka-mueller.de on edeka.de) is kept.
    return (
        candidate.source in ("website", "commoncrawl")
        and shared_by > 1
        and business.website is not None
        and domain == site_key(business.website)
    )


def _stats(rows: list[dict]) -> dict:
    with_email = [r for r in rows if r["best_email"]]
    return {
        "businesses": len(rows),
        "by_category": dict(Counter(r["category"] for r in rows)),
        "by_first_source": dict(Counter((r["sources"] or ["?"])[0] for r in rows)),
        "with_website": sum(1 for r in rows if r["website"]),
        "websites_crawled_ok": sum(1 for r in rows if r["crawl_status"] == "ok"),
        "with_best_email": len(with_email),
        "with_phone": sum(1 for r in rows if r["phone"]),
        "with_mobile_phone": sum(1 for r in rows if is_mobile(r["phone"])),
        "with_email_or_phone": sum(1 for r in rows if r["best_email"] or r["phone"]),
        "best_email_by_source": dict(Counter(r["best_email_source"] for r in with_email)),
        "best_email_by_confidence": dict(Counter(r["best_email_confidence"] for r in with_email)),
        "head_office_emails_excluded": sum(
            1 for r in rows for e in r["emails"] if e["status"] == "chain_hq"
        ),
        "shared_emails_excluded": sum(1 for r in rows for e in r["emails"] if e["status"] == "shared_email"),
        "funnel": {
            "no_website": sum(1 for r in rows if r["crawl_status"] == "no_website"),
            "only_social_or_directory_link": sum(1 for r in rows if r["crawl_status"] == "third_party_site"),
            "chain_site_skipped": sum(1 for r in rows if r["crawl_status"] == "chain_site"),
            "website_not_reachable": sum(
                1 for r in rows if r["crawl_status"] in ("unreachable", "redirected_offsite", "blocked_by_robots")
            ),
            "website_ok_but_no_email": sum(1 for r in rows if r["crawl_status"] == "ok" and not r["best_email"]),
            "website_ok_with_email": sum(1 for r in rows if r["crawl_status"] == "ok" and r["best_email"]),
        },
        "email_coverage_by_category": {
            category: f"{sum(1 for r in group if r['best_email'])}/{len(group)}"
            for category, group in _group(rows, "category").items()
        },
    }


def _group(rows: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    return groups
