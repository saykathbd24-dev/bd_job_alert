"""BD Govt Job Telegram Alert v4.

Read official *listing* cards and public JSON responses only. Never navigate to
an individual Alljobs detail page (those time out on some GitHub runners).
A job is eligible only when its organization, position, and application deadline
were actually present in an official source. Missing links are labelled missing.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LOG = logging.getLogger("jobs")
BASE = "https://alljobs.teletalk.com.bd"
PAGES = [BASE + "/jobs/government", BASE + "/exam-notices"]
VAS = "https://vas.teletalk.com.bd/clientLivejobs.php"
STATE = Path(__file__).with_name("state.json")
MAX_SEND = 8
MAX_CARDS = 180
TODAY = lambda: datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Dhaka")).date()
BAD = re.compile(r"(?:^\s*$|^undefined$|^null$|^n/?a$|^none$|^view circular$|^apply online$|^download notice$)", re.I)
DATEVAL = r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{1,2}[ -](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ ,/-]+\d{4})"
DEADLINE_RE = re.compile(r"(?:application\s*(?:end|closing|last)\s*date|apply\s*by|deadline|last\s*date|closing\s*date|আবেদনের\s*শেষ\s*তারিখ|শেষ\s*তারিখ)\s*[:\-]?\s*"+DATEVAL, re.I)
START_RE = re.compile(r"(?:application\s*start\s*date|starting\s*date|আবেদন\s*শুরু)\s*[:\-]?\s*"+DATEVAL, re.I)
BAD_TITLE = re.compile(r"^(?:government jobs?|notice details|job[s -]?details|exam notices?|alljobs|job information|position|read before apply)$", re.I)


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" :-|\t\r\n")


def legit_text(value: str, maxlen=180) -> bool:
    s = clean(value)
    return 3 <= len(s) <= maxlen and not BAD.fullmatch(s) and not BAD_TITLE.fullmatch(s) and not re.search(r"\b(?:undefined|null)\b", s, re.I)


def official(url: str) -> bool:
    try:
        p = urlsplit(url)
        h = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and (h.endswith(".teletalk.com.bd") or h.endswith(".gov.bd"))
    except ValueError:
        return False


def apply_link(url: str) -> bool:
    try:
        p = urlsplit(url)
        h = (p.hostname or "").lower()
        return official(url) and h not in ("alljobs.teletalk.com.bd", "vas.teletalk.com.bd")
    except ValueError:
        return False


def canonical(url: str) -> str:
    s = urlsplit(url)
    return urlunsplit((s.scheme.lower(), s.netloc.lower(), s.path.rstrip("/"), s.query, ""))


def iso_date(value) -> str:
    if not value:
        return ""
    s = clean(value).replace("Sept", "Sep")
    if re.fullmatch(r"\d{13}", s):  # unix milliseconds (rare)
        try:
            return datetime.fromtimestamp(int(s) / 1000).date().isoformat()
        except (OverflowError, ValueError):
            return ""
    m = re.search(DATEVAL, s, re.I)
    if not m:
        return ""
    s = m.group(1)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


@dataclass
class Job:
    organization: str
    position: str
    deadline: str
    source: str
    start: str = ""
    apply: str = ""
    circular: str = ""
    positions: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.organization = clean(self.organization)
        self.position = clean(self.position)
        self.deadline = iso_date(self.deadline)
        self.start = iso_date(self.start)
        self.apply = canonical(self.apply) if self.apply and apply_link(self.apply) else ""
        self.circular = canonical(self.circular) if self.circular and official(self.circular) else ""
        self.source = canonical(self.source) if official(self.source) else ""
        self.positions = list(dict.fromkeys([self.position] + self.positions))

    def valid(self) -> bool:
        if not (legit_text(self.organization) and legit_text(self.position, 150) and self.source and self.deadline):
            return False
        end = date.fromisoformat(self.deadline)
        if end < TODAY() or end > TODAY() + timedelta(days=400):
            return False
        if self.start and (self.start > self.deadline or date.fromisoformat(self.start) > TODAY() + timedelta(days=180)):
            return False
        return True

    def key(self) -> str:
        # Notices sometimes list many positions with separate detail URLs; group
        # the same institution and deadline into a single alert instead of a flood.
        return "|".join((self.organization.casefold(), self.deadline))


def from_card(card: dict, listing: str) -> Job | None:
    """Parse visible public listing text; do not guess a date from unrelated text."""
    text = str(card.get("text") or "")
    em, sm = DEADLINE_RE.search(text), START_RE.search(text)
    if not em:
        return None
    deadline = iso_date(em.group(1))
    start = iso_date(sm.group(1)) if sm else ""
    # Prefer actual headings/labels; only fall back to the first two simple card lines.
    lines = [clean(x) for x in text.splitlines() if clean(x)]
    lines = [x for x in lines if not BAD_TITLE.fullmatch(x) and not BAD.fullmatch(x)]
    headings = [clean(x) for x in card.get("headings", []) if legit_text(x, 150)]
    om = re.search(r"(?:organization|organisation|institution|employer|company|প্রতিষ্ঠান|সংস্থা)\s*[:\-]\s*([^\n]{3,150})", text, re.I)
    pm = re.search(r"(?:position|post name|job title|পদের নাম|পদ)\s*[:\-]\s*([^\n]{3,150})", text, re.I)
    # Important: a listing's heading may be the organization rather than job title.
    position = clean(pm.group(1) if pm else (headings[0] if headings else (lines[0] if lines else "")))
    org = clean(om.group(1) if om else "")
    if not org:
        for line in lines:
            if line != position and line not in headings[:1] and len(line) <= 170 and not re.search(
                r"application|deadline|date|vacanc|job id|adv no|circular|status|apply now|apply online|\d{4}-\d{2}-\d{2}", line, re.I
            ):
                org = line
                break
    links = card.get("links") or []
    app, circular = "", ""
    for entry in links:
        if not isinstance(entry, dict):
            continue
        href = urljoin(listing, str(entry.get("url") or ""))
        label = clean(entry.get("label")).lower()
        if apply_link(href) and not app:
            app = href
        if official(href) and (href.lower().split("?")[0].endswith(".pdf") or re.search(r"circular|notice|বিজ্ঞপ্তি", label)) and href != listing:
            if not circular:
                circular = href
    detail = str(card.get("detail") or "")
    source = urljoin(listing, detail) if detail else listing
    j = Job(org, position, deadline, source, start, app, circular)
    return j if j.valid() else None


def get_field(data: dict, *aliases):
    lookup = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in data.items()}
    for name in aliases:
        v = lookup.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if v not in (None, "", [], {}):
            if isinstance(v, dict):
                v = get_field(v, "name", "title", "value", "url")
            if isinstance(v, list) and v:
                v = v[0]
            return v
    return ""


def from_json(data: dict, source: str) -> Job | None:
    """Only accept keys that explicitly describe job + deadline (no createdAt)."""
    org = get_field(data, "organization_name", "organizationName", "orgName", "companyName", "employerName", "officeName", "organization", "organisation", "company", "employer")
    pos = get_field(data, "positionName", "postName", "designation", "jobTitle", "jobName", "position", "post", "title")
    end = get_field(data, "applicationEndDate", "application_deadline", "endDate", "closingDate", "deadline", "lastDate", "applyEndDate", "applicationEnd")
    start = get_field(data, "applicationStartDate", "applyStartDate", "startDate", "applicationStart")
    app = get_field(data, "applicationUrl", "applicationLink", "applyUrl", "applyLink", "onlineApplyLink", "webLink", "apply_url")
    circular = get_field(data, "circularPdf", "circularUrl", "noticePdf", "noticeUrl", "pdfUrl", "circularLink")
    detail = get_field(data, "detailUrl", "detailLink", "noticeDetailUrl", "jobUrl", "url")
    # Never manufacture URL from an ID; only use a URL explicitly found in the feed.
    src = urljoin(source, str(detail)) if detail else source
    if not official(src):
        src = source
    j = Job(str(org), str(pos), str(end), src, str(start), urljoin(source, str(app)) if app else "", urljoin(source, str(circular)) if circular else "")
    return j if j.valid() else None


def json_objects(root, max_nodes=2500):
    stack = [root]
    count = 0
    while stack and count < max_nodes:
        value = stack.pop()
        count += 1
        if isinstance(value, dict):
            yield value
            stack.extend(v for v in value.values() if isinstance(v, (dict, list)))
        elif isinstance(value, list):
            stack.extend(v for v in value[:250] if isinstance(v, (dict, list)))


# Runs inside Playwright page.evaluate; read-only DOM inspection, no clicks.
CARD_JS = r"""() => {
  const re = /\/(?:exam-notices\/\d+|jobs\/government\/\d+)(?:[?#]|$)/;
  const roots = [...document.querySelectorAll('a[href]')].filter(a=>re.test(a.href)).slice(0, 160);
  return roots.map(a => {
    let selected = a.parentElement || a;
    let best = selected;
    for (let n=0; n<7 && selected; n++, selected=selected.parentElement) {
      const t=(selected.innerText||'').trim();
      if(t.length>30 && t.length<2000) {best=selected;
        if (/deadline|end date|last date|শেষ তারিখ/i.test(t) && /\d{4}[-/]\d{1,2}[-/]\d{1,2}/.test(t)) break;
      }
    }
    const node=best;
    return {
      detail:a.href,
      text:(node.innerText||'').slice(0,1800),
      headings:[...node.querySelectorAll('h1,h2,h3,h4,h5,h6')].slice(0,5).map(x=>x.innerText),
      links:[...node.querySelectorAll('a[href]')].slice(0,16).map(x=>({label:x.innerText,url:x.href}))
    };
  });
}"""


def fetch_alljobs() -> tuple[list[Job], dict]:
    results = []
    diagnostics = {"cards": 0, "json_objects": 0, "valid_cards": 0, "valid_json": 0, "pages": 0, "issues": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = browser.new_context(locale="en-US", timezone_id="Asia/Dhaka")
            page = context.new_page()
            page.set_default_timeout(7000)
            payloads: list[dict | list] = []

            def receive(response):
                if len(payloads) >= 35 or response.request.resource_type not in ("fetch", "xhr"):
                    return
                if not official(response.url) or not (200 <= response.status < 300):
                    return
                if "json" not in (response.headers.get("content-type") or "").lower():
                    return
                try:
                    if int(response.headers.get("content-length") or "0") > 2_000_000:
                        return
                    payloads.append(response.json())
                except Exception:
                    return

            page.on("response", receive)
            for url in PAGES:
                payloads.clear()
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=18000)
                    if response and response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}")
                    page.wait_for_timeout(1700)
                    cards: list[dict] = []
                    for _ in range(4):
                        cards.extend(page.evaluate(CARD_JS))
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(650)
                    # There may be JSON in initial document state even without XHR.
                    try:
                        embedded = page.locator('script[type="application/json"]').all_text_contents()
                        for block in embedded[:4]:
                            if len(block) <= 1_000_000:
                                payloads.append(json.loads(block))
                    except Exception:
                        pass
                    unique = {str(c.get("detail", "")): c for c in cards if c.get("detail")}
                    diagnostics["cards"] += len(unique)
                    count = 0
                    for item in list(unique.values())[:MAX_CARDS]:
                        j = from_card(item, url)
                        if j:
                            results.append(j)
                            count += 1
                    diagnostics["valid_cards"] += count
                    found_json = 0
                    for payload in payloads:
                        for obj in json_objects(payload):
                            diagnostics["json_objects"] += 1
                            j = from_json(obj, url)
                            if j:
                                results.append(j)
                                found_json += 1
                    diagnostics["valid_json"] += found_json
                    diagnostics["pages"] += 1
                    LOG.info("LIST %s: links=%d valid_cards=%d valid_json=%d", url, len(unique), count, found_json)
                    if not count and not found_json:
                        # Diagnostic sample lets us fix exact markup without copying private data.
                        sample = next(iter(unique.values()), {})
                        LOG.warning("NO VERIFIED LIST DATA at %s; first card text=%r", url,
                                    clean(sample.get("text", ""))[:220])
                except Exception as exc:
                    diagnostic = f"{type(exc).__name__}: {str(exc)[:120]}"
                    diagnostics["issues"].append(diagnostic)
                    LOG.warning("LIST source unavailable %s: %s", url, diagnostic)
        finally:
            browser.close()
    return results, diagnostics


def fetch_vas() -> list[Job]:
    """Optional public source; TLS failure is reported, never bypassed."""
    r = requests.get(VAS, timeout=(5, 7), headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    found = []
    for tr in soup.select("tr"):
        cells = [clean(td.get_text(" ", strip=True)) for td in tr.select("td")]
        if len(cells) < 5 or not any("live" == x.lower() for x in cells):
            continue
        dates = [iso_date(d) for cell in cells for d in re.findall(DATEVAL, cell, re.I)]
        dates = [d for d in dates if d]
        if len(dates) < 2:
            continue
        urls = [urljoin(VAS, x.get("href")) for x in tr.select("a[href]")]
        apps = [url for url in urls if apply_link(url)]
        if not apps:
            continue
        org = cells[1] if len(cells) >= 7 else cells[0]
        # The VAS feed does not reliably list individual positions; do not invent one.
        # This helps with source diagnostics but does not generate unverified alerts.
        LOG.info("VAS live organization seen: %s, end=%s", org[:90], dates[1])
    return found


def merge(jobs: list[Job]) -> list[Job]:
    grouped: dict[str, Job] = {}
    for j in jobs:
        if not j.valid():
            continue
        key = j.key()
        if key not in grouped:
            grouped[key] = j
        else:
            original = grouped[key]
            for p in j.positions:
                if p not in original.positions:
                    original.positions.append(p)
            if not original.apply and j.apply:
                original.apply = j.apply
            if not original.circular and j.circular:
                original.circular = j.circular
            if not original.start and j.start:
                original.start = j.start
    return sorted(grouped.values(), key=lambda j: (j.start, j.deadline, j.organization), reverse=True)


def format_message(j: Job, reminder=False) -> str:
    remaining = (date.fromisoformat(j.deadline) - TODAY()).days
    title = "⏰ <b>আবেদনের শেষ সময় কাছে</b>" if reminder else "🇧🇩 <b>সরকারি চাকরির নতুন বিজ্ঞপ্তি</b>"
    position = ", ".join(j.positions[:8]) + (" …" if len(j.positions) > 8 else "")
    parts = [title, "", "🏢 <b>প্রতিষ্ঠান:</b> " + html.escape(j.organization),
             "💼 <b>পদ:</b> " + html.escape(position),
             "🗓 <b>আবেদন শুরু:</b> " + (j.start or "যাচাই করা যায়নি"),
             "📅 <b>শেষ তারিখ:</b> " + j.deadline,
             f"⏳ <b>বাকি:</b> {remaining} দিন", ""]
    if j.circular:
        is_pdf = urlsplit(j.circular).path.lower().endswith(".pdf")
        link_text = "📄 মূল PDF" if is_pdf else "📄 অফিসিয়াল বিজ্ঞপ্তির পেজ (সরাসরি PDF নয়)"
        parts.append('<a href="'+html.escape(j.circular, quote=True)+'">'+link_text+'</a>')
    else:
        parts.append("📄 মূল PDF/সার্কুলারের সরাসরি লিংক যাচাই করা যায়নি")
    if j.apply:
        parts.append('<a href="'+html.escape(j.apply, quote=True)+'">🔗 অফিসিয়াল আবেদন পোর্টাল</a>')
    else:
        parts.append("🔗 সরাসরি আবেদন লিংক যাচাই করা যায়নি—বিজ্ঞপ্তির পেজ দেখুন")
    parts.append('<a href="'+html.escape(j.source, quote=True)+'">🔎 অফিসিয়াল উৎসের পেজ</a>')
    parts.append("⚠️ আবেদন করার আগে অফিসিয়াল বিজ্ঞপ্তি ও যোগ্যতা মিলিয়ে নিন।")
    return "\n".join(parts)


def load_state() -> dict:
    if not STATE.exists():
        return {"seen": [], "warned": []}
    content = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(content, dict) or not isinstance(content.get("seen", []), list):
        raise RuntimeError("state.json damaged; refusing to resend duplicates")
    return {"seen": content.get("seen", []), "warned": content.get("warned", [])}


def send_telegram(token: str, chat_id: str, message: str):
    # Never include the bot token in error logs.
    try:
        response = requests.post("https://api.telegram.org/bot"+token+"/sendMessage",
                                 data={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": "true"},
                                 timeout=(6, 18))
        reply = response.json()
        if response.status_code >= 400 or not reply.get("ok"):
            raise RuntimeError("Telegram delivery rejected: HTTP %s; %s" % (response.status_code, clean(reply.get("description"))[:120]))
    except requests.RequestException as err:
        raise RuntimeError("Telegram delivery network error: " + type(err).__name__) from None


def process(jobs: list[Job], token: str, chat_id: str) -> int:
    state = load_state()
    seen, warned = set(state["seen"]), set(state["warned"])
    total = 0
    for j in jobs:
        if total >= MAX_SEND:
            break
        key = j.key()
        days = (date.fromisoformat(j.deadline) - TODAY()).days
        remindkey = f"{key}:{days}"
        new = key not in seen
        due = not new and days in (3, 1, 0) and remindkey not in warned
        if not (new or due):
            continue
        send_telegram(token, chat_id, format_message(j, reminder=due))
        if new:
            seen.add(key)
        if due:
            warned.add(remindkey)
        total += 1
        # Persist after EACH successful send to reduce duplicates after errors.
        STATE.write_text(json.dumps({"seen": sorted(seen)[-2000:], "warned": sorted(warned)[-2000:]}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    token, chat_id = os.environ.get("BOT_TOKEN", "").strip(), os.environ.get("CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.error("Missing BOT_TOKEN or CHAT_ID in GitHub Actions secrets")
        return 2
    jobs, report = fetch_alljobs()
    try:
        fetch_vas()
    except Exception as err:
        LOG.warning("Teletalk VAS optional source unavailable: %s", type(err).__name__)
    groups = merge(jobs)
    LOG.info("SUMMARY listings=%d cards=%d json_objects=%d verified=%d", report["pages"], report["cards"], report["json_objects"], len(groups))
    if not groups:
        LOG.error("NO VERIFIED JOBS. Listing pages may have changed or omit required fields. ")
        LOG.error("Do not interpret a green workflow as successful scraping. No messages sent.")
        return 2
    total = process(groups, token, chat_id)
    LOG.info("SUCCESS verified_groups=%d telegram_sent=%d (zero sent may mean all already notified)", len(groups), total)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        logging.exception("Unexpected failure (%s)", type(error).__name__)
        sys.exit(1)
