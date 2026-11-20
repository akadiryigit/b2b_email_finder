"""F6: read archived copies of a website from Common Crawl (https://commoncrawl.org).

Common Crawl is a free, public archive of billions of web pages. We use it only for
websites we could not open live (unreachable, or redirected elsewhere — e.g. by a
company firewall). Sites whose robots.txt blocked us are NOT looked up: Common Crawl
respects robots.txt too, and so do we.

How it works:
  1. the index API lists archived URLs of a host (with file name, offset, length)
  2. we fetch just that record with an HTTP Range request (a few KB, not the whole file)
  3. the record is gzip'd WARC: WARC headers, HTTP headers, then the HTML body
"""

from __future__ import annotations

import gzip
import json
import logging
import zlib
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .extract import CONTACT_PATH_HINTS, analyse_page

log = logging.getLogger(__name__)

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
DATA_URL = "https://data.commoncrawl.org/"
MAX_RECORDS_PER_SITE = 4


@dataclass
class ArchivedEmail:
    email: str
    url: str  # original page URL
    timestamp: str  # capture time, e.g. 20250312101500


def parse_index_lines(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if str(record.get("status")) == "200" and "html" in (record.get("mime") or record.get("mime-detected") or ""):
            records.append(record)
    return records


def pick_records(records: list[dict], limit: int = MAX_RECORDS_PER_SITE) -> list[dict]:
    """Newest capture per URL; contact pages first, then the homepage."""
    newest: dict[str, dict] = {}
    for record in records:
        url = record.get("url", "")
        if url not in newest or record.get("timestamp", "") > newest[url].get("timestamp", ""):
            newest[url] = record

    def priority(record: dict) -> tuple[int, int]:
        path = urlsplit(record["url"]).path.lower()
        if any(hint in path for hint in CONTACT_PATH_HINTS):
            return (0, len(path))
        if path in ("", "/"):
            return (1, 0)
        return (2, len(path))

    return sorted(newest.values(), key=priority)[:limit]


def parse_warc_record(raw: bytes) -> str | None:
    """gzip'd WARC response record -> HTML text (None if not usable)."""
    try:
        data = gzip.decompress(raw)
    except (OSError, EOFError):
        return None
    parts = data.split(b"\r\n\r\n", 2)  # WARC headers | HTTP headers | body
    if len(parts) < 3:
        return None
    http_headers, body = parts[1].decode("latin-1").lower(), parts[2]
    if "content-encoding: gzip" in http_headers:
        try:
            body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
        except zlib.error:
            return None
    charset = "utf-8"
    for line in http_headers.splitlines():
        if line.startswith("content-type:") and "charset=" in line:
            charset = line.split("charset=", 1)[1].strip().strip('"; ')
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class CommonCrawl:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._index_api: str | None = None

    async def _api(self) -> str:
        if self._index_api is None:
            response = await self.client.get(COLLINFO_URL, timeout=httpx.Timeout(30.0))
            response.raise_for_status()
            self._index_api = response.json()[0]["cdx-api"]  # newest collection first
        return self._index_api

    async def emails_for(self, website: str) -> list[ArchivedEmail]:
        host = urlsplit(website).hostname or ""
        if not host:
            return []
        response = await self.client.get(
            await self._api(),
            params={"url": f"{host}/*", "output": "json", "limit": 300},
            timeout=httpx.Timeout(60.0),
        )
        if response.status_code == 404:  # no captures for this host
            return []
        response.raise_for_status()
        found: dict[str, ArchivedEmail] = {}
        for record in pick_records(parse_index_lines(response.text)):
            html = await self._fetch(record)
            if not html:
                continue
            for email in analyse_page(html, record["url"]).emails:
                found.setdefault(email, ArchivedEmail(email, record["url"], record.get("timestamp", "")))
        return list(found.values())

    async def _fetch(self, record: dict) -> str | None:
        start = int(record["offset"])
        end = start + int(record["length"]) - 1
        response = await self.client.get(
            DATA_URL + record["filename"], headers={"Range": f"bytes={start}-{end}"}, timeout=httpx.Timeout(60.0)
        )
        if response.status_code not in (200, 206):
            return None
        return parse_warc_record(response.content)
