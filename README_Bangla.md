# BD Govt Job Bot — Browser Edition (v2)

আগের GitHub Actions-এ `Alljobs: 0 detail URLs` আসছিল। এই সংস্করণে Chromium/Playwright দিয়ে Alljobs-এর JavaScript-এ তৈরি তালিকা পড়ার চেষ্টা করে। VAS-এর SSL সমস্যা এখনও বাইরের সার্ভারের বিষয়; সেটি এড়িয়ে Alljobs থেকে অফিসিয়াল তথ্য নেওয়ার চেষ্টা হবে।

## ফোন থেকে আপডেট (সহজ পদ্ধতি)
1. এই ZIP ডাউনলোড করে Extract করো।
2. তোমার GitHub `bd_job_alert` Repository-তে **পুরোনো `bot.py`-কে এই ZIP-এর `bot.py` দিয়ে Replace** করো।
3. **পুরোনো `requirements.txt`-কেও এই ZIP-এর `requirements.txt` দিয়ে Replace** করো।
4. GitHub-এ `.github/workflows/alerts.yml` ফাইল খুলে Edit করে ZIP-এর `.github/workflows/alerts.yml`-এর লেখা বসাও। GitHub-এ ফোল্ডারসহ পাথ হুবহু একই থাকবে।
5. `state.json` পরিবর্তন কোরো না—এতে আগের পাঠানো বিজ্ঞপ্তির হিসাব আছে।
6. Commit করে Actions → BD Government Job Telegram Alerts → Run workflow চালাও। প্রথমবার Chromium ইনস্টল হওয়ার কারণে রান বেশি সময় নিতে পারে।
7. লগ খুলে `Alljobs browser: ... detail URLs` এবং `Alljobs verified live recruitment groups: ...` দেখো।

## গুরুত্বপূর্ণ
- GitHub-এর Settings → Secrets and variables → Actions-এ আগের BOT_TOKEN ও CHAT_ID যেমন আছে, তেমনই থাকবে। এগুলো কাউকে পাঠাবে না।
- `Success` মানেই সব সরকারি চাকরির অ্যালার্ট নিশ্চিত নয়। কেবল যাচাই করা চলমান বিজ্ঞপ্তি পাওয়া এবং মেসেজ পাঠানোর লগ দিয়েই নিশ্চিত হবে।
- কোনো যাচাইযোগ্য নিয়োগ না পেলে ইচ্ছাকৃতভাবে লাল Failed হবে, যাতে নীরবে ভুল Success না দেখায়। তবে সত্যিই কোনো নতুন নিয়োগ না থাকলেও এটা হতে পারে।
- সরকারি ওয়েবসাইটের কাঠামো পরিবর্তন হলে সংগ্রহের কোড আবার সংশোধন করতে হতে পারে। এটি সব সরকারি দপ্তর বা BPSC-এর সম্পূর্ণ কভারেজ দাবি করে না।
- কোনো নিশ্চিত PDF লিংক না পেলে PDF লিংক বানাবে না।
