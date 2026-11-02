# BD Govt Job Bot: API-first fix

আগের সমস্যার কারণ: Alljobs-এর তালিকায় পদের নাম/শেষ তারিখ একই কার্ডে থাকে না এবং বিস্তারিত পেজে timeout হচ্ছিল।

এ সংস্করণে পেজ স্ক্র্যাপ করে অনুমান না করে `https://alljobs.teletalk.com.bd/api/v1/published-jobs/search?searchKeyword=` API থেকে সরকারি চাকরির নাম, প্রতিষ্ঠান, আবেদনের শেষ তারিখ ও আবেদন লিংক নেওয়ার চেষ্টা করে। API-এর এই কাঠামোর উল্লেখ একটি উন্মুক্ত GitHub প্রকল্পে পাওয়া গেছে; আমরা লাইভ API চালিয়ে পরীক্ষা করতে পারিনি।

## ফোন থেকে তিনটি ফাইল বদলাবে
1. Repository: https://github.com/saykathbd24-dev/bd_job_alert
2. `bot.py` আপডেট করো।
3. `requirements.txt` আপডেট করো।
4. `.github/workflows/alerts.yml` আপডেট করো।
5. `state.json` ও GitHub Secrets (`BOT_TOKEN`, `CHAT_ID`) অপরিবর্তিত রাখবে।
6. GitHub Actions > BD Government Job Telegram Alerts > Run workflow চালাও।

## কোন লগ ভালো?
`INFO API keyword='' rows=... verified=...` এবং `INFO SUCCESS verified_jobs=... telegram_sent=...`

## সমস্যা হলে?
`OFFICIAL API UNAVAILABLE` বা `Unexpected API JSON structure` = সার্ভার API বা response structure ভিন্ন, এই কোড দিয়ে এখনো নির্ভরযোগ্য অ্যালার্ট চালু করা যাবে না।
`NO VERIFIED JOBS` = API থেকে যাচাইযোগ্য বর্তমান আবেদন পাওয়া যায়নি।

## সীমাবদ্ধতা
- API-তে দেখা সরকারি চাকরির তথ্য পর্যন্ত সীমিত; সব BPSC ও সব সরকারি দপ্তরের বিজ্ঞপ্তি নয়।
- সরাসরি মূল PDF না থাকলে সেটি কল্পনা করে যুক্ত করা হয় না।
- আবেদন শেষ হওয়ার তারিখ ও সরাসরি আবেদন পোর্টালের URL অনুপস্থিত হলে বিজ্ঞপ্তি বাদ দেয়।
- GitHub Scheduled workflow সাধারণত প্রতি ঘণ্টায় চালানোর পরিকল্পনা করা আছে, কিন্তু GitHub বিলম্ব করতে পারে।
- এই প্যাকেজে লাইভ Alljobs API থেকে সফল Run যাচাই হয়নি; এটি আরও একটি *পরীক্ষাযোগ্য* সংশোধন।
