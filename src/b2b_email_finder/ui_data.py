"""Data preparation and filtering for the UI — plain pandas, no Streamlit, easy to test."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .categories import CATEGORIES
from .utils import is_mobile

SEGMENT_LABELS = {key: category.label for key, category in CATEGORIES.items()}

# A colour-blind-safe categorical palette (validated with the dataviz skill's
# validator), assigned in the fixed CATEGORIES order. A segment keeps its colour
# whatever the filters show; a 9th segment must fold into "Other", not get a new hue.
CATEGORICAL_SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OTHER_COLOR = "#898781"
DARK_TEXT_ON = {"#eda100"}  # chips use white text, except on yellow
SURFACE = "#fcfcfb"  # ring around map points, so overlapping points stay readable
UNKNOWN = "(unknown)"

OWNERSHIP_LABELS = {
    "independent": "Unabhängig",
    "merchant": "Selbstständiger Kaufmann (wahrsch.)",
    "chain": "Kette",
}
CONFIDENCE_ORDER = ["manual", "high", "medium", "low"]
EMAIL_STATUS_COLORS = {"Email found": "#2a78d6", "No email": "#898781"}  # A7 map colouring

TABLE_COLUMNS = [
    "City", "District", "Segment", "Email", "Confidence", "Phone", "Phone type", "Name", "Subtype", "Ownership",
    "International", "Stadtteil", "Website", "Website from", "Found on", "Sources", "OSM",
]
HIDDEN_COLUMNS = ["Key", "lat", "lon"]


def segment_colors() -> dict[str, str]:
    labels = list(SEGMENT_LABELS.values())
    return {label: CATEGORICAL_SLOTS[i] if i < len(CATEGORICAL_SLOTS) else OTHER_COLOR for i, label in enumerate(labels)}


def color_for(segment: str) -> str:
    return segment_colors().get(segment, OTHER_COLOR)


def chip_style(segment: str) -> str:
    """CSS for a coloured Segment cell in the table."""
    background = color_for(segment)
    text = "#0b0b0b" if background in DARK_TEXT_ON else "#ffffff"
    return f"background-color: {background}; color: {text}; font-weight: 600"


def hex_to_rgba(color: str, alpha: int = 255) -> list[int]:
    color = color.lstrip("#")
    return [int(color[i:i + 2], 16) for i in (0, 2, 4)] + [alpha]


def load_results(path: Path) -> tuple[pd.DataFrame, dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("businesses", [])
    meta = {**payload.get("meta", {}), "has_districts": any("district" in row for row in rows)}
    return prepare(rows), meta


def prepare(rows: list[dict]) -> pd.DataFrame:
    """Turn pipeline rows into the table the UI shows (works with older result files too)."""
    records = []
    for row in rows:
        international = row.get("international")
        records.append(
            {
                "City": row.get("city") or row.get("addr_city") or UNKNOWN,
                "District": row.get("district") or UNKNOWN,
                "Segment": SEGMENT_LABELS.get(row.get("category"), row.get("category") or UNKNOWN),
                "Email": row.get("best_email") or "",
                "Confidence": row.get("best_email_confidence") or "",
                "Phone": row.get("phone") or "",
                "Phone type": ("mobile" if is_mobile(row.get("phone")) else "landline") if row.get("phone") else "",
                "Name": row.get("name") or row.get("brand") or "(no name)",
                "Subtype": row.get("subtype") or "",
                "Ownership": OWNERSHIP_LABELS.get(row.get("ownership") or "independent", row.get("ownership")),
                "International": f"✓ {international}" if international else "",
                "Stadtteil": row.get("suburb") or "",
                "Website": row.get("website") or "",
                "Website from": row.get("website_origin") or "",
                "Found on": row.get("best_email_source_url") or "",
                "Sources": ", ".join(row.get("sources") or ["osm"]),
                "OSM": row.get("osm_url") or "",
                "Key": f"{row.get('osm_type')}/{row.get('osm_id')}",
                "lat": row.get("lat"),
                "lon": row.get("lon"),
            }
        )
    return pd.DataFrame.from_records(records, columns=TABLE_COLUMNS + HIDDEN_COLUMNS)


# ---------------------------------------------------------------- A4 manual review, A5 suppression
REVIEWS_FILE = "reviews.json"


def load_reviews(path: Path) -> dict[str, dict]:
    """{place key: {"email": reviewed email, "verdict": "correct"|"wrong", "corrected": str}}"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_reviews(path: Path, reviews: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reviews, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_reviews(df: pd.DataFrame, reviews: dict[str, dict]) -> pd.DataFrame:
    """Manual corrections win: a corrected email replaces the found one, a wrong one is removed.

    Reviews are kept per place, so they survive new search runs.
    """
    df = df.copy()
    df["Review"] = ""
    for index, key in df["Key"].items():
        review = reviews.get(key)
        if not review:
            continue
        corrected = (review.get("corrected") or "").strip().lower()
        if corrected:
            df.loc[index, ["Email", "Confidence", "Review"]] = [corrected, "manual", "✎ corrected"]
        elif review.get("verdict") == "correct" and review.get("email") == df.loc[index, "Email"]:
            df.loc[index, ["Confidence", "Review"]] = ["manual", "✓ correct"]
        elif review.get("verdict") == "wrong" and review.get("email") == df.loc[index, "Email"]:
            df.loc[index, ["Email", "Confidence", "Review"]] = ["", "", "✗ wrong"]
    return df


