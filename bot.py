"""BD Govt Job Telegram Alerts — API-first edition.

Uses the Alljobs public search API (no Playwright or per-job page loads).
Refuses to invent recruitment data: messages require a real title, organization,
current application deadline and an official application URL.

The public API is not a guaranteed contract. If it fails or changes, print an
unambiguous error instead of a misleading successful run. Bots must be tested
on the actual GitHub runner before relying on them.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin
from zoneinfo import ZoneInfo

import requests

LOG = logging.getLogger("bdjobs")
TZ = ZoneInfo("Asia/Dhaka")
API = "https://alljobs.teletalk.com.bd/api/v1/published-jobs/search"
STATE = Path(__file__).with_name("state.json")
MAX_MESSAGES = 10
# Search all first; other keywords are fallback when the empty search is rejected/empty.
KEYWORDS = ["", "office", "assistant", "operator", "officer", "অফিস", "সহকারী", "পুলিশ", "শিক্ষক"]
INVALID = {"", "none", "null", "undefined", "n/a", "nan", "-"}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def valid_name(value):
    s = clean(value)
    return 2 < len(s) <= 180 and s.lower() not in INVALID


def official_url(value):
    """Allow direct government application sites, but never shortened links."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if (u.scheme not in ("https", "http") or not host or
            u.username or u.password or
            not (host.endswith(".teletalk.com.bd") or host.endswith(".gov.bd"))):
            return ""
        return url
    except ValueError:
        return ""


def local_date(value):
    """Convert full timestamps/ISO/local date; don't treat missing data as today."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            seconds = value / 1000 if value > 10**11 else value
            return datetime.fromtimestamp(seconds, TZ).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    s = clean(value).replace("Sept", "Sep")
    if s.lower() in INVALID:
        return ""
    # ISO with offset, timestamp or Z
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(TZ)
        return dt.date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%Y-%m-%d %I:%M %p"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def item_value(row, *keys):
    normalized = {re.sub("[^a-z0-9]", "", str(k).lower()): v for k, v in row.items()}
    for key in keys:
        v = normalized.get(re.sub("[^a-z0-9]", "", key.lower()))
        if v is not None and v != "" and v != []:
            return v
    return ""


@dataclass(frozen=True)
class Job:
    identifier: str
    org: str
    title: str
    deadline: str
    apply: str
    start: str = ""
    circular: str = ""

    def key(self):
        return "|".join((str(self.identifier or "").lower(), self.org.casefold(), self.title.casefold(), self.deadline))


def parse_job(row, today=None):
    if not isinstance(row, dict):
        return None
    org = clean(item_value(row, "org_name_bn", "org_name", "organization_name", "organizationName", "company_name"))
    title = clean(item_value(row, "job_title_bn", "job_title", "jobTitle", "position_name", "positionName"))
    deadline = local_date(item_value(row, "deadline_date", "deadlineDate", "application_end_date", "applicationEndDate", "end_date"))
    start = local_date(item_value(row, "application_start_date", "applicationStartDate", "start_date"))
    apply = official_url(clean(item_value(row, "application_site_url", "applicationSiteUrl", "apply_url", "applyUrl")))
    circular = official_url(clean(item_value(row, "circular_url", "circularUrl", "circular_pdf", "circularPdf", "pdfUrl",
                                             "advertisement_url", "advertisementUrl", "circular_link", "circularLink",
                                             "circular_pdf_url", "circularPdfUrl", "notice_pdf_url", "noticePdfUrl")))
    identifier = clean(item_value(row, "job_primary_id", "jobPrimaryId", "job_id", "jobId", "id"))
    now = today or datetime.now(TZ).date()
    if not (valid_name(org) and valid_name(title) and deadline and apply):
        return None
    end = date.fromisoformat(deadline)
    if end < now or end > now + timedelta(days=365):
        return None
    if start and start > deadline:
        return None
    return Job(identifier, org, title, deadline, apply, start, circular)


def extract_rows(payload):
    """The documented API returns govtJobs, possibly nested in a response object."""
    if isinstance(payload, dict):
        for candidate in (payload, payload.get("data"), payload.get("result"), payload.get("results")):
            if isinstance(candidate, dict):
                for k, v in candidate.items():
                    if str(k).casefold() in ("govtjobs", "governmentjobs"):
                        if isinstance(v, list):
                            return v
                        if isinstance(v, dict):
                            for name in ("content", "items", "data", "results"):
                                if isinstance(v.get(name), list):
                                    return v[name]
    return None


def fetch(session=None, keywords=None):
    session = session or requests.Session()
    seen = {}
    attempts = []
    valid_schema = False
    rows_total = 0
    for keyword in (keywords if keywords is not None else KEYWORDS):
        try:
            response = session.get(API, params={"searchKeyword": keyword},
                                   headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; BDGovtJobNotifier/1.0)"},
                                   timeout=(5, 12))
            response.raise_for_status()
            payload = response.json()
            rows = extract_rows(payload)
            if rows is None:
                raise ValueError("Unexpected API JSON structure: govtJobs not found")
            valid_schema = True
            rows_total += len(rows)
            accepted = 0
            for row in rows:
                j = parse_job(row)
                if j:
                    seen[j.key()] = j
                    accepted += 1
            LOG.info("API keyword=%r rows=%d verified=%d", keyword, len(rows), accepted)
            # Empty search usually covers all; avoid unnecessary requests.
            # If it returns zero, use fallback keywords for limited coverage.
            if not keyword and rows:
                break
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            msg = f"keyword={keyword!r}: {type(exc).__name__}: {str(exc)[:100]}"
            attempts.append(msg)
            LOG.warning("API request failed: %s", msg)
    if not valid_schema:
        raise RuntimeError("OFFICIAL API UNAVAILABLE or schema changed; no notifications. " + "; ".join(attempts[:3]))
    if rows_total > 0 and not seen:
        raise RuntimeError(f"API returned {rows_total} jobs but none could be verified as currently open with required fields. No alerts sent.")
    return list(seen.values())



class PortalLinks(HTMLParser):
    """Collect visible anchors, including button images, without browser automation."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._active = None
        self.section = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._active = {"href": attrs.get("href", ""),
                            "onclick": attrs.get("onclick", ""),
                            "label": attrs.get("title", "") or "",
                            "section": self.section}
        elif self._active and tag == "img":
            self._active["label"] += " " + attrs.get("alt", "")

    def handle_data(self, data):
        if self._active is not None:
            self._active["label"] += " " + data
        # Teletalk landing pages often list the currently open notice above older ones.
        if "Ongoing Circular" in data:
            self.section = "ongoing"
        elif "Previous Circular" in data:
            self.section = "previous"

    def handle_endtag(self, tag):
        if tag == "a" and self._active:
            self.links.append(self._active)
            self._active = None


