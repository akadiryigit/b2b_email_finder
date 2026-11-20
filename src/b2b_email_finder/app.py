"""Web UI: browse result files, filter, export, review, compare runs, map, start searches.

Start with:  b2b-email-finder ui
(or directly: streamlit run src/b2b_email_finder/app.py)

Streamlit re-runs this whole script top to bottom on every click; widget values
live in st.session_state, and @st.cache_data avoids re-reading files each time.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

import pydeck as pdk
import streamlit as st

from b2b_email_finder import suppression
from b2b_email_finder.categories import CATEGORIES
from b2b_email_finder.export import ATTRIBUTION
from b2b_email_finder.jobs import SearchJob
from b2b_email_finder.pipeline import PLACE_SOURCES, SearchOptions
from b2b_email_finder.ui_data import (
    CONFIDENCE_ORDER, EMAIL_STATUS_COLORS, REVIEWS_FILE, SEGMENT_LABELS, SURFACE, TABLE_COLUMNS, Filters,
    apply_filters, apply_reviews, apply_suppression, chip_style, color_for, compare_runs, coverage_by,
    email_list, hex_to_rgba, load_results, load_reviews, precision, save_reviews, to_excel,
)

RESULTS_DIR = Path(os.environ.get("B2B_RESULTS_DIR", "results"))
SUPPRESS_PATH = RESULTS_DIR / suppression.DEFAULT_FILE
REVIEWS_PATH = RESULTS_DIR / REVIEWS_FILE

st.set_page_config(page_title="B2B Email Finder", page_icon="📇", layout="wide")


@st.cache_data(show_spinner=False)
def cached_results(path: str, modified: float):
    return load_results(Path(path))


@st.cache_resource
def job_slot() -> dict:
    """One search at a time, shared by all browser tabs (survives page reloads)."""
    return {"job": None}


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text.lower()).strip("-") or "search"


def legend(segments: list[str], counts=None) -> None:
    chips = []
    for segment in segments:
        count = f" ({counts.get(segment, 0)})" if counts is not None else ""
        chips.append(
            f'<span style="{chip_style(segment)}; padding: 2px 8px; border-radius: 4px; '
            f'margin-right: 6px; white-space: nowrap; font-size: 0.85rem">{segment}{count}</span>'
        )
    st.markdown(" ".join(chips), unsafe_allow_html=True)


# ---------------------------------------------------------------- A8 background search
def search_form(expanded: bool) -> None:
    slot = job_slot()
    running = slot["job"] is not None and not slot["job"].done
    with st.sidebar.expander("New search", icon="🔎", expanded=expanded):
        with st.form("search"):
            area = st.text_input("Area", "Köln", help='City, district or state, e.g. "Köln", "Bayern"')
            categories = st.multiselect(
                "Segments", list(CATEGORIES), default=list(CATEGORIES), format_func=lambda k: SEGMENT_LABELS[k]
            )
            limit = st.number_input("Max OSM places (0 = all)", min_value=0, max_value=50_000, value=0, step=50)
            sources = st.multiselect(
                "Extra place databases", list(PLACE_SOURCES), default=list(PLACE_SOURCES),
                format_func=str.capitalize, help="Foursquare needs FSQ_PLACES_PARQUET (see README)",
            )
            crawl = st.checkbox(
                "Visit websites to find emails", value=True,
                help="Visits the shops' websites. Best run from a private network, not a company one.",
            )
            discover = st.checkbox("Find missing websites (guess + web search)", value=True,
                                   help="Web search needs BRAVE_API_KEY; guessing is free")
            archive = st.checkbox(
                "Use Common Crawl archive for unreachable sites", value=False,
                help="Slow and often overloaded: in the Köln test it found 1 email in 20 minutes",
            )
            submitted = st.form_submit_button("Run search", type="primary", width="stretch", disabled=running)
    if not submitted:
        return
    if not area.strip() or not categories:
        st.sidebar.error("Enter an area and pick at least one segment.")
        return
    opts = SearchOptions(
        area=area.strip(), categories=categories, limit=int(limit) or None, sources=sources,
        crawl=crawl, discover=discover, commoncrawl=archive, suppress=suppression.load(SUPPRESS_PATH),
    )
    suffix = "crawled" if crawl else "osm-only"
    out_base = RESULTS_DIR / _slug(f"{area}-{'-'.join(['osm', *sources])}-{suffix}")
    slot["job"] = SearchJob(opts, out_base).start()
    st.rerun()


@st.fragment(run_every=2)
def job_status() -> None:
    """Re-runs every 2 s on its own; called inside `with st.sidebar:`."""
    job = job_slot()["job"]
    if job is None:
        return
    if not job.done:
        with st.status(f"Search running … {job.elapsed}", expanded=True):
            for line in job.messages[-8:]:
                st.caption(line)
            if st.button("Cancel search", icon="✋"):
                job.cancel()
        return
    if job.error:
        st.error(job.messages[-1])
    elif job.cancelled:
        st.warning("Search cancelled.")
    elif job.json_path is not None and not job.shown:
        job.shown = True
        st.session_state["result_file"] = job.json_path.name
        st.rerun(scope="app")
    else:
        st.success(job.messages[-1])
    if st.button("Clear search status"):
        job_slot()["job"] = None
        st.rerun(scope="app")


# ---------------------------------------------------------------- A5 never-contact list
def suppression_box() -> set[str]:
    entries = suppression.load(SUPPRESS_PATH)
    with st.sidebar.expander(f"Never-contact list ({len(entries)})", icon="⛔"):
        new = st.text_area("Add emails or domains (one per line)", key="suppress_input", height=80)
        if st.button("Add to list") and new.strip():
            added = suppression.add(SUPPRESS_PATH, new.splitlines())
            st.toast(f"Added {len(added)} entr{'y' if len(added) == 1 else 'ies'}")
            st.rerun()
        if entries:
            st.caption(", ".join(sorted(entries)[:30]) + (" …" if len(entries) > 30 else ""))
    return entries


# ---------------------------------------------------------------- views
def table_view(view, file_stem: str) -> None:
    legend(list(SEGMENT_LABELS.values()))
    columns = TABLE_COLUMNS + ["Review"]
    event = st.dataframe(
        view[columns].style.map(chip_style, subset=["Segment"]),
        hide_index=True,
        height=540,
        key="table",
        on_select="rerun",
        selection_mode="multi-row",
        column_config={
            "Email": st.column_config.TextColumn("Email", width="medium"),
            "Confidence": st.column_config.TextColumn(
                "Confidence", help="manual: checked by you · high: email attached to this place, or own domain "
                "on its own Impressum · medium · low"
            ),
            "Website": st.column_config.LinkColumn("Website"),
            "Website from": st.column_config.TextColumn("Website from", help="osm / overture / foursquare / guess / search"),
            "Found on": st.column_config.LinkColumn("Found on", help="Page where the email is published"),
            "OSM": st.column_config.LinkColumn("OSM", display_text="map"),
        },
    )
    selected = view.iloc[event.selection.rows] if event and event.selection.rows else view.iloc[0:0]

    b1, b2, b3, b4 = st.columns(4)
    b1.download_button(
        "Filtered table (CSV)", view[columns].to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{file_stem}-filtered.csv", mime="text/csv", icon="⬇️", width="stretch",
    )
    emails = email_list(view)
    b2.download_button(
        f"Email list ({len(emails)}) (CSV)", emails.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{file_stem}-emails.csv", mime="text/csv", icon="✉️", width="stretch",
        help="One row per email, duplicates removed — import into Brevo / Mailchimp",
    )
    b3.download_button(
        # A function, not the bytes: the workbook is only built when the button is clicked.
        "Excel workbook", lambda: to_excel(view), file_name=f"{file_stem}.xlsx", icon="📊", width="stretch",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Summary, all places, one sheet per segment, email list",
    )
    b4.download_button(
        f"Selected rows ({len(selected)}) (CSV)", selected[columns].to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{file_stem}-selected.csv", mime="text/csv", icon="☑️", width="stretch",
        disabled=selected.empty, help="Tick rows in the table first",
    )


def coverage_view(view) -> None:
    st.caption("Email coverage = places with a usable, place-specific email (head-office emails never count).")
    progress = st.column_config.ProgressColumn("Coverage %", min_value=0, max_value=100, format="%.1f %%")
    left, right = st.columns(2)
    with left:
        st.subheader("By segment")
        st.dataframe(
            coverage_by(view, "Segment").style.map(chip_style, subset=["Segment"]),
            hide_index=True, column_config={"Coverage %": progress},
        )
    with right:
        st.subheader("By district")
        st.dataframe(coverage_by(view, "District"), hide_index=True, column_config={"Coverage %": progress}, height=420)


def map_view(view) -> None:
    points = view.dropna(subset=["lat", "lon"])
    if points.empty:
        st.info("No places with coordinates for the current filters.")
        return
    color_by = st.radio("Colour by", ["Segment", "Email found"], horizontal=True, key="map_color")
    if color_by == "Segment":
        groups = points["Segment"]
        colors = {s: color_for(s) for s in groups.unique()}
    else:
        groups = (points["Email"] != "").map({True: "Email found", False: "No email"})
        colors = EMAIL_STATUS_COLORS
    points = points.assign(
        group=groups,
        color=groups.map(lambda g: hex_to_rgba(colors[g], 235)),
        email=points["Email"].replace("", "–"),
        phone=points["Phone"].replace("", "–"),
        place=points["City"] + " · " + points["District"],
    )[["lon", "lat", "color", "group", "Name", "Segment", "Subtype", "email", "phone", "place"]]

    span = max(points["lat"].max() - points["lat"].min(), points["lon"].max() - points["lon"].min(), 0.01)
    view_state = pdk.ViewState(
        latitude=float(points["lat"].mean()),
        longitude=float(points["lon"].mean()),
        zoom=min(max(math.log2(360 / span) - 0.6, 4), 15),
    )
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=points,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_line_color=hex_to_rgba(SURFACE),
        stroked=True,
        line_width_min_pixels=2,
        get_radius=30,
        radius_min_pixels=5,   # >= 10 px diameter, readable at city zoom
        radius_max_pixels=10,
        pickable=True,
        auto_highlight=True,
    )
    tooltip = {
        "html": "<b>{Name}</b><br/>{Segment} · {Subtype}<br/>{place}<br/>✉ {email}<br/>☎ {phone}",
        "style": {"backgroundColor": "#ffffff", "color": "#0b0b0b", "fontSize": "13px"},
    }
    counts = points["group"].value_counts().to_dict()
    if color_by == "Segment":
        legend([s for s in SEGMENT_LABELS.values() if s in counts], counts)
    else:
        st.markdown(" &nbsp; ".join(
            f'<span style="color:{c}">●</span> {g} ({counts.get(g, 0)})' for g, c in colors.items()
        ), unsafe_allow_html=True)
    st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view_state, map_style="light", tooltip=tooltip), height=620)


def review_view(view, reviews: dict) -> None:
    """A4: check emails by hand. Gives the precision figure for the challenge."""
    correct, reviewed = precision(reviews)
    c1, c2, c3 = st.columns(3)
    c1.metric("Reviewed", reviewed, border=True)
    c2.metric("Correct", correct, border=True)
    c3.metric("Precision", f"{100 * correct / reviewed:.0f} %" if reviewed else "–", border=True,
              help="Share of checked emails that really belong to the place")

    candidates = view[(view["Email"] != "") | (view["Review"] != "")]
    if candidates.empty:
        st.info("No emails in the current filter to review.")
        return
    left, right = st.columns([1, 3])
    if left.button("Pick 50 random emails", icon="🎲"):
        st.session_state["review_keys"] = list(candidates.sample(min(50, len(candidates)))["Key"])
    keys = [k for k in st.session_state.get("review_keys", []) if k in set(candidates["Key"])]
    sample = candidates[candidates["Key"].isin(keys)] if keys else candidates.head(50)
    right.caption("Open “Found on”, then mark the email. A corrected email replaces the found one in every view and "
                  "export. Reviews are stored per place in results/reviews.json and survive new runs.")

    table = sample[["Key", "Name", "Segment", "District", "Email", "Found on"]].copy()
    table["Verdict"] = [reviews.get(k, {}).get("verdict", "") for k in table["Key"]]
    table["Corrected email"] = [reviews.get(k, {}).get("corrected", "") for k in table["Key"]]
    edited = st.data_editor(
        table,
        hide_index=True,
        key="review_editor",
        disabled=["Key", "Name", "Segment", "District", "Email", "Found on"],
        column_config={
            "Key": None,  # hidden
            "Found on": st.column_config.LinkColumn("Found on"),
            "Verdict": st.column_config.SelectboxColumn("Verdict", options=["", "correct", "wrong"]),
            "Corrected email": st.column_config.TextColumn("Corrected email"),
        },
    )
    if st.button("Save reviews", type="primary", icon="💾"):
        for _, row in edited.iterrows():
            verdict, corrected = row["Verdict"] or "", (row["Corrected email"] or "").strip()
            if corrected and not verdict:
                verdict = "wrong"  # the found email was replaced
            if verdict or corrected:
                reviews[row["Key"]] = {"email": row["Email"], "verdict": verdict, "corrected": corrected}
            else:
                reviews.pop(row["Key"], None)
        save_reviews(REVIEWS_PATH, reviews)
        st.toast("Reviews saved")
        st.rerun()


def compare_view(df, current: str, names: list[str], prepare_view) -> None:
    """A6: what changed compared with an earlier run."""
    others = [n for n in names if n != current]
    if not others:
        st.info("Run at least two searches to compare them.")
        return
    previous = st.selectbox("Compare with", others, key="compare_with")
    old_df, _ = cached_results(str(RESULTS_DIR / previous), (RESULTS_DIR / previous).stat().st_mtime)
    diff = compare_runs(prepare_view(old_df), df)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("New emails", len(diff["gained"]), border=True)
    c2.metric("Emails lost", diff["lost"], border=True)
    c3.metric("Emails changed", diff["changed"], border=True)
    c4.metric("New places", diff["new_places"], border=True)
    st.subheader("Coverage by segment")
    st.dataframe(diff["by_segment"].style.map(chip_style, subset=["Segment"]), hide_index=True)
    st.subheader("New emails")
    st.dataframe(diff["gained"], hide_index=True, column_config={"Found on": st.column_config.LinkColumn("Found on")})


# ---------------------------------------------------------------- page
def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(RESULTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    names = [p.name for p in files if p.name != REVIEWS_FILE]

    st.sidebar.title("📇 B2B Email Finder")
    with st.sidebar:
        job_status()
    search_form(expanded=not names)
    if not names:
        st.title("B2B Email Finder")
        st.info("No results yet. Start a search in the sidebar.")
        return

    if st.session_state.get("result_file") not in names:
        st.session_state["result_file"] = names[0]
    choice = st.sidebar.selectbox("Result file", names, key="result_file")
    path = RESULTS_DIR / choice
    raw_df, meta = cached_results(str(path), path.stat().st_mtime)
    st.sidebar.caption(f"Area: {meta.get('area', '?')}\n\nCreated: {meta.get('generated_at', '?')}")
    entries = suppression_box()
    reviews = load_reviews(REVIEWS_PATH)

    def prepare_view(frame):
        return apply_suppression(apply_reviews(frame, reviews), entries)

    df = prepare_view(raw_df)
    if not meta.get("has_districts") and len(df):
        st.sidebar.warning("This file was created before the district lookup existed — run the search again.")

    st.title("Business contacts")

    # Filters above the views.
    c1, c2, c3, c4 = st.columns([2, 1.6, 1.2, 1.4])
    text = c1.text_input("Search", placeholder="Name, email, district …", key="search_text")
    present_segments = [s for s in SEGMENT_LABELS.values() if s in set(df["Segment"])]
    segments = c2.multiselect("Segment", present_segments, placeholder="All segments")
    cities = c3.multiselect("City", sorted(df["City"].unique()), placeholder="All cities")
    in_cities = df[df["City"].isin(cities)] if cities else df
    districts = c4.multiselect("District", sorted(in_cities["District"].unique()), placeholder="All districts")
    d1, d2, d3, d4, d5 = st.columns([1.5, 1.3, 1, 1, 1.1])
    ownership = d1.multiselect("Ownership", sorted(df["Ownership"].unique()), placeholder="Any ownership")
    confidence = d2.multiselect(
        "Email confidence", [c for c in CONFIDENCE_ORDER if c in set(df["Confidence"])], placeholder="Any"
    )
    only_email = d3.toggle("Only with email", value=False, key="only_email")
    only_phone = d4.toggle("Only with phone", value=False, key="only_phone")
    only_international = d5.toggle("International markets", value=False, key="only_international")

    view = apply_filters(
        df, Filters(text, segments, cities, districts, ownership, confidence, only_email, only_international, only_phone)
    )

    with_email = int((view["Email"] != "").sum())
    m1, m2, m3, m5, m4 = st.columns(5)
    m5.metric("With phone", int((view["Phone"] != "").sum()), border=True)
    m1.metric("Places", len(view), border=True)
    m2.metric("With email", with_email, border=True)
    m3.metric("Email coverage", f"{100 * with_email / len(view):.1f} %" if len(view) else "–", border=True)
    m4.metric("International (likely)", int((view["International"] != "").sum()), border=True)

    tabs = st.tabs(["Table", "Email coverage", "Map", "Review", "Compare runs"])
    with tabs[0]:
        table_view(view, path.stem)
    with tabs[1]:
        coverage_view(view)
    with tabs[2]:
        map_view(view)
    with tabs[3]:
        review_view(view, reviews)
    with tabs[4]:
        compare_view(df, choice, names, prepare_view)

    st.caption(ATTRIBUTION)


main()
