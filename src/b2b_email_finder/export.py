"""Step 4: write results as JSON (complete) and CSV (one flat row per business)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ATTRIBUTION = "Business data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"

CSV_COLUMNS = [
    "city", "district", "suburb", "category", "subtype", "best_email", "best_email_confidence", "phone",
    "name", "brand", "operator", "ownership", "international",
    "website", "website_shared_by", "crawl_status",
    "best_email_status", "best_email_kind", "best_email_source", "best_email_source_url",
    "other_emails", "lat", "lon", "osm_url", "scraped_at",
]


def save_search(result, categories: list[str], out_base: Path) -> tuple[Path, Path]:
    """Write <out_base>.json and <out_base>.csv for a pipeline.SearchResult."""
    out_base.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_base.parent / f"{out_base.name}.json"
    csv_path = out_base.parent / f"{out_base.name}.csv"
    meta = {
        "area": result.area.display_name,
        "categories": categories,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overpass_query": result.query,
        "stats": result.stats,
    }
    write_json(json_path, meta, result.rows)
    write_csv(csv_path, result.rows)
    return json_path, csv_path


def write_json(path: Path, meta: dict, rows: list[dict]) -> None:
    payload = {"meta": {**meta, "attribution": ATTRIBUTION}, "businesses": rows}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    # utf-8-sig adds a BOM so Excel shows umlauts correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            others = [e for e in row["emails"] if e["email"] != row["best_email"]]
            writer.writerow(
                {**row, "other_emails": "; ".join(f"{e['email']} ({e['status']})" for e in others)}
            )
