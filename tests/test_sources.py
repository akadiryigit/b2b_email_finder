import gzip
import struct

from b2b_email_finder.commoncrawl import parse_index_lines, parse_warc_record, pick_records
from b2b_email_finder.conflate import conflate, distinctive_tokens, fold, name_similarity
from b2b_email_finder.discovery import guess_domains, own_site_candidates, search_query, verify_match
from b2b_email_finder.models import Business
from b2b_email_finder.sources import ExternalPlace, map_category, parse_foursquare_row, parse_overture_row
from b2b_email_finder import suppression


# ---------------------------------------------------------------- category mapping
def test_map_category():
    assert map_category("convenience_store") == ("convenience", "Späti / Convenience")
    assert map_category("gas_station") == ("fuel", "Tankstelle")
    assert map_category("hookah_bar") == ("shisha_bar", "Shisha-Bar")
    assert map_category("vape_shop") == ("vape", "Vape-Shop")
    assert map_category("Retail > Candy Store") == ("sweets", "Süßwaren")
    assert map_category("Retail > Food and Beverage Retail > Liquor Store") == ("food", "Getränkemarkt")
    assert map_category("beer_garden") is None
    assert map_category("food_delivery_service") is None
    assert map_category("italian_restaurant") is None


def test_parse_overture_row():
    wkb = b"\x01" + struct.pack("<I", 1) + struct.pack("<dd", 6.92, 50.95)
    row = {
        "id": "08f1", "geometry": wkb, "names": {"primary": "Kiosk Ali Baba"},
        "categories": {"primary": "convenience_store", "alternate": ["kiosk"]},
        "websites": ["www.alibaba-kiosk.de"], "emails": ["Info@AliBaba-Kiosk.de"],
        "brand": None,
    }
    place = parse_overture_row(row)
    assert (place.lon, place.lat) == (6.92, 50.95)
    assert place.category == "convenience" and place.websites == ["https://www.alibaba-kiosk.de/"]
    assert place.emails == ["info@alibaba-kiosk.de"]
    assert parse_overture_row({**row, "categories": {"primary": "dentist"}}) is None


def test_parse_foursquare_row():
    row = {
        "fsq_place_id": "4b0", "name": "Öz Gida", "latitude": 50.94, "longitude": 6.95,
        "website": None, "email": "oz@web.de", "fsq_category_labels": ["Retail > Grocery Store"],
    }
    place = parse_foursquare_row(row)
    assert place.category == "food" and place.emails == ["oz@web.de"] and place.international


# ---------------------------------------------------------------- conflation
def test_name_helpers():
    assert fold("Gıda Köln Straße") == "gida koeln strasse"
    assert distinctive_tokens("Kiosk Ali Baba") == ["ali", "baba"]
    assert distinctive_tokens("Kiosk") == []
    assert name_similarity("Kiosk Ali Baba", "Ali-Baba Büdchen") > 0.9


def _osm(name, lat, lon, website=None):
    return Business(osm_type="node", osm_id=1, category="kiosk", name=name, lat=lat, lon=lon, website=website)


def _ext(name, lat, lon, websites=(), emails=(), source="overture", category="kiosk"):
    return ExternalPlace(source, "x1", name, lat, lon, category, "Kiosk", "kiosk", websites=list(websites), emails=list(emails))


def test_conflate_merges_close_similar_places():
    osm = _osm("Kiosk Ali Baba", 50.95, 6.92)
    matched, new = conflate([osm], [_ext("Ali Baba Kiosk", 50.9502, 6.9201, ["https://alibaba.de/"], ["a@alibaba.de"])])
    assert (matched, new) == (1, [])
    assert osm.website == "https://alibaba.de/" and osm.website_origin == "overture"
    assert osm.sourced_emails[0].email == "a@alibaba.de" and osm.sources == ["osm", "overture"]


def test_conflate_keeps_far_or_different_places_apart():
    osm = _osm("Kiosk Ali Baba", 50.95, 6.92)
    matched, new = conflate([osm], [_ext("Ali Baba", 50.96, 6.92), _ext("Sonnen Kiosk", 50.9501, 6.9201)])
    assert matched == 0 and len(new) == 2
    assert new[0].osm_type == "overture" and new[0].sources == ["overture"]


def test_conflate_same_website_matches_even_with_other_name():
    osm = _osm("Büdchen", 50.95, 6.92, website="https://www.ehrenfeld-kiosk.de/")
    matched, _ = conflate([osm], [_ext("Ehrenfelder Trinkhalle", 50.9508, 6.9210, ["https://ehrenfeld-kiosk.de/"])])
    assert matched == 1


def test_conflate_second_source_matches_first_sources_new_place():
    _, new = conflate([], [_ext("Kiosk Ali Baba", 50.95, 6.92)])
    matched, newer = conflate(new, [_ext("Ali Baba", 50.95005, 6.92005, source="foursquare")])
    assert matched == 1 and newer == [] and new[0].sources == ["overture", "foursquare"]


