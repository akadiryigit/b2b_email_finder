"""Crawler tests against a fake website — no network needed (httpx.MockTransport)."""

import asyncio

import httpx

from b2b_email_finder.crawler import PoliteCrawler

HTML = {"content-type": "text/html; charset=utf-8"}

SITE = {
    "/robots.txt": (200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /intern/\n"),
    "/": (200, HTML, '<a href="/impressum">Impressum</a> <a href="/intern/kontakt">Kontakt</a>'),
    "/impressum": (200, HTML, '<a href="mailto:info@kiosk.de">info@kiosk.de</a>'),
    "/intern/kontakt": (200, HTML, "secret@kiosk.de"),
}


def _run(site: dict, start: str = "https://kiosk.de/"):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        status, headers, body = site.get(request.url.path, (404, HTML, "not found"))
        return httpx.Response(status, headers=headers, text=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            crawler = PoliteCrawler(client, user_agent="b2b-email-finder/test", delay=0)
            return await crawler.crawl(start)

    return asyncio.run(go()), requested


def test_follows_contact_links_and_respects_robots():
    result, requested = _run(SITE)
    assert result.emails == {"info@kiosk.de": "https://kiosk.de/impressum"}
    assert "/intern/kontakt" not in requested
    assert result.robots_blocked == ["https://kiosk.de/intern/kontakt"]


def test_falls_back_to_impressum_when_homepage_has_no_links():
    site = {
        "/": (200, HTML, "Willkommen"),
        "/impressum": (200, HTML, "E-Mail: kontakt@kiosk.de"),
    }
    result, requested = _run(site)
    assert result.emails == {"kontakt@kiosk.de": "https://kiosk.de/impressum"}
    assert requested.count("/robots.txt") == 1  # robots.txt cached per origin


def test_disallow_all_robots_fetches_nothing():
    site = {"/robots.txt": (200, {}, "User-agent: *\nDisallow: /\n"), "/": (200, HTML, "a@kiosk.de")}
    result, requested = _run(site)
    assert result.pages == [] and result.emails == {}
    assert requested == ["/robots.txt"]


def test_drops_pages_redirected_to_another_site():
    """E.g. a corporate firewall redirecting to its block page, which shows *your* email."""
    site = {
        "/robots.txt": (404, HTML, ""),
        "/": (302, {"location": "http://10.0.0.1/php/urlblock.php?args=x"}, ""),
        "/php/urlblock.php": (200, HTML, "Blocked. User: me@my-employer.com"),
    }
    result, _ = _run(site)
    assert result.emails == {} and result.pages == []
    assert result.offsite_redirects == ["https://kiosk.de/ -> http://10.0.0.1/php/urlblock.php?args=x"]


def test_second_run_uses_the_disk_cache(tmp_path):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        status, headers, body = SITE.get(request.url.path, (404, HTML, ""))
        return httpx.Response(status, headers=headers, text=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            crawler = PoliteCrawler(client, user_agent="t", delay=0, cache_dir=tmp_path)
            return await crawler.crawl("https://kiosk.de/")

    first = asyncio.run(go())
    count = len(requested)
    second = asyncio.run(go())
    assert second.emails == first.emails == {"info@kiosk.de": "https://kiosk.de/impressum"}
    assert len(requested) == count  # nothing fetched again


def test_skips_non_html():
    site = {"/": (200, {"content-type": "application/pdf"}, "info@kiosk.de")}
    result, _ = _run(site)
    assert result.emails == {}
