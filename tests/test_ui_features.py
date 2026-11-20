import asyncio
import json
from io import BytesIO
from pathlib import Path

import pytest

from b2b_email_finder import jobs
from b2b_email_finder.ui_data import (
    apply_reviews, apply_suppression, compare_runs, email_list, precision, prepare, to_excel,
)

ROWS = [
    {"osm_type": "node", "osm_id": 1, "city": "Köln", "district": "Ehrenfeld", "category": "kiosk",
     "name": "Büdchen", "best_email": "info@buedchen.de", "best_email_confidence": "high", "lat": 50.95, "lon": 6.92},
    {"osm_type": "node", "osm_id": 2, "city": "Köln", "district": "Nippes", "category": "vape",
     "name": "Cloud Vape", "best_email": "shop@cloud.de", "best_email_confidence": "medium", "lat": 50.96, "lon": 6.93},
    {"osm_type": "overture", "osm_id": "o1", "city": "Köln", "district": "Kalk", "category": "kiosk",
     "name": "Kiosk Kalk", "best_email": "info@buedchen.de", "best_email_confidence": "low", "lat": 50.94, "lon": 7.0},
    {"osm_type": "node", "osm_id": 3, "city": "Köln", "district": "Kalk", "category": "fuel",
     "name": "Freie Tankstelle", "best_email": None, "lat": 50.93, "lon": 7.01},
]


def test_reviews_correct_wrong_and_corrected():
    reviews = {
        "node/1": {"email": "info@buedchen.de", "verdict": "correct", "corrected": ""},
        "node/2": {"email": "shop@cloud.de", "verdict": "wrong", "corrected": ""},
        "node/3": {"email": "", "verdict": "wrong", "corrected": "Tank@Frei.de"},
    }
    df = apply_reviews(prepare(ROWS), reviews).set_index("Key")
    assert df.loc["node/1", ["Confidence", "Review"]].tolist() == ["manual", "✓ correct"]
    assert df.loc["node/2", ["Email", "Review"]].tolist() == ["", "✗ wrong"]
    assert df.loc["node/3", ["Email", "Confidence"]].tolist() == ["tank@frei.de", "manual"]
    assert precision(reviews) == (1, 3)


def test_review_of_an_older_email_does_not_hide_a_new_one():
    reviews = {"node/2": {"email": "old@cloud.de", "verdict": "wrong", "corrected": ""}}
    assert apply_reviews(prepare(ROWS), reviews).set_index("Key").loc["node/2", "Email"] == "shop@cloud.de"


def test_suppression_hides_emails_and_domains():
    df = apply_reviews(prepare(ROWS), {})
    hidden = apply_suppression(df, {"cloud.de"}).set_index("Key")
    assert hidden.loc["node/2", ["Email", "Review"]].tolist() == ["", "⛔ never contact"]
    assert hidden.loc["node/1", "Email"] == "info@buedchen.de"


def test_email_list_dedupes():
    emails = email_list(prepare(ROWS))
    assert list(emails["email"]) == ["info@buedchen.de", "shop@cloud.de"]
    assert list(emails.columns) == ["email", "name", "segment", "city", "district", "confidence", "found_on"]


def test_excel_workbook():
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.load_workbook(BytesIO(to_excel(apply_reviews(prepare(ROWS), {}))))
    assert book.sheetnames == ["Summary", "All places", "Kiosk", "Tankstelle", "Vape & Shisha-Shop", "Email list"]
    sheet = book["All places"]
    segment_col = [c.value for c in sheet[1]].index("Segment") + 1
    assert sheet.cell(row=2, column=segment_col).fill.start_color.rgb.upper().endswith("2A78D6")  # Kiosk blue
    assert sheet.freeze_panes == "A2"


def test_compare_runs():
    old = prepare(ROWS[:2] + [{**ROWS[3]}])
    new_rows = [ROWS[0], {**ROWS[1], "best_email": None}, ROWS[2], {**ROWS[3], "best_email": "tank@frei.de"}]
    diff = compare_runs(old, prepare(new_rows))
    assert list(diff["gained"]["Email"]) == ["info@buedchen.de", "tank@frei.de"]  # new place + newly found
    assert (diff["lost"], diff["changed"], diff["new_places"]) == (1, 0, 1)
    kiosk = diff["by_segment"].set_index("Segment").loc["Kiosk"]
    assert kiosk["Δ emails"] == 1


def test_search_job_runs_saves_and_can_be_cancelled(tmp_path, monkeypatch):
    class Result:
        rows, stats = [], {}

    async def quick(opts):
        return Result()

    async def slow(opts):
        await asyncio.sleep(30)

    monkeypatch.setattr(jobs, "save_search", lambda result, cats, base: (base.with_suffix(".json"), None))
    monkeypatch.setattr(jobs, "run_search", quick)
    job = jobs.SearchJob(jobs.SearchOptions(area="Köln", categories=["kiosk"]), tmp_path / "x").start()
    job.join(5)
    assert job.done and job.json_path == tmp_path / "x.json" and not job.error

    monkeypatch.setattr(jobs, "run_search", slow)
    job = jobs.SearchJob(jobs.SearchOptions(area="Köln", categories=["kiosk"]), tmp_path / "y").start()
    while job._task is None:
        pass
    job.cancel()
    job.join(5)
    assert job.done and job.cancelled


def test_app_with_two_runs(tmp_path, monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    for name, rows in (("old.json", ROWS[:2]), ("new.json", ROWS)):
        payload = {"meta": {"area": "Köln"}, "businesses": rows}
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "reviews.json").write_text(json.dumps({"node/2": {"email": "shop@cloud.de", "verdict": "wrong"}}))
    (tmp_path / "suppress.txt").write_text("info@buedchen.de\n")
    monkeypatch.setenv("B2B_RESULTS_DIR", str(tmp_path))

    app_file = Path(__file__).parents[1] / "src" / "b2b_email_finder" / "app.py"
    at = testing.AppTest.from_file(str(app_file), default_timeout=60).run()
    assert not at.exception
    assert at.selectbox(key="result_file").options == ["new.json", "old.json"]
    # suppressed info@buedchen.de (2 places) and reviewed-wrong shop@cloud.de leave no email
    assert [m.value for m in at.metric][:2] == ["4", "0"]
    labels = [b.label for b in at.get("download_button")]
    assert any(label.startswith("Email list") for label in labels) and "Excel workbook" in labels
    assert at.selectbox(key="compare_with").value == "old.json"
