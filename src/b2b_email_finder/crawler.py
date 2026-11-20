"""Step 2a: politely visit a business website and collect the emails it publishes.

"Polite" means:
  * robots.txt is respected (including Crawl-delay)
  * at most `max_pages` pages per site: homepage + Impressum/Kontakt pages
  * a pause between requests to the same site
  * a limited number of sites in parallel
  * an honest User-Agent, no proxy rotation, no CAPTCHA solving
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .cache import DiskCache
from .extract import analyse_page
from .utils import site_key

FALLBACK_PATHS = ("/impressum", "/kontakt", "/datenschutz")  # tried only if the homepage links to none
CACHE_DAYS_OK = 14  # a visited website is not visited again for two weeks
CACHE_DAYS_FAILED = 1  # an unreachable one is retried after a day
MAX_CRAWL_DELAY = 10.0  # if robots.txt asks for longer, we stop instead of waiting


@dataclass
class CrawlResult:
    start_url: str
    pages: list[str] = field(default_factory=list)
    emails: dict[str, str] = field(default_factory=dict)  # email -> page it was found on
    robots_blocked: list[str] = field(default_factory=list)
    offsite_redirects: list[str] = field(default_factory=list)  # "requested -> final" URLs
    errors: list[str] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)  # page -> visible text (only if keep_text)


class PoliteCrawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        max_pages: int = 5,
        delay: float = 1.0,
        concurrency: int = 5,
        max_bytes: int = 2_000_000,
        keep_text: bool = False,
        cache_dir: Path | None = None,
    ):
        self.client = client
        self.cache = DiskCache(cache_dir)
        self.keep_text = keep_text
        self.user_agent = user_agent
        self.max_pages = max_pages
        self.delay = delay
        self.max_bytes = max_bytes
        self._semaphore = asyncio.Semaphore(concurrency)
        self._robots: dict[str, RobotFileParser] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def crawl(self, start_url: str) -> CrawlResult:
        # Several store pages on one host (a chain) are crawled one after another,
        # never in parallel, so the per-site delay also holds across stores.
        cached = self._from_cache(start_url)
        if cached is not None:
            return cached
        host_lock = self._host_locks.setdefault(urlsplit(start_url).netloc, asyncio.Lock())
        async with host_lock, self._semaphore:
            result = await self._crawl_site(start_url)
        self.cache.set(start_url, asdict(result))
        return result

    def _from_cache(self, start_url: str) -> CrawlResult | None:
        data = self.cache.get(start_url, CACHE_DAYS_OK)
        if data is None:
            return None
        result = CrawlResult(**data)
        if not result.pages and self.cache.get(start_url, CACHE_DAYS_FAILED) is None:
            return None  # failed visits are retried sooner
        if self.keep_text and result.pages and not result.texts:
            return None  # cached without page text, but this caller needs it
        return result

    async def _crawl_site(self, start_url: str) -> CrawlResult:
        result = CrawlResult(start_url=start_url)
        queue, queued = [start_url], {start_url}
        is_first = True

        while queue and len(result.pages) < self.max_pages:
            url = queue.pop(0)
            if not is_first:
                wait = max(self.delay, await self._crawl_delay(url))
                if wait > MAX_CRAWL_DELAY:
                    break
                await asyncio.sleep(wait)

            page, transport_failed = await self._fetch_allowed(url, result)
            if page is None and transport_failed and is_first and url.startswith("https://"):
                # Many small-business sites have broken TLS but work over plain http.
                page, _ = await self._fetch_allowed("http://" + url[len("https://"):], result)

            was_first, is_first = is_first, False
            if page is None:
                continue
            final_url, html = page
            result.pages.append(final_url)
            info = analyse_page(html, final_url)
            for email in info.emails:
                result.emails.setdefault(email, final_url)
            if self.keep_text:
                result.texts[final_url] = info.text
            if was_first:
                links = info.contact_links or [urljoin(final_url, p) for p in FALLBACK_PATHS]
                for link in links:
                    if link not in queued:
                        queued.add(link)
                        queue.append(link)
        return result

    async def _fetch_allowed(self, url: str, result: CrawlResult) -> tuple[tuple[str, str] | None, bool]:
        """Fetch one HTML page if robots.txt allows it.

        Returns ((final_url, html) or None, transport_failed).
        """
        robots = await self._robots_for(url)
        if not robots.can_fetch(self.user_agent, url):
            result.robots_blocked.append(url)
            return None, False
        try:
            async with self.client.stream("GET", url) as response:
                if response.status_code >= 400:
                    result.errors.append(f"{url}: HTTP {response.status_code}")
                    return None, False
                if site_key(str(response.url)) != site_key(url):
                    # Redirected to another site: a firewall/proxy block page, a domain
                    # parking page, a hosting provider's error page... Its emails do not
                    # belong to this business, so we drop the page.
                    result.offsite_redirects.append(f"{url} -> {response.url}")
                    return None, False
                if "html" not in response.headers.get("content-type", "").lower():
                    return None, False
                body = await self._read_limited(response)
                return (str(response.url), _decode(body, response.encoding)), False
        except httpx.HTTPError as exc:
            result.errors.append(f"{url}: {type(exc).__name__}")
            return None, True

    async def _read_limited(self, response: httpx.Response) -> bytes:
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= self.max_bytes:
                break
        return b"".join(chunks)

    async def _crawl_delay(self, url: str) -> float:
        delay = (await self._robots_for(url)).crawl_delay(self.user_agent)
        return float(delay or 0)

    async def _robots_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = await self._load_robots(origin)
        return self._robots[origin]

    async def _load_robots(self, origin: str) -> RobotFileParser:
        parser = RobotFileParser()
        try:
            response = await self.client.get(origin + "/robots.txt")
        except httpx.HTTPError:
            parser.parse([])  # unreachable robots.txt: the site itself will likely fail too
            return parser
        if response.status_code in (401, 403):
            parser.disallow_all = True
        elif response.status_code >= 400:
            parser.allow_all = True  # no robots.txt = no restrictions (RFC 9309)
        else:
            parser.parse(response.text.splitlines())
        return parser


def _decode(body: bytes, encoding: str | None) -> str:
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except LookupError:  # unknown charset name in the Content-Type header
        return body.decode("utf-8", errors="replace")
