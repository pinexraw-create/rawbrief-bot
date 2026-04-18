#!/usr/bin/env python3
import os
import sys
import time
import requests
import anthropic
import urllib.request
import urllib.error
import json
from datetime import datetime, timedelta

NEWS_API_KEY = os.environ.get(“NEWS_API_KEY”)
ANTHROPIC_API_KEY = os.environ.get(“ANTHROPIC_API_KEY”)
TELEGRAM_BOT_TOKEN = os.environ.get(“TELEGRAM_BOT_TOKEN”)
TELEGRAM_CHANNEL_ID = os.environ.get(“TELEGRAM_CHANNEL_ID”)

MAX_RETRIES = 10
RETRY_INTERVAL_SECONDS = 5 * 60

def check_env():
missing = []
for var in [“NEWS_API_KEY”, “ANTHROPIC_API_KEY”, “TELEGRAM_BOT_TOKEN”, “TELEGRAM_CHANNEL_ID”]:
if not os.environ.get(var):
missing.append(var)
if missing:
print(f”  ERROR: Missing environment variables: {’, ’.join(missing)}”)
sys.exit(1)

def verify_telegram_bot():
url = f”https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe”
try:
req = urllib.request.Request(url, method=“GET”)
with urllib.request.urlopen(req, timeout=10) as response:
result = json.loads(response.read().decode(“utf-8”))
if result.get(“ok”):
bot = result[“result”]
print(f”  Bot verified: @{bot[‘username’]} ({bot[‘first_name’]})”)
return bot
else:
print(f”  ERROR: Invalid bot token - {result.get(‘description’)}”)
sys.exit(1)
except Exception as e:
print(f”  ERROR: Could not reach Telegram API - {e}”)
sys.exit(1)

def fetch_prices():
prices = {}
try:
r = requests.get(
“https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true”,
timeout=10
)
r.raise_for_status()
data = r.json()
btc_price = data[“bitcoin”][“usd”]
btc_change = data[“bitcoin”][“usd_24h_change”]
sign = “+” if btc_change >= 0 else “”
prices[“BTC”] = f”${btc_price:,.0f} ({sign}{btc_change:.1f}%)”
except Exception as e:
print(f”  WARNING: Could not fetch BTC price: {e}”)
prices[“BTC”] = “N/A”

```
for ticker, label in [("%5EGSPC", "SPX"), ("GC%3DF", "Gold")]:
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        meta = data["chart"]["result"][0]["meta"]
        price = meta["regularMarketPrice"]
        prev_close = meta["previousClose"]
        change_pct = ((price - prev_close) / prev_close) * 100
        sign = "+" if change_pct >= 0 else ""
        if label == "Gold":
            prices["Gold"] = f"${price:,.0f}/oz ({sign}{change_pct:.1f}%)"
        else:
            prices["SPX"] = f"{price:,.0f} ({sign}{change_pct:.1f}%)"
    except Exception as e:
        print(f"  WARNING: Could not fetch {label} price: {e}")
        prices[label] = "N/A"

print(f"  BTC: {prices.get('BTC')} | S&P 500: {prices.get('SPX')} | Gold: {prices.get('Gold')}")
return prices
```

def fetch_fear_greed():
try:
r = requests.get(
“https://api.alternative.me/fng/?limit=1”,
timeout=10
)
r.raise_for_status()
data = r.json()
entry = data[“data”][0]
value = entry[“value”]
classification = entry[“value_classification”]
print(f”  Fear & Greed Index: {value} ({classification})”)
return f”{value}/100 - {classification}”
except Exception as e:
print(f”  WARNING: Could not fetch Fear & Greed Index: {e}”)
return “N/A”

def fetch_news():
base_url = “https://newsapi.org/v2/everything”
yesterday = (datetime.utcnow() - timedelta(days=1)).strftime(”%Y-%m-%d”)

