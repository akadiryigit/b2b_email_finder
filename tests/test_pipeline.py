"""End-to-end run of the pipeline against fake servers — no network needed."""

import asyncio

import httpx

from b2b_email_finder import pipeline
from b2b_email_finder.geocode import Area
from b2b_email_finder.sources import ExternalPlace

AREA = Area("Köln", "Köln, NRW", 3600062578, (6.77, 50.83, 7.17, 51.09))
HTML = {"content-type": "text/html; charset=utf-8"}

OSM = {
    "elements": [
        # has a website listed in OSM
        {"type": "node", "id": 1, "lat": 50.95, "lon": 6.92,
         "tags": {"shop": "kiosk", "name": "Kiosk Sonne", "website": "https://kiosk-sonne.de"}},
        # no website anywhere -> found by domain guessing
        {"type": "node", "id": 2, "lat": 50.96, "lon": 6.93,
         "tags": {"shop": "kiosk", "name": "Kiosk Ali Baba", "addr:street": "Venloer Straße", "addr:postcode": "50823"}},
        # chain station: its chain website is never crawled
        {"type": "node", "id": 3, "lat": 50.97, "lon": 6.94,
         "tags": {"amenity": "fuel", "brand": "Aral", "website": "https://tankstelle.aral.de/koeln/3"}},
        # discounter: excluded
        {"type": "node", "id": 4, "lat": 50.98, "lon": 6.95, "tags": {"shop": "supermarket", "brand": "Lidl"}},
    ]
}
OVERTURE = [
    # matches node 1 and adds an email
    ExternalPlace("overture", "o1", "Sonne Kiosk", 50.95001, 6.92001, "kiosk", "Kiosk", "kiosk", emails=["sonne@web.de"]),
    # a vape shop OSM doesn't know, inside Köln
    ExternalPlace("overture", "o2", "Cloud Vape", 50.94, 6.95, "vape", "Vape-Shop", "vape_shop",
                  websites=["https://cloud-vape.de/"]),
    # inside the bounding box but outside Köln -> dropped
    ExternalPlace("overture", "o3", "Kiosk Pulheim", 51.0, 6.80, "kiosk", "Kiosk", "kiosk"),
]
SITES = {
    "kiosk-sonne.de": {"/": '<a href="/impressum">Impressum</a>', "/impressum": "Mail: info@kiosk-sonne.de"},
    "ali-baba.de": {"/": "Kiosk Ali Baba, Venloer Str. 12, 50823 Köln. kontakt@ali-baba.de"},
    "cloud-vape.de": {"/": '<a href="/kontakt">Kontakt</a>', "/kontakt": "shop@cloud-vape.de"},
}


def _levels(business):
    if business.key == ("overture", "o3"):
        return {"8": "Pulheim", "id:3600000001": "Pulheim"}
    return {"6": "Köln", "9": "Ehrenfeld", "id:3600062578": "Köln"}


def test_full_search(monkeypatch):
    requested_hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host.removeprefix("www.")
        requested_hosts.append(host)
        page = SITES.get(host, {}).get(request.url.path)
        if page is None:
            return httpx.Response(404, headers=HTML, text="not found")
        return httpx.Response(200, headers=HTML, text=page)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(pipeline.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    async def fake_area(client, name):
        return AREA

    async def fake_overpass(client, query, cache_dir=None):
        return OSM

    async def fake_admin(client, businesses, cache_dir=None):
        return {b.key: _levels(b) for b in businesses}

    async def domain_exists(self, domain):
        return domain in SITES

    async def mail_server(self, domain):
        return "mx"

    monkeypatch.setattr(pipeline, "resolve_area", fake_area)
    monkeypatch.setattr(pipeline, "fetch_overpass", fake_overpass)
    monkeypatch.setattr(pipeline, "fetch_admin_levels", fake_admin)
    monkeypatch.setattr(pipeline, "load_overture", lambda bbox: (OVERTURE, {"dentist": 3}))
    monkeypatch.setattr(pipeline.DomainChecker, "exists", domain_exists)
    monkeypatch.setattr(pipeline.MailDomainChecker, "check", mail_server)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    opts = pipeline.SearchOptions(
        area="Köln", categories=list(pipeline.CATEGORIES), sources=["overture"], commoncrawl=False,
        delay=0, cache_dir=None,
    )
    result = asyncio.run(pipeline.run_search(opts))
    rows = {r["name"]: r for r in result.rows}

    assert set(rows) == {"Kiosk Sonne", "Kiosk Ali Baba", None, "Cloud Vape"}  # Lidl + Pulheim kiosk dropped
    sonne = rows["Kiosk Sonne"]
    assert sonne["sources"] == ["osm", "overture"]
    assert sonne["best_email"] == "info@kiosk-sonne.de" and sonne["best_email_confidence"] == "high"
    assert {e["email"] for e in sonne["emails"]} == {"info@kiosk-sonne.de", "sonne@web.de"}

    ali = rows["Kiosk Ali Baba"]
    assert ali["website"] == "https://ali-baba.de/" and ali["website_origin"] == "guess (name+street)"
    assert ali["best_email"] == "kontakt@ali-baba.de"

    vape = rows["Cloud Vape"]
    assert vape["category"] == "vape" and vape["sources"] == ["overture"] and vape["best_email"] == "shop@cloud-vape.de"
    assert vape["district"] == "Ehrenfeld"

    aral = rows[None]
    assert aral["crawl_status"] == "chain_site" and aral["best_email"] is None
    assert "tankstelle.aral.de" not in requested_hosts

    assert result.stats["overture_new"] == 2 and result.stats["external_places_outside_area_dropped"] == 1
    assert result.stats["websites_found_by_guess"] == 1
    assert result.stats["overture_unmapped_categories_top"] == {"dentist": 3}
