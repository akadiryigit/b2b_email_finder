from b2b_email_finder.categories import CATEGORIES, match_tags
from b2b_email_finder.pipeline import Candidate, _is_head_office_email
from b2b_email_finder.models import Business
from b2b_email_finder.segments import international_hint, ownership
from b2b_email_finder.verify import MX, classify, confidence

ALL = list(CATEGORIES.values())


def test_match_tags_segments_and_subtypes():
    assert match_tags({"shop": "kiosk"}, ALL) == ("kiosk", "Kiosk")
    assert match_tags({"shop": "e-cigarette"}, ALL) == ("vape", "Vape-Shop")
    assert match_tags({"shop": "lottery"}, ALL) == ("tobacco", "Lotto")
    assert match_tags({"amenity": "hookah_lounge"}, ALL) == ("shisha_bar", "Shisha-Bar")
    assert match_tags({"shop": "beverages"}, ALL) == ("food", "Getränkemarkt")
    # a gas station that also has shop=kiosk for its shop is a Tankstelle
    assert match_tags({"amenity": "fuel", "shop": "kiosk"}, ALL) == ("fuel", "Tankstelle")
    assert match_tags({"shop": "bakery"}, ALL) is None


def test_eight_segments_fit_the_palette():
    assert len(CATEGORIES) <= 8


def test_ownership():
    assert ownership({}) == "independent"
    assert ownership({"brand": "Aral"}) == "chain"
    assert ownership({"brand": "Lidl"}) == "discounter"
    assert ownership({"brand": "EDEKA", "name": "EDEKA Müller"}) == "merchant"
    assert ownership({"brand": "REWE", "name": "REWE City"}) == "chain"
    assert ownership({"brand": "REWE", "name": "REWE", "operator": "Schmitz oHG"}) == "merchant"
    assert ownership({"brand": "Lotto"}) == "independent"  # service brand, not an owner


def test_international_hint():
    assert international_hint({"name": "Istanbul Market"}) == 'name: "istanbul"'
    assert international_hint({"name": "Öz Gida"}) == 'name: "gida"'
    assert international_hint({"name": "Kiosk", "diet:halal": "yes"}) == "diet:halal=yes"
    assert international_hint({"name": "Büdchen am Ring"}) is None


def _business(website):
    return Business(osm_type="node", osm_id=1, category="fuel", website=website)


def test_head_office_emails_are_excluded():
    jet = _business("https://www.jet.de/")
    assert _is_head_office_email(Candidate("info@jet.de", "website", "https://www.jet.de/impressum"), jet, 4)
    assert _is_head_office_email(Candidate("datens@de.bp.com", "website", "x"), _business("https://aral.de/"), 1)
    # unknown chain domain, but the website is shared by several places
    shared = _business("https://www.minichain.de/")
    assert _is_head_office_email(Candidate("info@minichain.de", "website", "x"), shared, 3)
    # a merchant's own address on a shared chain page is kept
    edeka = _business("https://www.edeka.de/eh/mueller")
    assert not _is_head_office_email(Candidate("info@edeka-mueller.de", "website", "x"), edeka, 5)
    # own site, not shared
    own = _business("https://kiosk-ali.de/")
    assert not _is_head_office_email(Candidate("info@kiosk-ali.de", "website", "x"), own, 1)


def test_confidence():
    site = "https://kiosk.de/"
    assert confidence(classify("info@kiosk.de", MX, site), "website", "https://kiosk.de/impressum") == "high"
    assert confidence(classify("info@kiosk.de", MX, site), "osm", "https://osm.org/node/1") == "high"
    assert confidence(classify("info@kiosk.de", MX, site), "website", "https://kiosk.de/") == "medium"
    assert confidence(classify("ali@gmx.de", MX, site), "website", "https://kiosk.de/") == "low"


def test_drugstores_and_discounters_by_name_are_excluded():
    assert ownership({"name": "dm-drogerie markt"}) == "discounter"
    assert ownership({"name": "Rossmann"}) == "discounter"
    assert ownership({"name": "Lidl"}) == "discounter"
    assert ownership({"name": "Kiosk Müller"}) == "independent"
