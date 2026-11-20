from b2b_email_finder.verify import (
    A_FALLBACK, MX, NONE, NOT_CHECKED, NULL_MX, address_kind, check_syntax, classify, rank,
)


def test_check_syntax():
    assert check_syntax("info@kiosk.de")
    assert not check_syntax("info..x@kiosk.de")
    assert not check_syntax(".info@kiosk.de")
    assert not check_syntax(("a" * 65) + "@kiosk.de")


def test_address_kind():
    assert address_kind("info@kiosk.de") == "role"
    assert address_kind("info+web@kiosk.de") == "role"
    assert address_kind("datenschutz@kiosk.de") == "low_priority"
    assert address_kind("no-reply@kiosk.de") == "no_reply"
    assert address_kind("ayse.yilmaz@kiosk.de") == "other"


def test_classify_statuses():
    site = "https://www.kiosk.de/"
    assert classify("info@kiosk.de", MX, site).status == "ok"
    assert classify("info@kiosk.de", A_FALLBACK, site).status == "ok"
    assert classify("info@kiosk.de", NONE, site).status == "no_mail_server"
    assert classify("info@kiosk.de", NULL_MX, site).status == "no_mail_server"
    assert classify("info@kiosk.de", NOT_CHECKED, site).status == "unverified"
    assert classify("noreply@kiosk.de", MX, site).status == "no_reply"
    assert classify("x@mailinator.com", MX, site).status == "disposable"


def test_classify_website_match_and_freemail():
    check = classify("info@shop.kiosk.de", MX, "https://www.kiosk.de/")
    assert check.matches_website is True
    gmx = classify("kiosk.ali@gmx.de", MX, "https://www.kiosk.de/")
    assert gmx.matches_website is False and gmx.freemail
    assert classify("info@kiosk.de", MX, None).matches_website is None


def test_rank_prefers_matching_role_address_from_impressum():
    site = "https://kiosk.de/"
    impressum = rank(classify("info@kiosk.de", MX, site), "website", "https://kiosk.de/impressum")
    homepage_other = rank(classify("ali@kiosk.de", MX, site), "website", "https://kiosk.de/")
    freemail = rank(classify("kiosk@web.de", MX, site), "website", "https://kiosk.de/impressum")
    privacy = rank(classify("datenschutz@kiosk.de", MX, site), "website", "https://kiosk.de/impressum")
    dead = rank(classify("info@kiosk.de", NONE, site), "website", "https://kiosk.de/impressum")
    assert impressum > homepage_other > freemail > dead
    assert impressum > privacy
