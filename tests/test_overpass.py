import asyncio

import httpx
import pytest

from b2b_email_finder import overpass
from b2b_email_finder.categories import CATEGORIES
from b2b_email_finder.overpass import OverpassError, build_query, fetch_overpass, parse_elements
from b2b_email_finder.utils import host_key, is_third_party, normalize_website, registrable_domain

CATS = [CATEGORIES["kiosk"], CATEGORIES["fuel"]]


def test_build_query():
    query = build_query(3600062578, CATS, require_website=True, limit=20)
    assert "area(id:3600062578)->.searchArea;" in query
    assert 'nwr["shop"="kiosk"][~"^(website|contact:website|url)$"~"."](area.searchArea);' in query
    assert 'nwr["amenity"="fuel"]' in query
    assert query.endswith("out center tags 20;")


def test_parse_elements_node_and_way():
    data = {
        "elements": [
            {
                "type": "node", "id": 1, "lat": 50.9, "lon": 6.9,
                "tags": {
                    "shop": "kiosk", "name": "Büdchen", "contact:website": "www.buedchen.de",
                    "email": "info@buedchen.de; chef@buedchen.de", "addr:city": "Köln",
                },
            },
            {
                "type": "way", "id": 2, "center": {"lat": 51.0, "lon": 7.0},
                "tags": {"amenity": "fuel", "brand": "Aral", "website": "https://tankstelle.aral.de/x"},
            },
            {"type": "node", "id": 3, "lat": 1, "lon": 1, "tags": {"shop": "bakery"}},
        ]
    }
    kiosk, fuel = parse_elements(data, CATS)
    assert kiosk.category == "kiosk" and kiosk.city == "Köln"
    assert kiosk.website == "https://www.buedchen.de/"
    assert kiosk.osm_emails == ["info@buedchen.de", "chef@buedchen.de"]
    assert kiosk.osm_url == "https://www.openstreetmap.org/node/1"
    assert fuel.category == "fuel" and fuel.brand == "Aral" and fuel.lat == 51.0


def _fetch(statuses, monkeypatch, tmp_path):
    hosts = []

    async def no_sleep(_):
        return None

    monkeypatch.setattr(overpass.asyncio, "sleep", no_sleep)

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(statuses.pop(0), json={"elements": [{"type": "node", "id": 1}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_overpass(client, "q", cache_dir=tmp_path, endpoints=("https://a.test/api", "https://b.test/api"))

    return asyncio.run(go()), hosts


def test_fetch_retries_on_other_endpoint_and_caches(monkeypatch, tmp_path):
    data, hosts = _fetch([502, 200], monkeypatch, tmp_path)
    assert data["elements"][0]["id"] == 1
    assert hosts == ["a.test", "b.test"]
    cached, hosts = _fetch([], monkeypatch, tmp_path)  # second call: served from cache
    assert cached == data and hosts == []


def test_fetch_gives_up_with_clear_error(monkeypatch, tmp_path):
    with pytest.raises(OverpassError, match="unavailable"):
        _fetch([504, 504, 504, 504], monkeypatch, tmp_path)


def test_url_helpers():
    assert normalize_website("kiosk.de;https://other.de") == "https://kiosk.de/"
    assert normalize_website("ftp://kiosk.de") is None
    assert registrable_domain("www.shop.kiosk.de") == "kiosk.de"
    assert registrable_domain("shop.example.co.uk") == "example.co.uk"
    assert host_key("https://www.Kiosk.de/x") == "kiosk.de"
    assert host_key("https://kiosk-a.metro.biz/") != host_key("https://kiosk-b.metro.biz/")
    assert is_third_party("https://m.facebook.com/kiosk")
    assert not is_third_party("https://kiosk.de/")