def link_from_anchor(page_url, anchor):
    href = clean(anchor.get("href"))
    if not href or href in ("#", "javascript:void(0)"):
        # Some portals use window.open('docs/file.pdf') onclick.
        js = clean(anchor.get("onclick"))
        match = re.search(r"(?:window\.open|location(?:\.href)?)\s*\(?\s*['\"]([^'\"]+)['\"]", js, re.I)
        href = match.group(1) if match else ""
    if not href or href.lower().startswith(("javascript:", "data:", "mailto:", "tel:")):
        return ""
    candidate = official_url(urljoin(page_url, html.unescape(href)))
    return candidate if candidate and candidate != page_url else ""


def select_advertisement_link(page_url, text, deadline=""):
    """Only return a link clearly marked Advertisement/Circular (not a job index)."""
    parser = PortalLinks()
    parser.feed(text)
    # If a Teletalk landing page advertises another campaign's expiry, do
    # not accidentally link to that outdated campaign's PDF.
    expected = date.fromisoformat(deadline) if deadline else None
    if expected:
        date_markers = (expected.strftime("%d %b %Y").lower(),
                        expected.strftime("%d-%b-%Y").lower(),
                        expected.strftime("%d/%m/%Y").lower(),
                        expected.isoformat())
        visible = clean(re.sub("<[^>]+>", " ", text)).lower()
        # Only enforce when the page explicitly mentions a deadline and a date.
        deadline_parts = re.findall(r"(?:deadline|আবেদন শেষ)[^\n<]{0,60}", visible, re.I)
        if deadline_parts and any(re.search(r"\b20\d{2}\b", t) for t in deadline_parts):
            if not any(token in " ".join(deadline_parts) for token in date_markers):
                return ""
    for anchor in parser.links:
        if anchor["section"] == "previous":
            continue
        label = clean(anchor["label"]).lower()
        link = link_from_anchor(page_url, anchor)
        if not link:
            continue
        path = urlparse(link).path.lower()
        if ("advertisement" in label or "বিজ্ঞপ্তি" in label or
            ("download" in label and "circular" in label) or
            (path.endswith(".pdf") and ("circular" in label or "advert" in path or "circular" in path))):
            return link
    return ""


