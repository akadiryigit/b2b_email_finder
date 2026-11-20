import json
from pathlib import Path

import pytest

from b2b_email_finder.ui_data import Filters, apply_filters, chip_style, coverage_by, prepare, segment_colors

ROWS = [
    {
        "city": "Köln", "district": "Ehrenfeld", "suburb": "Neuehrenfeld", "category": "kiosk",
        "subtype": "Kiosk", "ownership": "independent", "name": "Büdchen",
        "website": "https://buedchen.de/", "best_email": "info@buedchen.de", "best_email_confidence": "high",
        "lat": 50.95, "lon": 6.92, "osm_url": "https://www.openstreetmap.org/node/1", "emails": [],
    },
    {
        "city": "Köln", "district": "Nippes", "suburb": "Niehl", "category": "fuel", "subtype": "Tankstelle",
        "ownership": "chain", "brand": "Aral", "best_email": None, "lat": 50.99, "lon": 6.96, "emails": [],
    },
    {
        "city": "Pulheim", "district": None, "category": "convenience", "subtype": "Späti / Convenience",
        "ownership": "independent", "international": 'name: "gida"', "name": "Öz Gida",
        "best_email": "oz.gida@web.de", "best_email_confidence": "low", "lat": 50.99, "lon": 6.80, "emails": [],
    },
]


def test_prepare_columns_and_labels():
    df = prepare(ROWS)
    assert list(df["Segment"]) == ["Kiosk", "Tankstelle", "Späti / Convenience"]
    assert list(df["District"]) == ["Ehrenfeld", "Nippes", "(unknown)"]
    assert list(df["Ownership"]) == ["Unabhängig", "Kette", "Unabhängig"]
    assert df.loc[1, "Name"] == "Aral"  # falls back to brand
    assert df.loc[2, "International"] == '✓ name: "gida"'


def test_filters():
    df = prepare(ROWS)
    assert len(apply_filters(df, Filters())) == 3
    assert len(apply_filters(df, Filters(only_with_email=True))) == 2
    assert list(apply_filters(df, Filters(segments=["Tankstelle"]))["Name"]) == ["Aral"]
    assert list(apply_filters(df, Filters(cities=["Köln"], districts=["Ehrenfeld"]))["Name"]) == ["Büdchen"]
    assert list(apply_filters(df, Filters(only_international=True))["Name"]) == ["Öz Gida"]
    assert list(apply_filters(df, Filters(confidence=["high"]))["Name"]) == ["Büdchen"]
    assert list(apply_filters(df, Filters(text="NIEHL"))["Name"]) == ["Aral"]  # Stadtteil is searched too


def test_coverage_by_segment():
    table = coverage_by(prepare(ROWS), "Segment").set_index("Segment")
    assert table.loc["Kiosk", "Coverage %"] == 100.0
    assert table.loc["Tankstelle", "With email"] == 0


def test_segment_colours_are_fixed_and_distinct():
    colors = segment_colors()
    assert colors["Kiosk"] == "#2a78d6" and colors["Tankstelle"] == "#eb6834"
    assert len(set(colors.values())) == len(colors)
    assert "color: #0b0b0b" in chip_style(list(colors)[3])  # dark text on yellow


def test_app_renders_without_errors(tmp_path, monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    payload = {"meta": {"area": "Köln", "generated_at": "2026-11-19T12:00:00+00:00"}, "businesses": ROWS}
    (tmp_path / "koeln.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("B2B_RESULTS_DIR", str(tmp_path))

    app_file = Path(__file__).parents[1] / "src" / "b2b_email_finder" / "app.py"
    at = testing.AppTest.from_file(str(app_file), default_timeout=30).run()
    assert not at.exception
    assert [m.value for m in at.metric][:5] == ["3", "2", "66.7 %", "0", "1"]

    at.toggle(key="only_email").set_value(True).run()
    assert at.metric[0].value == "2"
    at.text_input(key="search_text").set_value("gida").run()
    assert at.metric[0].value == "1"
    assert not at.exception