def precision(reviews: dict[str, dict]) -> tuple[int, int]:
    """(correct, reviewed) — corrected emails count as wrong finds."""
    verdicts = [r.get("verdict") for r in reviews.values() if r.get("verdict") in ("correct", "wrong")]
    return verdicts.count("correct"), len(verdicts)


def apply_suppression(df: pd.DataFrame, entries: set[str]) -> pd.DataFrame:
    if not entries:
        return df
    df = df.copy()
    emails = df["Email"].str.lower()
    hit = (emails != "") & (emails.isin(entries) | emails.str.rpartition("@")[2].isin(entries))
    df.loc[hit, ["Email", "Confidence"]] = ["", ""]
    df.loc[hit, "Review"] = "⛔ never contact"
    return df


# ---------------------------------------------------------------- A1/A2 exports
EMAIL_LIST_COLUMNS = ["email", "name", "segment", "city", "district", "confidence", "found_on"]


def email_list(df: pd.DataFrame) -> pd.DataFrame:
    """One row per email address (duplicates removed), ready to import into a newsletter tool."""
    with_email = df[df["Email"] != ""]
    rows = pd.DataFrame(
        {
            "email": with_email["Email"], "name": with_email["Name"], "segment": with_email["Segment"],
            "city": with_email["City"], "district": with_email["District"],
            "confidence": with_email["Confidence"], "found_on": with_email["Found on"],
        }
    )
    return rows.drop_duplicates(subset="email").reset_index(drop=True)


