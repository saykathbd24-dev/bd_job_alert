import importlib.util
from pathlib import Path
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'bot.py'
spec = importlib.util.spec_from_file_location('bot', path)
bot = importlib.util.module_from_spec(spec)
import sys
sys.modules['bot'] = bot
spec.loader.exec_module(bot)

html = '''<a href="/jobs/government/878?jobId=14911">CID</a>
<script>"/jobs/government/1089?jobId=14784"</script>'''
assert len(bot.detail_urls(html)) == 2
assert bot.trusted_apply('https://cid.teletalk.com.bd/')
assert not bot.trusted_apply('https://evil.example.com')
assert bot.job_detail('<h1>Some Job</h1>Application Start Date 2026-10-01 Application End Date 2026-11-08', 'https://alljobs.teletalk.com.bd/jobs/government/1') is None
j = bot.Job(org='Sample Agency', apply='https://cid.teletalk.com.bd/',start='2026-10-11',end='2026-11-08',positions=['Office Assistant'],source='https://alljobs.teletalk.com.bd/jobs/government/878?jobId=14911')
assert 'cid.teletalk.com.bd' in bot.message(j)
print('4 local assertions passed')
