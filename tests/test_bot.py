from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
import bot

bot.TODAY = lambda: date(2026, 11, 2)


def sample_card():
    return {
        "detail": "https://alljobs.teletalk.com.bd/exam-notices/15081",
        "text": "Office Support Staff\nTaxes Zone-19, Dhaka (TAX19)\nCircular Status: Live\nApplication Start Date\n2026-11-01\nApplication End Date\n2026-11-21\nDownload Notice",
        "headings": ["Office Support Staff"],
        "links": [
            {"label": "View Circular", "url": "https://alljobs.teletalk.com.bd/exam-notices/15081"},
            {"label": "Apply Online", "url": "https://tax19.teletalk.com.bd/tax19_2026/"},
        ],
    }


def test_listing_card_official_details():
    j = bot.from_card(sample_card(), bot.PAGES[1])
    assert j is not None
    assert j.organization == "Taxes Zone-19, Dhaka (TAX19)"
    assert j.position == "Office Support Staff"
    assert j.deadline == "2026-11-21"
    assert j.apply == "https://tax19.teletalk.com.bd/tax19_2026"
    assert j.source.endswith("15081")
    assert j.circular == "https://alljobs.teletalk.com.bd/exam-notices/15081"


def test_expired_or_unverified_rejected():
    c = sample_card()
    c["text"] = c["text"].replace("2026-11-21", "2026-10-30")
    assert bot.from_card(c, bot.PAGES[1]) is None
    c = sample_card()
    c["text"] = c["text"].replace("2026-11-21", "undefined")
    assert bot.from_card(c, bot.PAGES[1]) is None
    assert not bot.official("https://fakegov.bd.evil.test/notices")


def test_json_envelopes():
    data = {"data": {"jobs": [
        {"companyName": "Bangladesh Armed Services Board (BASB)",
         "postName": "Verification Officer", "applicationStartDate": "2026-11-01",
         "applicationEndDate": "2026-11-21",
         "applyLink": "https://basb.teletalk.com.bd/",
         "noticeUrl": "https://basb.teletalk.com.bd/circulars.php"},
        {"companyName": "Old Office", "postName": "Clerk", "applicationEndDate": "2025-11-21"}
    ]}}
    result = [j for obj in bot.json_objects(data) if (j := bot.from_json(obj, bot.PAGES[1]))]
    assert len(result) == 1
    assert result[0].position == "Verification Officer"
    assert result[0].start == "2026-11-01"


def test_no_fabricated_application_url():
    c = sample_card()
    c["links"] = [c["links"][0]]
    j = bot.from_card(c, bot.PAGES[1])
    assert j and not j.apply
    assert "যাচাই করা যায়নি" in bot.format_message(j)


def test_dedup_state_and_messages(tmp_path):
    bot.STATE = tmp_path / "state.json"
    bot.STATE.write_text('{"seen": [], "warned": []}', encoding="utf-8")
    sent = []
    bot.send_telegram = lambda token, chat_id, msg: sent.append(msg)
    a = bot.from_card(sample_card(), bot.PAGES[1])
    assert bot.process([a], "secret", "1") == 1
    assert bot.process([a], "secret", "1") == 0
    assert len(sent) == 1
    assert len(json.loads(bot.STATE.read_text())["seen"]) == 1


def test_missing_fields_do_not_create_false_alert():
    assert bot.from_json({"title": "Government Jobs", "createdAt": "2026-11-02", "organization": "Any Office"}, bot.PAGES[0]) is None
    bad = sample_card()
    bad["headings"] = ["undefined"]
    bad["text"] = "undefined\nApplication End Date 2026-11-21"
    assert bot.from_card(bad, bot.PAGES[1]) is None