```
queries = [
    ('bitcoin OR ethereum OR "bitcoin ETF" OR "ethereum ETF" OR "crypto ETF" OR BTC OR ETH OR "crypto regulation"', "crypto"),
    ('"S&P 500" OR Nasdaq OR Apple OR Tesla OR Microsoft OR Google OR Meta OR Nvidia OR Amazon OR JPMorgan OR "Goldman Sachs" OR "Morgan Stanley" OR earnings', "stocks"),
    ('"gold price" OR "silver price" OR "WTI" OR "Brent crude" OR "crude oil" OR "natural gas price" OR "wheat price" OR "copper price"', "commodities"),
]

all_articles = []
for query, category in queries:
    params = {
        "q": query,
        "from": yesterday,
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 7,
        "apiKey": NEWS_API_KEY,
    }
    try:
        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("status") == "error":
            print(f"  WARNING: NewsAPI error for {category}: {data.get('message')}")
            continue

        articles = data.get("articles", [])
        count = 0
        for article in articles:
            if article.get("title") and article.get("description"):
                if article["title"] == "[Removed]":
                    continue
                all_articles.append({
                    "category": category,
                    "title": article["title"],
                    "description": article.get("description", ""),
                    "url": article.get("url", ""),
                    "source": article.get("source", {}).get("name", "Unknown"),
                    "publishedAt": article.get("publishedAt", ""),
                })
                count += 1
                if count >= 6:
                    break

        print(f"  [{category.upper()}] Fetched {count} articles")

    except requests.RequestException as e:
        print(f"  WARNING: Failed to fetch {category} news: {e}")

return all_articles
```

def format_with_claude(articles, prices, fear_greed):
if not articles:
return None

```
articles_text = ""
for i, article in enumerate(articles, 1):
    articles_text += (
        f"{i}. [{article['category'].upper()}] {article['title']}\n"
        f"   Summary: {article['description']}\n\n"
    )

today = datetime.utcnow().strftime("%B %d, %Y | %H:%M UTC")
btc = prices.get("BTC", "N/A")
spx = prices.get("SPX", "N/A")
gold = prices.get("Gold", "N/A")

prompt = (
    "You are a financial news editor for a beginner-friendly Telegram channel. "
    "Produce today's post using EXACTLY the template below.\n\n"
    "TEMPLATE:\n\n"
    f"Raw Brief - {today}\n"
    "------------------------------\n"
    f"BTC {btc}\n"
    f"S&P 500 {spx}\n"
    f"Gold {gold}\n"
    f"Fear & Greed {fear_greed}\n"
    "------------------------------\n"
    "Crypto\n"
    "-> [bullet] [emoji]\n"
    "-> [bullet] [emoji]\n"
    "------------------------------\n"
    "Stocks\n"
    "-> [bullet] [emoji]\n"
    "-> [bullet] [emoji]\n"
    "------------------------------\n"
    "Commodities\n"
    "-> [bullet] [emoji]\n"
    "-> [bullet] [emoji]\n"
    "------------------------------\n"
    "Not financial advice. DYOR.\n\n"
    "BULLET RULES:\n"
    "1. Exactly 2 bullets per section\n"
    "2. Every bullet MUST include a specific number, price, or percentage\n"
    "3. Emoji goes at the END of each bullet\n"
    "4. Max 15 words per bullet\n"
    "5. Write like a sharp confident friend, not a news wire\n"
    "6. No URLs, no source names, no markdown\n"
    "7. Crypto: only Bitcoin, Ethereum, major ETFs, crypto regulation\n"
    "8. Stocks: only top S&P 500 companies or major bank earnings\n"
    "9. Commodities: only gold, silver, oil, natural gas, wheat, copper\n\n"
    "Articles:\n"
    f"{articles_text}\n"
    "Write only the post. Nothing else."
)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
message = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=1500,
    messages=[{"role": "user", "content": prompt}],
)
return message.content[0].text.strip()
```

