from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from . import suppression
from .categories import CATEGORIES
from .export import ATTRIBUTION, save_search
from .geocode import AreaNotFound
from .overpass import OverpassError
from .pipeline import DEFAULT_USER_AGENT, PLACE_SOURCES, SearchOptions, run_search


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="b2b-email-finder",
        description="Find public contact data of German businesses from OpenStreetMap "
        "and the businesses' own websites.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("categories", help="list supported business categories")

    ui = sub.add_parser(
        "ui", help="open the web UI (needs the [ui] extra); extra flags go to streamlit, e.g. --server.headless true"
    )
    ui.add_argument("--results-dir", type=Path, default=Path("results"), help="where result files are read/written")
    ui.add_argument("--port", type=int, default=8501)

    search = sub.add_parser("search", help="find businesses in an area and their contact data")
    search.add_argument("--area", required=True, help='city, district or state, e.g. "Köln", "Bayern", or "Deutschland"')
    search.add_argument("--category", nargs="+", choices=list(CATEGORIES), default=list(CATEGORIES))
    search.add_argument("--limit", type=int, help="max businesses to fetch from OSM (good for testing)")
    search.add_argument("--require-website", action="store_true", help="only businesses with a website in OSM")
    search.add_argument("--only-with-email", action="store_true", help="only output businesses where an email was found")
    search.add_argument(
        "--sources", nargs="*", choices=list(PLACE_SOURCES), default=list(PLACE_SOURCES),
        help="extra place databases merged with OSM (default: overture foursquare; '--sources' alone = none)",
    )
    search.add_argument("--no-crawl", action="store_true", help="do not visit websites (no discovery, no archive)")
    search.add_argument("--no-discover", action="store_true", help="do not guess/search websites for places without one")
    search.add_argument("--max-search-queries", type=int, default=2000, help="web search API budget (needs BRAVE_API_KEY)")
    search.add_argument(
        "--commoncrawl", action="store_true",
        help="also try the Common Crawl archive for unreachable sites (slow, often overloaded)",
    )
    search.add_argument("--no-districts", action="store_true", help="skip the city/district lookup")
    search.add_argument("--no-verify", action="store_true", help="skip DNS mail-server checks")
    search.add_argument("--max-pages", type=int, default=5, help="max pages per website (default 5)")
    search.add_argument("--delay", type=float, default=1.0, help="seconds between requests to the same site")
    search.add_argument("--concurrency", type=int, default=5, help="websites crawled in parallel")
    search.add_argument(
        "--user-agent",
        default=os.environ.get("B2B_EMAIL_FINDER_UA", DEFAULT_USER_AGENT),
        help="HTTP User-Agent; please add a contact URL or email (env: B2B_EMAIL_FINDER_UA)",
    )
    search.add_argument(
        "--suppress", type=Path, default=Path("results") / suppression.DEFAULT_FILE,
        help="file with emails/domains (one per line) never to output (default: results/suppress.txt)",
    )
    search.add_argument("--no-cache", action="store_true", help="do not cache Overpass results in .cache/")
    search.add_argument("--out", type=Path, default=Path("results/results"), help="output path without extension")
    search.add_argument("-v", "--verbose", action="store_true")
    return parser


def _seed_demo_results(results_dir: Path) -> None:
    """First start: copy the bundled example files (B2B_DEMO_DIR) into an empty results folder."""
    demo_dir = os.environ.get("B2B_DEMO_DIR")
    if not demo_dir or any(results_dir.glob("*.json")):
        return
    results_dir.mkdir(parents=True, exist_ok=True)
    for example in Path(demo_dir).glob("*.json"):
        shutil.copy(example, results_dir / example.name)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args, extra = parser.parse_known_args(argv)
    if extra and args.command != "ui":
        parser.error(f"unrecognized arguments: {' '.join(extra)}")

    if args.command == "categories":
        for category in CATEGORIES.values():
            filters = ", ".join(f"{k}={v}" for k, v in category.osm_filters)
            print(f"{category.key:<8} {category.label}  [{filters}]\n         {category.notes}")
        return 0

    if args.command == "ui":
        # Streamlit runs a script file, so we start it as a subprocess.
        _seed_demo_results(args.results_dir)
        app = Path(__file__).with_name("app.py")
        env = {**os.environ, "B2B_RESULTS_DIR": str(args.results_dir.resolve())}
        command = [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port)]
        command += [a for a in extra if a != "--"]  # e.g. --server.headless true
        return subprocess.call(command, env=env)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    opts = SearchOptions(
        area=args.area,
        categories=args.category,
        limit=args.limit,
        require_website=args.require_website,
        only_with_email=args.only_with_email,
        admin_areas=not args.no_districts,
        sources=args.sources,
        crawl=not args.no_crawl,
        discover=not args.no_discover,
        max_search_queries=args.max_search_queries,
        commoncrawl=args.commoncrawl,
        verify=not args.no_verify,
        max_pages=args.max_pages,
        delay=args.delay,
        concurrency=args.concurrency,
        user_agent=args.user_agent,
        cache_dir=None if args.no_cache else Path(".cache"),
        suppress=suppression.load(args.suppress),
    )
    try:
        result = asyncio.run(run_search(opts))
    except (AreaNotFound, OverpassError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json_path, csv_path = save_search(result, opts.categories, args.out)

    print("\nSummary")
    for key, value in result.stats.items():
        print(f"  {key:<26} {value}")
    print(f"\nSaved {len(result.rows)} rows to {json_path} and {csv_path}")
    print(ATTRIBUTION)
    return 0