def resolve_advertisement(job, session=None, cache=None):
    """Find official Advertisement link for one portal, never the Alljobs list.

    A limited number of HTML requests run only when a NEW alert is being sent;
    failures return '' and leave the application portal as an honest fallback.
    """
    if job.circular:
        return job.circular
    if not job.apply:
        return ""
    cache = cache if cache is not None else {}
    cache_key = (job.apply, job.deadline)
    if cache_key in cache:
        return cache[cache_key]
    cache[cache_key] = ""
    session = session or requests.Session()
    try:
        response = session.get(job.apply, timeout=(3, 5),
                               headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        response.raise_for_status()
        page = official_url(response.url) or job.apply
        content = response.text[:300000]
        link = select_advertisement_link(page, content, job.deadline)
        if link:
            cache[cache_key] = link
            LOG.info("Advertisement found: portal=%s", urlparse(job.apply).hostname)
            return link
        # Ongoing Circular: the portal's homepage often links to the actual
        # application campaign, where the Advertisement PDF button resides.
        parser = PortalLinks()
        parser.feed(content)
        ongoing = [link_from_anchor(page, a) for a in parser.links
                   if a["section"] == "ongoing" and "circular" in clean(a["label"]).lower()]
        for url in [u for u in ongoing if u][:1]:
            if urlparse(url).hostname != urlparse(page).hostname:
                continue
            try:
                r = session.get(url, timeout=(3, 5),
                                headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                link = select_advertisement_link(official_url(r.url) or url, r.text[:300000], job.deadline)
                if link:
                    cache[cache_key] = link
                    LOG.info("Advertisement found through ongoing circular: portal=%s", urlparse(job.apply).hostname)
                    return link
            except requests.RequestException:
                continue
    except (requests.RequestException, ValueError, UnicodeError) as e:
        LOG.warning("Advertisement lookup unavailable for %s (%s); using official application portal", urlparse(job.apply).hostname, type(e).__name__)
    LOG.info("No direct Advertisement URL confirmed for %s; official portal will be shown", urlparse(job.apply).hostname)
    return ""


def format_message(job, reminder=False):
    days = (date.fromisoformat(job.deadline) - datetime.now(TZ).date()).days
    lines = ["⏰ <b>আবেদনের শেষ সময় কাছে</b>" if reminder else "🇧🇩 <b>সরকারি চাকরির বিজ্ঞপ্তি</b>", "",
             "🏢 <b>প্রতিষ্ঠান:</b> " + html.escape(job.org),
             "💼 <b>পদ:</b> " + html.escape(job.title),
             "📅 <b>আবেদন শেষ:</b> " + job.deadline,
             "🗓 <b>আবেদন শুরু:</b> " + (job.start or "API-তে যাচাই হয়নি"),
             f"⏳ <b>বাকি:</b> {days} দিন", ""]
    if job.circular:
        lines.append('<a href="' + html.escape(job.circular, quote=True) + '">📄 মূল নিয়োগ বিজ্ঞপ্তি (Advertisement)</a>')
    else:
        lines.append("📄 সরাসরি Advertisement লিংক পাওয়া যায়নি; নিচের আবেদন পোর্টালে Advertisement বাটন চাপুন।")
    lines.extend(['<a href="' + html.escape(job.apply, quote=True) + '">🔗 অফিসিয়াল আবেদন পোর্টাল</a>',
                  "⚠️ আবেদন করার আগে মূল বিজ্ঞপ্তির শর্ত ও সময়সীমা মিলিয়ে নিন।"])
    return "\n".join(lines)


def read_state():
    if not STATE.exists():
        return {"seen": [], "warned": []}
    data = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("seen", []), list):
        raise RuntimeError("Invalid state.json; refusing to resend all messages")
    return {"seen": data.get("seen", []), "warned": data.get("warned", [])}


def save_state(seen, warned):
    STATE.write_text(json.dumps({"seen": list(sorted(seen))[-4000:], "warned": list(sorted(warned))[-4000:]},
                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_telegram(token, chat, content):
    # Token never appears in logs, even if a request exception occurs.
    try:
        r = requests.post("https://api.telegram.org/bot" + token + "/sendMessage",
                          data={"chat_id": chat, "text": content, "parse_mode": "HTML", "disable_web_page_preview": "true"},
                          timeout=(5, 15))
        resp = r.json()
        if not r.ok or not resp.get("ok"):
            raise RuntimeError("Telegram refused message (HTTP %s; %s)" % (r.status_code, clean(resp.get("description"))[:80]))
    except requests.RequestException as e:
        raise RuntimeError("Telegram network error: " + type(e).__name__) from None


def deliver(jobs, token, chat, today=None):
    today = today or datetime.now(TZ).date()
    st = read_state()
    seen, warned = set(st["seen"]), set(st["warned"])
    count = 0
    ad_cache = {}
    ad_session = requests.Session()
    for j in sorted(jobs, key=lambda x: x.deadline):
        if count >= MAX_MESSAGES:
            break
        key = j.key()
        remaining = (date.fromisoformat(j.deadline) - today).days
        warning_key = f"{key}:D{remaining}"
        new = key not in seen
        reminder = not new and remaining in (3, 1, 0) and warning_key not in warned
        if not (new or reminder):
            continue
        ad_link = resolve_advertisement(j, session=ad_session, cache=ad_cache)
        send_telegram(token, chat, format_message(replace(j, circular=ad_link), reminder))
        if new:
            seen.add(key)
        if reminder:
            warned.add(warning_key)
        save_state(seen, warned)
        count += 1
    return count


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat = os.environ.get("CHAT_ID", "").strip()
    if not token or not chat:
        LOG.error("Missing BOT_TOKEN or CHAT_ID secrets")
        return 2
    try:
        jobs = fetch()
        LOG.info("SUMMARY verified_jobs=%d (source: Alljobs search API)", len(jobs))
        if not jobs:
            LOG.warning("NO VERIFIED JOBS: API returned no currently eligible entries; no notification sent.")
            return 0
        count = deliver(jobs, token, chat)
        LOG.info("SUCCESS verified_jobs=%d telegram_sent=%d", len(jobs), count)
        return 0
    except Exception as e:
        LOG.error("FAILED: %s", str(e)[:380])
        return 2


if __name__ == "__main__":
    sys.exit(main())
