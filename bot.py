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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup

VAS = "https://vas.teletalk.com.bd/clientLivejobs.php"
ALLJOBS = "https://alljobs.teletalk.com.bd/exam-notices"
STATE = Path(__file__).with_name("state.json")
MAX_NEW = 12
MAX_DETAILS = 24  # bound run time on GitHub Actions
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




def browser_detail_urls() -> list[str]:
    """Open official Alljobs listing in Chromium, collecting rendered listing links.

    Alljobs often delivers an empty JavaScript shell to requests/BeautifulSoup.
    Scroll several times for lazy-loaded cards and try the government listing route.
    """
    collected = []
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
        page = context.new_page()
        try:
            for url in ("https://alljobs.teletalk.com.bd/jobs/government", ALLJOBS):
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    if response and response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")
                    page.wait_for_timeout(3000)
                    for _ in range(5):
                        collected.extend(detail_urls(page.content()))
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(1000)
                    LOG.info("Rendered %s: %d candidate links", url, len(set(collected)))
                except Exception as exc:
                    errors.append(str(exc)[:150])
                    LOG.warning("Rendered listing unavailable at %s: %s", url, exc)
        finally:
            browser.close()
    found = list(dict.fromkeys(collected))[:MAX_DETAILS]
    if not found:
        raise RuntimeError("Browser returned no official listing links. Details: " + "; ".join(errors))
    return found


def browser_markup(page, url: str) -> str:
    response = page.goto(url, wait_until="domcontentloaded", timeout=9000)
    if response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}")
    # The site hydrates client-side; give it a short bounded chance to populate.
    try:
        page.wait_for_function("() => { const t = document.body?.innerText || ''; return t.includes('Application End Date') && !t.includes('ADV NO: undefined'); }", timeout=4500)
    except PlaywrightTimeout:
        pass
    return page.content()

def job_detail(markup: str, detail_link: str) -> Job | None:
    """Only alert when the rendered official page proves job, organization and dates.

    Alljobs' dynamic page often places the title and organization as ordinary
    text, rather than h1/h2 headings. Its placeholders say `undefined` and
    occasionally print today's date, which must NEVER become an alert.
    """
    soup = BeautifulSoup(markup, "html.parser")
    for element in soup.select("script,style,nav,footer"):
        element.decompose()
    text = soup.get_text(" ", strip=True)
    if not text or re.search(r"(?:Circular\s+Status|Status)\s*:\s*(?:Closed|Expired)\b", text, re.I):
        return None
    # The real Alljobs details use an ISO date followed by a local time.
    sm = re.search(r"Application\s+Start\s+Date\s*:?\s*(\d{4}-\d{1,2}-\d{1,2})", text, re.I)
    em = re.search(r"Application\s+End\s+Date\s*:?\s*(\d{4}-\d{1,2}-\d{1,2})", text, re.I)
    if not (sm and em):
        return None
    start, end = parse_date(sm.group(1)), parse_date(em.group(1))
    if not (start and end) or end < date.today().isoformat() or end < start:
        return None
    # Exclude the well-known unresolved client-side placeholders.
    if re.search(r"ADV\s*NO\s*:\s*undefined", text, re.I):
        return None
    apps = [u for _, u in links_in(soup, detail_link) if trusted_apply(u)]
    if not apps:
        apps = [u for u in re.findall(r"https?://[a-z0-9.-]+\.teletalk\.com\.bd(?:/[a-zA-Z0-9_./?=&%-]*)?", markup, re.I) if trusted_apply(u)]
    if not apps:
        return None
    # The actual detail layout: [position] [organization] Status: Live ADV NO:
    # Extract the bounded section immediately before Status instead of requiring headings.
    prefix = re.split(r"\bStatus\s*:\s*Live\b", text, maxsplit=1, flags=re.I)[0]
    prefix = re.sub(r"^.*?Job\s*ID\s*:\s*", "", prefix, flags=re.I | re.S)
    prefix = re.sub(r"^\s*(?:undefined\s*\(vacancies\)\s*)?", "", prefix, flags=re.I)
    headings = [h.get_text(" ", strip=True) for h in soup.select("h1,h2,h3,h4,h5,h6")]
    headings = [h for h in headings if h and h.lower() not in {"organization information", "read before apply"}
                and not re.search(r"job[s]?[- ]details|government jobs|application|teletalk|undefined|vacancies", h, re.I)]
    org = ""
    position = ""
    # The title is commonly an h3; the org appears immediately afterwards.
    for h in headings:
        if h in prefix and len(h) >= 4 and len(h) <= 110:
            tail = prefix.split(h, 1)[1].strip()
            tail = re.sub(r"^\s*(?:Job ID\s*:\s*)?", "", tail, flags=re.I)
            possible_org = tail.strip(" :-–—")
            if 5 <= len(possible_org) <= 150 and not re.search(r"undefined|null|apply now|wish list", possible_org, re.I):
                position, org = h, possible_org
                break
    if not (org and position):
        # Fallback to explicit labeled fields only, not guessed headings.
        om = re.search(r"(?:Organization|Organisation|Employer|প্রতিষ্ঠান)\s*:\s*(.{5,120}?)(?=\s+(?:Status|ADV NO|Position|Post)\s*:|$)", text, re.I)
        pm = re.search(r"(?:Position|Post Name|পদের নাম)\s*:\s*(.{4,110}?)(?=\s+(?:Status|ADV NO|Organization|Application)\s*:|$)", text, re.I)
        if om and pm:
            org, position = om.group(1).strip(), pm.group(1).strip()
    if not (org and position):
        LOG.warning("Skipped unverified organization/title: %s", detail_link)
        return None
    if re.search(r"(?:undefined|\bnull\b|exam result|written result|viva schedule|পরীক্ষার ফলাফল)", org + ' ' + position, re.I):
        return None
    return Job(org=org, apply=apps[0], start=start, end=end, positions=[position], source=detail_link)


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
        # Browser renders JavaScript list; raw HTTP is only a fallback.
        urls = browser_detail_urls()
        LOG.info("Alljobs browser: %d detail URLs", len(urls))
        verified = 0
        failed = 0
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.set_default_timeout(6500)
            try:
                for index, url in enumerate(urls, 1):
                    try:
                        j = job_detail(browser_markup(page, url), url)
                        if j:
                            gathered.append(j)
                            verified += 1
                        else:
                            failed += 1
                    except Exception as exc:
                        failed += 1
                        LOG.warning("Detail not readable: %s: %s", url, str(exc)[:160])
                    LOG.info("Progress %s/%s: verified=%s skipped=%s", index, len(urls), verified, failed)
            finally:
                browser.close()
        LOG.info("Alljobs verified live recruitment groups: %d", verified)
        if not verified:
            errors.append("Alljobs: no verifiable live notices")
    except Exception as exc:
        errors.append(f"Alljobs: {type(exc).__name__}")
        LOG.warning("Alljobs browser discovery failed: %s", exc)

    if not gathered:
        raise RuntimeError("NO VERIFIED LIVE JOBS: Teletalk/Alljobs yielded no usable entries. "
                           "Check URLs, HTML structure or website availability. "
                           "No notifications or state updates made. Causes: " + "; ".join(errors))
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
