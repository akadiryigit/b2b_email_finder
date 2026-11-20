"""Rules that keep wrong emails off a shop."""

from b2b_email_finder.conflate import conflate
from b2b_email_finder.extract import analyse_page, clean_email
from b2b_email_finder.models import Business
from b2b_email_finder.pipeline import SHARED_EMAIL_MIN_PLACES, Candidate, _shared_emails
from b2b_email_finder.sources import ExternalPlace, map_category
from b2b_email_finder.verify import MX, classify, confidence, rank

SITE = "https://kiosk.de/"


def test_authority_and_provider_emails_are_ignored():
    assert clean_email("poststelle@ldi.nrw.de") is None
    assert clean_email("info@bfdi.bund.de") is None
    assert clean_email("support@ionos.de") is None
    assert clean_email("poststelle@irgendwo.de") is None
    page = "Datenschutz: Verantwortlich kiosk@web.de. Aufsicht: poststelle@ldi.nrw.de"
    assert analyse_page(page, SITE + "datenschutz").emails == ["kiosk@web.de"]


def test_foreign_domain_on_the_shops_site_ranks_low():
    own = classify("info@kiosk.de", MX, SITE)
    freemail = classify("kiosk@web.de", MX, SITE)
    agency = classify("hello@webagentur-xy.de", MX, SITE)
    page = SITE + "impressum"
    assert rank(own, "website", page) > rank(freemail, "website", page) > rank(agency, "website", page)
    assert confidence(agency, "website", page) == "low"
    assert confidence(freemail, "website", page) == "medium"


def test_loosely_matched_website_is_never_high_confidence():
    own = classify("info@kiosk.de", MX, SITE)
    assert confidence(own, "website", SITE + "impressum") == "high"
    assert confidence(own, "website", SITE + "impressum", weak_site=True) == "medium"


def test_email_seen_for_many_places_is_shared():
    candidates = {("node", i): [Candidate("info@builder.biz", "website", "x")] for i in range(SHARED_EMAIL_MIN_PLACES)}
    candidates[("node", 99)] = [Candidate("info@kiosk.de", "website", "x")]
    assert _shared_emails(candidates) == {"info@builder.biz"}


def test_same_website_matches_across_grid_cells():
    # 140 m apart east-west: more than one old grid cell
    osm = Business("node", 1, category="kiosk", name="Büdchen", lat=50.95, lon=6.9200, website="https://x-kiosk.de/")
    place = ExternalPlace("overture", "o", "Trinkhalle Nord", 50.95, 6.9220, "kiosk", "Kiosk", "kiosk",
                          websites=["https://x-kiosk.de/"])
    assert conflate([osm], [place])[0] == 1


def test_category_mapping_edge_cases():
    assert map_category("smokehouse") is None
    assert map_category("smoke_shop") == ("tobacco", "Tabak")
    place = ExternalPlace("overture", "o", "Supermarkt", 50.9, 6.9, "food", "Supermarkt", "international_grocery_store")
    assert place.international == "category: international_grocery_store"


def test_phone_normalisation():
    from b2b_email_finder.utils import is_mobile, normalize_phone

    assert normalize_phone("0221 / 12 34-56") == "+49221123456"
    assert normalize_phone("+49 (0)221 123456") == "+49221123456"
    assert normalize_phone("0049 171 1234567; 0221 99") == "+491711234567"
    assert normalize_phone("123") is None and normalize_phone(None) is None
    assert is_mobile("+491711234567") and not is_mobile("+49221123456")


def test_phone_from_other_source_fills_gap():
    osm = Business("node", 1, category="kiosk", name="Kiosk Ali Baba", lat=50.95, lon=6.92)
    place = ExternalPlace("overture", "o", "Ali Baba Kiosk", 50.95, 6.92, "kiosk", "Kiosk", "kiosk",
                          phones=["+491711234567"])
    conflate([osm], [place])
    assert osm.phone == "+491711234567"


def test_demo_files_are_copied_into_an_empty_results_folder(tmp_path, monkeypatch):
    from b2b_email_finder.cli import _seed_demo_results

    demo, results = tmp_path / "demo", tmp_path / "results"
    demo.mkdir()
    (demo / "koeln.json").write_text("{}")
    monkeypatch.setenv("B2B_DEMO_DIR", str(demo))
    _seed_demo_results(results)
    assert (results / "koeln.json").exists()
    (demo / "other.json").write_text("{}")
    _seed_demo_results(results)  # results folder not empty any more: nothing is added
    assert not (results / "other.json").exists()