def send_to_telegram(text):
url = f”https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage”
payload = {
“chat_id”: TELEGRAM_CHANNEL_ID,
“text”: text,
“disable_web_page_preview”: True,
“disable_notification”: False,
}
try:
data = json.dumps(payload).encode(“utf-8”)
req = urllib.request.Request(
url,
data=data,
headers={“Content-Type”: “application/json”},
method=“POST”,
)
with urllib.request.urlopen(req, timeout=15) as response:
result = json.loads(response.read().decode(“utf-8”))
if result.get(“ok”):
msg_id = result[“result”][“message_id”]
print(f”  Message sent! Message ID: {msg_id}”)
return True
else:
desc = result.get(“description”, “Unknown error”)
print(f”  ERROR: Telegram rejected the message - {desc}”)
return False
except urllib.error.HTTPError as e:
body = e.read().decode(“utf-8”)
error_data = json.loads(body) if body else {}
desc = error_data.get(“description”, e.reason)
print(f”  ERROR {e.code}: {desc}”)
if “chat not found” in desc.lower():
print(”  HINT: The bot must be an admin of the channel.”)
return False
except Exception as e:
print(f”  ERROR: Unexpected failure - {e}”)
return False

def _attempt_post():
print(”\n[1/5] Checking environment variables…”)
check_env()
print(”  All secrets present.”)

```
print("\n[2/5] Verifying Telegram bot...")
verify_telegram_bot()

print("\n[3/5] Fetching live data...")
prices = fetch_prices()
fear_greed = fetch_fear_greed()
articles = fetch_news()

if not articles:
    print("  No articles found.")
    return False

print(f"  Total articles collected: {len(articles)}")

print("\n[4/5] Formatting post with Claude Haiku...")
post_text = format_with_claude(articles, prices, fear_greed)

if not post_text:
    print("  Claude returned no content.")
    return False

print("  Post formatted successfully.")
print()
print("-" * 55)
print(post_text)
print("-" * 55)

print("\n[5/5] Sending to Telegram channel...")
return send_to_telegram(post_text)
```

def run_bot():
now = datetime.utcnow().strftime(”%Y-%m-%d %H:%M UTC”)
print(”\n” + “=” * 55)
print(f”   Running bot - {now}”)
print(”=” * 55)

```
for attempt in range(1, MAX_RETRIES + 1):
    if attempt > 1:
        print(f"\n  Retry {attempt - 1}/{MAX_RETRIES - 1} - waiting 5 minutes...")
        time.sleep(RETRY_INTERVAL_SECONDS)

    print(f"\n--- Attempt {attempt}/{MAX_RETRIES} @ {datetime.utcnow().strftime('%H:%M UTC')} ---")

    try:
        success = _attempt_post()
    except Exception as e:
        print(f"  UNEXPECTED ERROR: {e}")
        success = False

    if success:
        print("\n" + "=" * 55)
        print("  Done! Post delivered to Telegram channel.")
        print("=" * 55)
        return

    print(f"  Attempt {attempt} failed.")

print("\n" + "=" * 55)
print(f"  All {MAX_RETRIES} attempts failed. Waiting for next scheduled time.")
print("=" * 55)
```

def start_scheduler():
from apscheduler.schedulers.blocking import BlockingScheduler
scheduler = BlockingScheduler(timezone=“UTC”)

```
for hour in [8, 13, 20]:
    scheduler.add_job(run_bot, "cron", hour=hour, minute=0)
    print(f"  Scheduled: {hour:02d}:00 UTC")

print("\n" + "=" * 55)
print("   Raw Brief Bot - Scheduler Active")
print("=" * 55)
print("  Posts at: 08:00, 13:00, 20:00 UTC")
print("  Press Ctrl+C to stop.")
print("=" * 55)

try:
    scheduler.start()
except (KeyboardInterrupt, SystemExit):
    print("\n  Scheduler stopped.")
```

if **name** == “**main**”:
import argparse
parser = argparse.ArgumentParser(description=“Raw Brief Telegram Bot”)
parser.add_argument(”–once”, action=“store_true”, help=“Run once and exit”)
args = parser.parse_args()

```
if args.once:
    run_bot()
else:
    check_env()
    start_scheduler()
```
