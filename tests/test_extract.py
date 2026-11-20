from b2b_email_finder.extract import analyse_page, clean_email, is_contact_page

PAGE = """
<html><head>
<script type="application/ld+json">
  {"@type": "LocalBusiness", "name": "Kiosk Ehrenfeld", "contactPoint": {"email": "mailto:Bestellung@Kiosk-Ehrenfeld.de"}}
</script>
<script>var tracker = "track@analytics-vendor.com";</script>
</head><body>
  <a href="/impressum">Impressum</a>
  <a href="/ueber-uns">Über uns</a>
  <a href="https://kiosk-ehrenfeld.de/kontakt?ref=nav#top">Kontakt</a>
  <a href="https://facebook.com/kiosk">Facebook Kontakt</a>
  <a href="mailto:info@kiosk-ehrenfeld.de?subject=Hallo">Schreib uns</a>
  <img src="logo@2x.png">
  <p>Inhaber: Max, E-Mail: chef@kiosk-ehrenfeld.de.</p>
  <p>Beispiel: max.mustermann@example.com, info [at] kiosk-ehrenfeld.de</p>
</body></html>
"""


def test_extracts_mailto_jsonld_and_text_emails():
    info = analyse_page(PAGE, "https://kiosk-ehrenfeld.de/")
    assert info.emails == [
        "info@kiosk-ehrenfeld.de",
        "bestellung@kiosk-ehrenfeld.de",
        "chef@kiosk-ehrenfeld.de",
    ]


def test_ignores_scripts_images_placeholders_and_obfuscated_addresses():
    emails = analyse_page(PAGE, "https://kiosk-ehrenfeld.de/").emails
    assert "track@analytics-vendor.com" not in emails
    assert not any("example.com" in e or "png" in e for e in emails)


def test_contact_links_same_site_and_prioritised():
    links = analyse_page(PAGE, "https://kiosk-ehrenfeld.de/").contact_links
    assert links == [
        "https://kiosk-ehrenfeld.de/impressum",
        "https://kiosk-ehrenfeld.de/kontakt?ref=nav",
        "https://kiosk-ehrenfeld.de/ueber-uns",
    ]


def test_clean_email():
    assert clean_email(" Info@Kiosk.DE. ") == "info@kiosk.de"
    assert clean_email("mailto:info%40kiosk.de") == "info@kiosk.de"
    assert clean_email("icon@2x.webp") is None
    assert clean_email("not-an-email") is None
    assert clean_email("ihre.email@kiosk.de") is None


def test_is_contact_page():
    assert is_contact_page("https://a.de/de/Impressum.html")
    assert is_contact_page("https://a.de/kontakt/")
    assert not is_contact_page("https://a.de/")
