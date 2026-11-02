"""Bangladesh government vacancy alerts from official Teletalk sources.

Runs in GitHub Actions with BOT_TOKEN / CHAT_ID in repository secrets.
Only links/dates extracted from official pages are sent. No fabricated PDFs.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

VAS = "https://vas.teletalk.com.bd/clientLivejobs.php"
ALLJOBS = "https://alljobs.teletalk.com.bd/exam-notices"
STATE = Path(__file__).with_name("state.json")
MAX_NEW = 12
MAX_DETAILS = 35
TIMEOUT = 18
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GovtJobAlert/1.0; public info monitoring)"}
DATE_RE = re.compile(r"\b\d{1,2}[-/]\w{3,9}[-/]\d{4}\b|\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b")
DATE_ISO_RE = re.compile(r"\b(\d{4}-\d{1,2}-\d{1,2})\b")
LOG = logging.getLogger("bd_jobs")


@dataclass
class Job:
    org: str
    apply: str
    start: str
    end: str
    positions: list[str]
    source: str
    circular: str = ""

    @property
    def uid(self) -> str:
        # Same Teletalk application portal + identical application period => one circular
        return hashlib.sha256(f"{urlparse(self.apply).hostname}/{self.start}/{self.end}".encode()).hexdigest()[:20]


def trusted_apply(url: str) -> bool:
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        return parts.scheme in ("http", "https") and host.endswith(".teletalk.com.bd") and host not in {
            "alljobs.teletalk.com.bd", "vas.teletalk.com.bd"
        }
    except ValueError:
        return False


def trusted_circular(url: str) -> bool:
    try:
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        return parts.scheme in ("http", "https") and (host.endswith(".teletalk.com.bd") or host.endswith(".gov.bd"))
    except ValueError:
        return False


def parse_date(s: str) -> str:
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def request_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def links_in(node, base: str):
    pairs = []
    for a in node.select('a[href]'):
        u = urljoin(base, a.get('href', '').strip())
        pairs.append((a.get_text(" ", strip=True), u))
    return pairs


def live_vas(markup: str) -> list[Job]:
    soup = BeautifulSoup(markup, "html.parser")
    result = []
    for tr in soup.select("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue
        cells = [c.get_text(" ", strip=True) for c in tds]
        if not any(re.search(r"\bLive\b", c, re.I) for c in cells):
            continue
        dates = [parse_date(x) for x in DATE_RE.findall(" ".join(cells))]
        dates = [d for d in dates if d]
        if len(dates) < 2:
            continue
        start, end = dates[:2]
        if end < date.today().isoformat():
            continue
        # VAS columns: logo, organization, short name, start date, end date, status, weblink
        # Prefer the second cell, but handle tables that omit the logo cell.
        org = cells[1] if len(cells) >= 7 else cells[0]
        org = re.sub(r"\s+", " ", org).strip()
        if not org:
            continue
        apply_links = [u for label, u in links_in(tr, VAS) if trusted_apply(u)]
        # Some official Teletalk portals still use http:// URLs.
        if not apply_links:
            candidates = re.findall(r"https?://[a-z0-9.-]+\.teletalk\.com\.bd(?:/[A-Za-z0-9_./?=&%-]*)?", str(tr), re.I)
            apply_links = candidates
            apply_links = [u for u in apply_links if trusted_apply(u)]
        if not apply_links:
            continue
        result.append(Job(org=org, apply=apply_links[-1], start=start, end=end, positions=[], source=VAS))
    return result


def detail_urls(markup: str) -> list[str]:
    """Extract official detail URLs even when pages omit jobId parameters."""
    soup = BeautifulSoup(markup, "html.parser")
    found = []
    for _, url in links_in(soup, ALLJOBS):
        if re.fullmatch(r"https://alljobs\.teletalk\.com\.bd/(?:exam-notices/\d+|jobs/government/\d+)(?:\?[^#]*)?", url):
            found.append(url)
    # Many official pages embed links in JS, JSON or data attributes.
    for raw in re.findall(r"(?:https?://alljobs\.teletalk\.com\.bd)?/(?:exam-notices/\d+|jobs/government/\d+)(?:\?[^\"'<>\s]*)?", markup):
        found.append(urljoin(ALLJOBS, html.unescape(raw)))
    return list(dict.fromkeys(found))[:MAX_DETAILS]


def job_detail(markup: str, detail_link: str) -> Job | None:
    """Recognize official Alljobs notice detail pages, not exam results."""
    soup = BeautifulSoup(markup, "html.parser")
    text = soup.get_text(" ", strip=True)
    if re.search(r"(?:Circular\s+Status|Status)\s*:\s*(?:Closed|Expired)\b", text, re.I):
        return None
    # Avoid false notices from pages with incomplete placeholders.
    start_match = re.search(r"Application\s+Start\s+Date\s*:?\s*(\d{4}-\d{1,2}-\d{1,2})", text, re.I)
    end_match = re.search(r"Application\s+End\s+Date\s*:?\s*(\d{4}-\d{1,2}-\d{1,2})", text, re.I)
    if not (start_match and end_match):
        return None
    start, end = parse_date(start_match.group(1)), parse_date(end_match.group(1))
    if not (start and end) or end < date.today().isoformat() or end < start:
        return None
    # The official exam-notices details list a position, org (SHORTNAME), a
    # 'Download Notice' link and the application's official Teletalk domain.
    apps = [url for _, url in links_in(soup, detail_link) if trusted_apply(url)]
    if not apps:
        apps = [u for u in re.findall(r"https?://[a-z0-9.-]+\.teletalk\.com\.bd(?:/[a-zA-Z0-9_./?=&%-]*)?", markup, re.I) if trusted_apply(u)]
    if not apps:
        return None
    position = ""
    org = ""
    headings = [h.get_text(" ", strip=True) for h in soup.select("h1,h2,h3,h4")]
    headings = [h for h in headings if h and not re.search(r"notice details|teletalk jobs|read before|organization information|view circular", h, re.I)]
    if headings:
        position = headings[0]
    # Official exam-notices places the organization directly after the position.
    if position:
        pattern = re.escape(position) + r"\s+(.{4,150}?)\s+ADV NO\s*:"
        m = re.search(pattern, text, re.I)
        if m:
            org = m.group(1).strip()
    if not org:
        m = re.search(r"([A-Za-z][A-Za-z0-9 ,.&/\-]{4,100}\([A-Z0-9_-]{2,20}\))\s+ADV NO", text, re.I)
        if m:
            org = m.group(1).strip()
    if not org or org.lower() in {"undefined", "null"} or not position:
        LOG.warning("Skipped unverified organization/title: %s", detail_link)
        return None
    if re.search(r"exam result|written result|viva schedule|পরীক্ষার ফলাফল|পরীক্ষার সময়সূচি", position, re.I):
        return None
    job = Job(org=org, apply=apps[0], start=start, end=end,
              positions=[position], source=detail_link)
    return job


def merge_jobs(items: list[Job]) -> list[Job]:
    records: dict[str, Job] = {}
    for j in items:
        k = j.uid
        if k not in records:
            records[k] = j
        else:
            present = records[k]
            if present.org.startswith("সরকারি প্রতিষ্ঠান (") and not j.org.startswith("সরকারি প্রতিষ্ঠান ("):
                present.org = j.org
            for title in j.positions:
                if title not in present.positions:
                    present.positions.append(title)
            if j.source != VAS:
                present.source = j.source
    return sorted(records.values(), key=lambda x: (x.start, x.org), reverse=True)


def official_circular(apply_url: str) -> str:
    """Find an *explicitly linked* circular or PDF. Return empty if unverified.

    Note: a `.gov.bd` PDF directly linked from a Teletalk page is accepted.
    """
    try:
        markup = request_text(apply_url)
    except requests.RequestException as exc:
        LOG.warning("Could not inspect circular page %s: %s", apply_url, exc)
        return ""
    links = links_in(BeautifulSoup(markup, "html.parser"), apply_url)
    selected = []
    for label, url in links:
        if not trusted_circular(url):
            continue
        if (re.search(r"circular|advertisement|নিয়োগ\s*বিজ্ঞপ্তি|নিয়োগ\s*বিজ্ঞপ্তি", label, re.I)
                or re.search(r"(?:circular|advertisement|recruitment)[^/]*\.pdf(?:\?|$)", url, re.I)):
            selected.append(url)
    selected.sort(key=lambda u: not urlparse(u).path.lower().endswith(".pdf"))
    return selected[0] if selected else ""


def escape(s: str) -> str:
    return html.escape(s, quote=False)


def message(j: Job, warning: int | None = None) -> str:
    days = (date.fromisoformat(j.end) - date.today()).days
    heading = "⏰ <b>আবেদনের শেষ সময় কাছে!</b>" if warning is not None else "🇧🇩 <b>নতুন সরকারি চাকরির বিজ্ঞপ্তি</b>"
    title = ", ".join(j.positions[:12]) if j.positions else "পদের তথ্য মূল বিজ্ঞপ্তিতে দেখুন"
    if len(j.positions) > 12:
        title += f" (আরও {len(j.positions)-12}টি পদ)"
    circular = f'<a href="{html.escape(j.circular, quote=True)}">মূল বিজ্ঞপ্তি / সার্কুলার</a>' if j.circular else "মূল PDF লিংক যাচাই করা যায়নি"
    return (f"{heading}\n\n"
            f"🏢 <b>প্রতিষ্ঠান:</b> {escape(j.org)}\n"
            f"💼 <b>পদ:</b> {escape(title)}\n"
            f"📅 <b>আবেদন শুরু:</b> {escape(j.start)}\n"
            f"📌 <b>আবেদনের শেষ তারিখ:</b> {escape(j.end)}\n"
            f"⌛ <b>বাকি:</b> {days} দিন\n\n"
            f"📄 {circular}\n"
            f'🔗 <a href="{html.escape(j.apply, quote=True)}">সরকারি আবেদন লিংক</a>\n'
            f'🔎 <a href="{html.escape(j.source, quote=True)}">তথ্যের উৎস</a>\n\n'
            "⚠️ আবেদন করার আগে মূল বিজ্ঞপ্তির যোগ্যতা ও শর্ত মিলিয়ে নিন।")


def telegram_send(token: str, chat_id: str, text: str):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"}, timeout=25)
    r.raise_for_status()
    response = r.json()
    if not response.get("ok"):
        raise RuntimeError("Telegram API rejected the message")


def load_state() -> dict:
    if STATE.exists():
        try:
            data = json.loads(STATE.read_text(encoding="utf-8"))
            return {"seen": list(data.get("seen", [])), "warned": list(data.get("warned", []))}
        except (ValueError, TypeError):
            raise RuntimeError("state.json could not be decoded; stopping to prevent duplicate alerts")
    return {"seen": [], "warned": []}


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    if not (token and chat_id):
        raise RuntimeError("Set BOT_TOKEN and CHAT_ID in GitHub repository Actions secrets")

    gathered = []
    errors = []
    try:
        found = live_vas(request_text(VAS))
        LOG.info("VAS: %d entries", len(found))
        gathered += found
    except Exception as exc:
        errors.append(f"Teletalk VAS: {type(exc).__name__}")
        LOG.warning("VAS source unavailable: %s", exc)

    try:
        urls = detail_urls(request_text(ALLJOBS))
        LOG.info("Alljobs: %d detail URLs", len(urls))
        if not urls:
            LOG.warning("No listing links found; Alljobs markup may be client-rendered or changed")
        for url in urls:
            try:
                j = job_detail(request_text(url), url)
                if j:
                    gathered.append(j)
            except requests.RequestException as exc:
                LOG.warning("Skipped detail %s: %s", url, exc)
    except Exception as exc:
        errors.append(f"Alljobs: {type(exc).__name__}")
        LOG.warning("Alljobs unavailable: %s", exc)

    if len(errors) == 2:
        raise RuntimeError("Both sources failed; no alerts sent and no state changed")
    jobs = merge_jobs(gathered)
    state = load_state()
    seen, warned = set(state["seen"]), set(state["warned"])
    sent_count = 0
    for job in jobs:
        if sent_count >= MAX_NEW:
            break
        if job.uid not in seen:
            job.circular = official_circular(job.apply)
            telegram_send(token, chat_id, message(job))
            seen.add(job.uid)
            sent_count += 1
        else:
            days = (date.fromisoformat(job.end) - date.today()).days
            if days in (3, 1, 0) and f"{job.uid}:{days}" not in warned:
                job.circular = official_circular(job.apply)
                telegram_send(token, chat_id, message(job, days))
                warned.add(f"{job.uid}:{days}")
                sent_count += 1

    state["seen"] = sorted(seen)[-1500:]
    state["warned"] = sorted(warned)[-1500:]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOG.info("Completed: %d live groups, sent %d messages, errors: %s", len(jobs), sent_count, errors)


if __name__ == "__main__":
    main()