# ---------------------------------------------------------------- discovery
def test_guess_domains():
    kiosk = _osm("Kiosk Ali Baba", 50.95, 6.92)
    assert guess_domains(kiosk, "Köln") == [
        "ali-baba.de", "alibaba.de", "kiosk-ali-baba.de", "kioskalibaba.de", "ali-baba-koeln.de",
    ]
    assert guess_domains(_osm("Kiosk", 0, 0), "Köln") == []  # too generic
    assert guess_domains(_osm("Kiosk Ali", 0, 0), "Köln") == []  # too short to be safe


def test_own_site_candidates_skips_directories_and_social():
    urls = [
        "https://www.gelbeseiten.de/x", "https://www.facebook.com/kiosk", "https://alibaba-kiosk.de/",
        "https://alibaba-kiosk.de/impressum", "https://www.wolt.com/de/x", "https://other.de/",
    ]
    assert own_site_candidates(urls) == ["https://alibaba-kiosk.de/", "https://other.de/"]


def test_verify_match():
    kiosk = _osm("Kiosk Ali Baba", 50.95, 6.92)
    kiosk.street, kiosk.postcode = "Venloer Straße", "50823"
    page = "Impressum Kiosk Ali Baba Inh. A. Yilmaz, Venloer Str. 12, 50823 Köln, info@alibaba.de"
    assert verify_match(kiosk, "Köln", [page]) == "name+street"
    assert verify_match(kiosk, "Köln", ["Ali Baba Reisen GmbH, Hauptstr. 1, 10115 Berlin"]) is None
    no_address = _osm("Kiosk Ali Baba", 50.95, 6.92)
    assert verify_match(no_address, "Köln", ["Kiosk Ali Baba – dein Büdchen in Köln"]) == "name+city"
    assert verify_match(no_address, "Köln", ["Domain alibaba.de zu verkaufen"]) is None
    assert search_query(kiosk, "Köln") == '"Kiosk Ali Baba" Venloer Straße Köln'


# ---------------------------------------------------------------- Common Crawl
def test_index_parsing_and_record_choice():
    text = "\n".join([
        '{"url": "https://k.de/", "status": "200", "mime": "text/html", "timestamp": "20240101"}',
        '{"url": "https://k.de/", "status": "200", "mime": "text/html", "timestamp": "20250101"}',
        '{"url": "https://k.de/impressum", "status": "200", "mime": "text/html", "timestamp": "20230101"}',
        '{"url": "https://k.de/logo.png", "status": "200", "mime": "image/png", "timestamp": "20250101"}',
        '{"url": "https://k.de/alt", "status": "301", "mime": "text/html", "timestamp": "20250101"}',
        "not json",
    ])
    picked = pick_records(parse_index_lines(text))
    assert [(r["url"], r["timestamp"]) for r in picked] == [
        ("https://k.de/impressum", "20230101"), ("https://k.de/", "20250101"),
    ]


def test_parse_warc_record():
    body = "<html><a href='mailto:info@k.de'>Mail</a> Köln</html>".encode("iso-8859-1")
    record = (
        b"WARC/1.0\r\nWARC-Type: response\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=iso-8859-1\r\n\r\n" + body
    )
    assert "Köln" in parse_warc_record(gzip.compress(record))
    assert parse_warc_record(b"not gzip") is None


# ---------------------------------------------------------------- suppression list
def test_suppression(tmp_path):
    path = tmp_path / "suppress.txt"
    assert suppression.add(path, ["Info@Kiosk.de", "spam.de", "info@kiosk.de", "nonsense"]) == ["info@kiosk.de", "spam.de"]
    assert suppression.add(path, ["spam.de"]) == []
    entries = suppression.load(path)
    assert suppression.is_suppressed("INFO@kiosk.de", entries)
    assert suppression.is_suppressed("anyone@spam.de", entries)
    assert not suppression.is_suppressed("other@kiosk.de", entries)


# ---------------------------------------------------------------- Overture taxonomy (current schema)
def _overture(primary, alternates=None, status="open"):
    wkb = b"\x01" + struct.pack("<I", 1) + struct.pack("<dd", 6.95, 50.94)
    return {"id": "x", "geometry": wkb, "names": {"primary": "Test"}, "categories": None,
            "basic_category": "food_and_beverage_store", "operating_status": status,
            "taxonomy": {"primary": primary, "hierarchy": [], "alternates": alternates}}


def test_overture_taxonomy_exact_mapping():
    assert parse_overture_row(_overture("kiosk")).category == "kiosk"
    assert parse_overture_row(_overture("smoke_and_vape_store")).category == "vape"
    assert parse_overture_row(_overture("liquor_store")).subtype == "Getränkemarkt"
    # broad parent categories are not ours ("beverage" in food_and_beverage_store!)
    assert parse_overture_row(_overture("food_and_beverage_store")) is None
    assert parse_overture_row(_overture("vitamin_and_supplement_store")) is None
    assert parse_overture_row(_overture("ev_charging_station")) is None
    assert parse_overture_row(_overture("cannabis_dispensary")) is None


def test_overture_alternates_only_for_neutral_primary():
    assert parse_overture_row(_overture("shipping_center", ["kiosk"])).category == "kiosk"
    assert parse_overture_row(_overture("doner_kebab_restaurant", ["kiosk"])) is None


def test_overture_closed_places_are_skipped():
    assert parse_overture_row(_overture("kiosk", status="permanently_closed")) is None
