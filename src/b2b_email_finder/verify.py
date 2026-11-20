"""Step 3: judge how useful each found email address is.

What we check (all free, no email is ever sent):
  * syntax
  * can the domain receive mail at all? (DNS MX record, RFC 5321 A-record fallback,
    RFC 7505 "null MX" = domain explicitly receives no mail)
  * kind of address: role (info@, kontakt@), no-reply, low-priority (datenschutz@,
    jobs@), or other (often a named person -> personal data)
  * freemail (web.de, gmx.de, ...) and disposable providers
  * does the email domain match the business's website?

What we deliberately do NOT do: SMTP "RCPT TO" probing to test single mailboxes.
It is unreliable (catch-all servers accept everything), port 25 is blocked by most
home internet providers, and mail servers blacklist IPs that probe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver

from .extract import EMAIL_PATTERN, is_contact_page
from .utils import registrable_domain, site_key

ROLE_LOCAL_PARTS = frozenset(
    {
        "info", "kontakt", "contact", "office", "mail", "post", "service", "hallo", "hello",
        "team", "sales", "vertrieb", "bestellung", "order", "shop", "kiosk", "tankstelle",
        "station", "support", "buero", "verwaltung", "zentrale", "empfang", "anfrage",
        "anfragen", "inquiry",
    }
)
LOW_PRIORITY_LOCAL_PARTS = frozenset(
    {
        "datenschutz", "privacy", "dsb", "dpo", "abuse", "webmaster", "postmaster",
        "hostmaster", "admin", "jobs", "karriere", "bewerbung", "career", "careers",
        "presse", "press", "buchhaltung", "rechnung", "invoice", "billing",
    }
)
NO_REPLY_LOCAL_PARTS = frozenset(
    {"noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "mailer-daemon", "bounce", "bounces"}
)
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "gmx.com",
        "t-online.de", "yahoo.com", "yahoo.de", "outlook.com", "outlook.de",
        "hotmail.com", "hotmail.de", "live.de", "icloud.com", "freenet.de", "aol.com",
        "mail.de", "posteo.de", "online.de", "arcor.de", "vodafone.de",
    }
)
DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com", "10minutemail.com", "guerrillamail.com", "trashmail.com",
        "trashmail.de", "wegwerfmail.de", "yopmail.com", "temp-mail.org",
        "sharklasers.com", "dispostable.com",
    }
)

# mail_server values
MX, A_FALLBACK, NULL_MX, NONE, DNS_ERROR, NOT_CHECKED = (
    "mx", "a_fallback", "null_mx", "none", "dns_error", "not_checked",
)
USABLE_STATUSES = ("ok", "unverified")


@dataclass(frozen=True)
class EmailCheck:
    email: str
    domain: str
    mail_server: str
    kind: str  # role | other | low_priority | no_reply
    freemail: bool
    disposable: bool
    matches_website: bool | None
    # ok | unverified | invalid_syntax | disposable | no_mail_server | no_reply | chain_hq | shared_email
    status: str


def check_syntax(email: str) -> bool:
    local, _, domain = email.rpartition("@")
    return (
        bool(EMAIL_PATTERN.fullmatch(email))
        and len(email) <= 254
        and 0 < len(local) <= 64
        and ".." not in email
        and not local.startswith(".")
        and not local.endswith(".")
        and not domain.startswith("-")
    )


def address_kind(email: str) -> str:
    local = email.rpartition("@")[0].split("+", 1)[0]
    if local in NO_REPLY_LOCAL_PARTS:
        return "no_reply"
    if local in LOW_PRIORITY_LOCAL_PARTS:
        return "low_priority"
    if local in ROLE_LOCAL_PARTS:
        return "role"
    return "other"


def classify(email: str, mail_server: str = NOT_CHECKED, website: str | None = None) -> EmailCheck:
    domain = email.rpartition("@")[2]
    kind = address_kind(email)
    disposable = domain in DISPOSABLE_DOMAINS
    matches = None if not website else registrable_domain(domain) == site_key(website)

    if not check_syntax(email):
        status = "invalid_syntax"
    elif disposable:
        status = "disposable"
    elif mail_server in (NONE, NULL_MX):
        status = "no_mail_server"
    elif kind == "no_reply":
        status = "no_reply"
    elif mail_server in (MX, A_FALLBACK):
        status = "ok"
    else:
        status = "unverified"

    return EmailCheck(
        email=email,
        domain=domain,
        mail_server=mail_server,
        kind=kind,
        freemail=domain in FREEMAIL_DOMAINS,
        disposable=disposable,
        matches_website=matches,
        status=status,
    )


def rank(check: EmailCheck, source: str, source_url: str) -> int:
    """Higher = better contact address for this business."""
    score = {"ok": 100, "unverified": 40}.get(check.status, -100)
    if check.matches_website:
        score += 40
    if source == "osm":
        score += 25  # a mapper attached it to exactly this place
    elif source in ("overture", "foursquare"):
        score += 15  # a place database attached it to this place
    elif source == "website" and is_contact_page(source_url):
        score += 20  # found on the Impressum / Kontakt page
    elif source == "commoncrawl" and is_contact_page(source_url):
        score += 10  # archived Impressum: right page, but may be outdated
    score += {"role": 15, "other": 5, "low_priority": -30, "no_reply": -100}[check.kind]
    if _foreign_domain(check, source):
        score -= 30  # probably a web agency, host or other third party named on the page
    return score


def _foreign_domain(check: EmailCheck, source: str) -> bool:
    """Found on the shop's website, but neither on the shop's domain nor a freemail address."""
    return source in ("website", "commoncrawl") and check.matches_website is False and not check.freemail


def confidence(check: EmailCheck, source: str, source_url: str, weak_site: bool = False) -> str:
    """How sure are we that this email reaches *this* place? high / medium / low.

    weak_site: the website itself was only matched to the place by name + city.
    """
    if check.status not in USABLE_STATUSES:
        return "none"
    on_contact_page = source in ("website", "commoncrawl") and is_contact_page(source_url)
    if check.status != "ok" or _foreign_domain(check, source):
        return "low"
    if source == "commoncrawl" or (weak_site and source == "website"):
        # archived copy (possibly outdated) or loosely matched website: never "high"
        return "medium" if (check.matches_website or on_contact_page) else "low"
    if source == "osm" or (check.matches_website and on_contact_page):
        return "high"  # attached to this exact place, or own domain on its own Impressum
    if source in ("overture", "foursquare") and check.matches_website:
        return "high"
    if source in ("overture", "foursquare") or check.matches_website or on_contact_page:
        return "medium"
    return "low"


class MailDomainChecker:
    """Async DNS checks with a per-domain cache (each domain is looked up once)."""

    def __init__(self, lifetime: float = 5.0):
        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.lifetime = lifetime
        self._tasks: dict[str, asyncio.Future[str]] = {}

    async def check(self, domain: str) -> str:
        if domain not in self._tasks:
            self._tasks[domain] = asyncio.ensure_future(self._lookup(domain))
        return await self._tasks[domain]

    async def _lookup(self, domain: str) -> str:
        try:
            answer = await self._resolver.resolve(domain, "MX")
        except dns.resolver.NXDOMAIN:
            return NONE
        except dns.resolver.NoAnswer:
            return await self._address_fallback(domain)
        except dns.exception.DNSException:
            return DNS_ERROR
        if [record.exchange.to_text() for record in answer] == ["."]:
            return NULL_MX
        return MX

    async def _address_fallback(self, domain: str) -> str:
        # RFC 5321 §5.1: without MX, mail goes to the domain's A/AAAA address.
        for record_type in ("A", "AAAA"):
            try:
                await self._resolver.resolve(domain, record_type)
                return A_FALLBACK
            except dns.resolver.NoAnswer:
                continue
            except dns.resolver.NXDOMAIN:
                return NONE
            except dns.exception.DNSException:
                return DNS_ERROR
        return NONE
