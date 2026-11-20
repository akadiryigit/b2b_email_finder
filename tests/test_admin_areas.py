from b2b_email_finder.admin_areas import build_admin_query, in_area, parse_admin_response, resolve_place
from b2b_email_finder.models import Business


def _b(osm_type, osm_id, lat=50.9, lon=6.9):
    return Business(osm_type=osm_type, osm_id=osm_id, category="kiosk", lat=lat, lon=lon)


def test_build_admin_query_groups_ids_by_type():
    query = build_admin_query([_b("node", 1), _b("way", 2), _b("node", 3)])
    assert "node(id:1,3);" in query and "way(id:2);" in query and "relation(id:" not in query
    assert "is_in" in query and '"admin_level"~"^(4|6|8|9|10)$"' in query


def test_build_admin_query_uses_coordinates_for_other_sources():
    query = build_admin_query([_b("overture", "abc", 50.95, 6.92)])
    assert "foreach" not in query
    assert "is_in(50.9500000,6.9200000)->.containing;" in query
    assert 'make pt idx="overture|abc"; out;' in query


def test_parse_point_markers_and_area_ids():
    data = {
        "elements": [
            {"type": "pt", "id": 1, "tags": {"idx": "overture|abc"}},
            {"type": "area", "id": 3600062578, "tags": {"admin_level": "6", "name": "Köln"}},
        ]
    }
    levels = parse_admin_response(data)[("overture", "abc")]
    assert levels["6"] == "Köln"
    assert in_area(levels, 3600062578) and not in_area(levels, 3600000001)


def test_parse_admin_response_assigns_areas_to_preceding_business():
    data = {
        "elements": [
            {"type": "node", "id": 1},
            {"type": "area", "id": 9, "tags": {"admin_level": "6", "name": "Köln"}},
            {"type": "area", "id": 8, "tags": {"admin_level": "9", "name": "Ehrenfeld"}},
            {"type": "way", "id": 2},
            {"type": "area", "id": 7, "tags": {"admin_level": "8", "name": "Pulheim"}},
            {"type": "node", "id": 3},
        ]
    }
    result = parse_admin_response(data)
    assert {k: v for k, v in result[("node", 1)].items() if not k.startswith("id:")} == {"6": "Köln", "9": "Ehrenfeld"}
    assert result[("way", 2)]["8"] == "Pulheim"
    assert result[("node", 3)] == {}


def test_resolve_place():
    # kreisfreie Stadt: only level 6
    p = resolve_place({"4": "NRW", "6": "Köln", "9": "Nippes", "10": "Niehl"}, None, None)
    assert (p.city, p.district, p.suburb) == ("Köln", "Nippes", "Niehl")
    # Gemeinde in a Landkreis: level 8 wins, county name is never used as city
    p = resolve_place({"6": "Rhein-Erft-Kreis", "8": "Pulheim", "10": "Brauweiler"}, None, None)
    assert (p.city, p.district) == ("Pulheim", "Brauweiler")
    assert resolve_place({"6": "Landkreis Harburg"}, None, None).city is None
    # city-state
    assert resolve_place({"4": "Berlin", "9": "Mitte", "10": "Moabit"}, None, None).city == "Berlin"
    # fall back to address tags
    p = resolve_place({}, "Bonn", "Beuel")
    assert (p.city, p.district, p.suburb) == ("Bonn", "Beuel", "Beuel")


def test_empty_answers_count_as_unknown(tmp_path, monkeypatch):
    import asyncio

    from b2b_email_finder import admin_areas

    async def empty_overpass(client, query, cache_dir=None, attempts=4):
        return {"elements": [{"type": "pt", "id": 1, "tags": {"idx": "overture|a"}}]}  # marker, no areas

    monkeypatch.setattr(admin_areas, "fetch_overpass", empty_overpass)
    result = asyncio.run(admin_areas.fetch_admin_levels(None, [_b("overture", "a")], cache_dir=tmp_path))
    assert ("overture", "a") not in result  # unknown -> the pipeline keeps the place
    assert not list((tmp_path / "admin").glob("*.json")) if (tmp_path / "admin").exists() else True


def test_lookup_results_are_cached_per_place(tmp_path, monkeypatch):
    import asyncio

    from b2b_email_finder import admin_areas

    calls = []

    async def fake_overpass(client, query, cache_dir=None, attempts=4):
        calls.append(query)
        return {"elements": [{"type": "node", "id": 1},
                             {"type": "area", "id": 3600062578, "tags": {"admin_level": "6", "name": "Köln"}}]}

    monkeypatch.setattr(admin_areas, "fetch_overpass", fake_overpass)
    first = asyncio.run(admin_areas.fetch_admin_levels(None, [_b("node", 1)], cache_dir=tmp_path))
    # a different mix of places later: node 1 comes from the cache, only node 2 is looked up
    asyncio.run(admin_areas.fetch_admin_levels(None, [_b("node", 1), _b("node", 2)], cache_dir=tmp_path))
    assert first[("node", 1)]["6"] == "Köln"
    assert len(calls) == 2 and "node(id:2);" in calls[1] and "1" not in calls[1].split("node(id:")[1].split(")")[0]
