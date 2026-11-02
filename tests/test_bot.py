import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'bot.py'
spec = importlib.util.spec_from_file_location('bd_alert_bot', SCRIPT)
bot = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)


class FakeResp:
    status_code = 200
    ok = True
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload


class FakeSession:
    def __init__(self, payload): self.payload = payload; self.calls=[]
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return FakeResp(self.payload)


class TestBot(unittest.TestCase):
    def sample(self, **changes):
        row = {'job_primary_id': 15084, 'org_name':'Department of National Savings',
               'job_title':'Office Assistant', 'deadline_date':'2026-11-26',
               'application_site_url':'https://nsd.teletalk.com.bd/'}
        row.update(changes)
        return row

    def test_valid_job_and_date(self):
        j=bot.parse_job(self.sample(),today=date(2026,11,2))
        self.assertIsNotNone(j)
        self.assertEqual(j.deadline,'2026-11-26')
        self.assertEqual(j.apply,'https://nsd.teletalk.com.bd/')

    def test_expired(self):
        self.assertIsNone(bot.parse_job(self.sample(deadline_date='2026-11-01'),today=date(2026,11,2)))

    def test_missing_info_and_unsafe_url(self):
        self.assertIsNone(bot.parse_job(self.sample(org_name='undefined'),today=date(2026,11,2)))
        self.assertIsNone(bot.parse_job(self.sample(application_site_url='https://fake-teletalk.com.bd/'),today=date(2026,11,2)))

    def test_timestamp_conversion(self):
        self.assertEqual(bot.local_date('2026-11-26T23:30:00Z'), '2026-11-27')
        self.assertEqual(bot.local_date(''), '')

    def test_extract_rows(self):
        data={'data': {'govtJobs':[self.sample()]}}
        self.assertEqual(len(bot.extract_rows(data)),1)
        self.assertIsNone(bot.extract_rows({'data': {'jobs':[]}}))

    def test_fetch_mock(self):
        sess=FakeSession({'govtJobs':[self.sample()]})
        original = bot.parse_job
        with patch.object(bot, 'parse_job', side_effect=lambda row: original(row, today=date(2026,11,2))):
            jobs=bot.fetch(sess, keywords=[''])
        self.assertEqual(len(jobs),1)
        self.assertEqual(len(sess.calls),1)

    def test_dedup(self):
        j=bot.parse_job(self.sample(),today=date(2026,11,2))
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, 'STATE', Path(temp)/'state.json'):
                with patch.object(bot, 'send_telegram') as send:
                    self.assertEqual(bot.deliver([j], 'token','chat',today=date(2026,11,2)),1)
                    self.assertEqual(bot.deliver([j], 'token','chat',today=date(2026,11,2)),0)
                    self.assertEqual(send.call_count,1)

if __name__=='__main__': unittest.main()