def to_excel(df: pd.DataFrame) -> bytes:
    """Summary sheet + one sheet per segment, coloured segment cells, filters, frozen header."""
    from io import BytesIO

    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    columns = [c for c in TABLE_COLUMNS if c in df.columns] + (["Review"] if "Review" in df.columns else [])
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        coverage_by(df, "Segment").to_excel(writer, sheet_name="Summary", index=False)
        df[columns].to_excel(writer, sheet_name="All places", index=False)
        for segment in [s for s in SEGMENT_LABELS.values() if s in set(df["Segment"])]:
            sheet = re.sub(r"[\[\]:*?/\\]", "-", segment)[:31]
            df[df["Segment"] == segment][columns].to_excel(writer, sheet_name=sheet, index=False)
        email_list(df).to_excel(writer, sheet_name="Email list", index=False)

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for cell in worksheet[1]:
                cell.font = Font(bold=True)
            headers = [cell.value for cell in worksheet[1]]
            for index, header in enumerate(headers, start=1):
                width = 12 if header in ("Confidence", "Sources") else 28 if header in ("Email", "email", "Name") else 18
                worksheet.column_dimensions[get_column_letter(index)].width = width
            for header in ("Segment", "segment"):
                if header not in headers:
                    continue
                column = headers.index(header) + 1
                for row in worksheet.iter_rows(min_row=2, min_col=column, max_col=column):
                    cell = row[0]
                    color = color_for(str(cell.value))
                    cell.fill = PatternFill("solid", start_color="FF" + color.lstrip("#").upper())
                    cell.font = Font(bold=True, color="0B0B0B" if color in DARK_TEXT_ON else "FFFFFF")
    return buffer.getvalue()


# ---------------------------------------------------------------- A6 compare runs
def compare_runs(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    """What changed between two result files, matched by place key."""
    old_emails = old.set_index("Key")["Email"].to_dict()
    new_indexed = new.set_index("Key")
    gained, lost, changed = [], [], []
    for key, row in new_indexed.iterrows():
        before, after = old_emails.get(key, ""), row["Email"]
        if after and not before:
            gained.append(key)
        elif before and not after and key in old_emails:
            lost.append(key)
        elif before and after and before != after:
            changed.append(key)
    by_segment = coverage_by(new, "Segment")[["Segment", "Places", "With email", "Coverage %"]].merge(
        coverage_by(old, "Segment")[["Segment", "With email", "Coverage %"]],
        on="Segment", how="left", suffixes=("", " before"),
    ).fillna(0)
    by_segment["Δ emails"] = (by_segment["With email"] - by_segment["With email before"]).astype(int)
    columns = ["City", "District", "Segment", "Email", "Confidence", "Name", "Found on"]
    return {
        "gained": new_indexed.loc[gained, columns].reset_index(drop=True) if gained else pd.DataFrame(columns=columns),
        "lost": len(lost),
        "changed": len(changed),
        "new_places": int((~new["Key"].isin(old["Key"])).sum()),
        "by_segment": by_segment,
    }


@dataclass
class Filters:
    text: str = ""
    segments: list[str] = field(default_factory=list)  # empty list = no filter
    cities: list[str] = field(default_factory=list)
    districts: list[str] = field(default_factory=list)
    ownership: list[str] = field(default_factory=list)
    confidence: list[str] = field(default_factory=list)
    only_with_email: bool = False
    only_international: bool = False
    only_with_phone: bool = False


def apply_filters(df: pd.DataFrame, filters: Filters) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for column, wanted in (
        ("Segment", filters.segments), ("City", filters.cities), ("District", filters.districts),
        ("Ownership", filters.ownership), ("Confidence", filters.confidence),
    ):
        if wanted:
            mask &= df[column].isin(wanted)
    if filters.only_with_email:
        mask &= df["Email"] != ""
    if filters.only_with_phone:
        mask &= df["Phone"] != ""
    if filters.only_international:
        mask &= df["International"] != ""
    text = filters.text.strip().lower()
    if text:
        haystack = df[["Name", "Email", "Phone", "City", "District", "Stadtteil", "Subtype"]].agg(" ".join, axis=1)
        mask &= haystack.str.lower().str.contains(text, regex=False)
    return df[mask]


def coverage_by(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Email coverage per segment / district: places, with website, with email, %."""
    grouped = df.groupby(column, sort=False).agg(
        Places=("Name", "size"),
        **{"With website": ("Website", lambda s: int((s != "").sum()))},
        **{"With email": ("Email", lambda s: int((s != "").sum()))},
    )
    grouped["Coverage %"] = (100 * grouped["With email"] / grouped["Places"]).round(1)
    return grouped.sort_values("Places", ascending=False).reset_index()
