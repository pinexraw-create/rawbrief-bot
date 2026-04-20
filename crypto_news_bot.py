#!/usr/bin/env python3
import os
import sys
import time
import requests
import anthropic
import urllib.request
import urllib.error
import urllib.parse
import json
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

ANTHROPIC_API_KEY = os.environ.get(“ANTHROPIC_API_KEY”)
TELEGRAM_BOT_TOKEN = os.environ.get(“TELEGRAM_BOT_TOKEN”)
TELEGRAM_CHANNEL_ID = os.environ.get(“TELEGRAM_CHANNEL_ID”)
ADMIN_CHAT_ID = os.environ.get(“ADMIN_CHAT_ID”)

MAX_RETRIES = 10
RETRY_INTERVAL_SECONDS = 300

RSS_FEEDS = {
“crypto”: [
“https://coindesk.com/arc/outboundfeeds/rss/”,
“https://cointelegraph.com/rss”,
“https://bitcoinmagazine.com/feed”,
],
“stocks”: [
“https://cnbc.com/id/20910258/device/rss/rss.html”,
“https://feeds.marketwatch.com/marketwatch/topstories”,
“https://feeds.reuters.com/reuters/businessNews”,
],
“commodities”: [
“https://oilprice.com/rss/main”,
“https://www.kitco.com/rss/”,
],
}

def check_env():
missing = []
for var in [“ANTHROPIC_API_KEY”, “TELEGRAM_BOT_TOKEN”, “TELEGRAM_CHANNEL_ID”]:
if not os.environ.get(var):
missing.append(var)
if missing:
print(“ERROR: Missing variables: “ + “, “.join(missing))
sys.exit(1)

def verify_telegram_bot():
url = “https://api.telegram.org/bot” + TELEGRAM_BOT_TOKEN + “/getMe”
try:
req = urllib.request.Request(url, method=“GET”)
with urllib.request.urlopen(req, timeout=10) as response:
result = json.loads(response.read().decode(“utf-8”))
if result.get(“ok”):
bot = result[“result”]
print(“Bot verified: @” + bot[“username”])
return bot
else:
print(“ERROR: Invalid bot token”)
sys.exit(1)
except Exception as e:
print(“ERROR: Could not reach Telegram API: “ + str(e))
sys.exit(1)

def fetch_prices():
prices = {}

```
try:
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true",
        timeout=10
    )
    r.raise_for_status()
    data = r.json()

    btc_price = data["bitcoin"]["usd"]
    btc_change = data["bitcoin"]["usd_24h_change"]
    sign = "+" if btc_change >= 0 else ""
    prices["BTC"] = "$" + format(int(btc_price), ",") + " (" + sign + str(round(btc_change, 1)) + "%)"

    eth_price = data["ethereum"]["usd"]
    eth_change = data["ethereum"]["usd_24h_change"]
    sign = "+" if eth_change >= 0 else ""
    prices["ETH"] = "$" + format(int(eth_price), ",") + " (" + sign + str(round(eth_change, 1)) + "%)"

except Exception as e:
    print("WARNING: Could not fetch crypto prices: " + str(e))
    prices["BTC"] = "N/A"
    prices["ETH"] = "N/A"

for ticker, label in [("%5EGSPC", "SPX"), ("GC%3DF", "Gold"), ("SI%3DF", "Silver"), ("CL%3DF", "Oil")]:
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker,
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
            prices["Gold"] = "$" + format(int(price), ",") + "/oz (" + sign + str(round(change_pct, 1)) + "%)"
        elif label == "Silver":
            prices["Silver"] = "$" + str(round(price, 2)) + "/oz (" + sign + str(round(change_pct, 1)) + "%)"
        elif label == "Oil":
            prices["Oil"] = "$" + str(round(price, 2)) + "/barrel (" + sign + str(round(change_pct, 1)) + "%)"
        else:
            prices["SPX"] = format(int(price), ",") + " (" + sign + str(round(change_pct, 1)) + "%)"
    except Exception as e:
        print("WARNING: Could not fetch " + label + " price: " + str(e))
        prices[label] = "N/A"

print("BTC: " + prices.get("BTC", "N/A") + " | ETH: " + prices.get("ETH", "N/A") + " | S&P 500: " + prices.get("SPX", "N/A") + " | Gold: " + prices.get("Gold", "N/A") + " | Silver: " + prices.get("Silver", "N/A") + " | Oil: " + prices.get("Oil", "N/A"))
return prices
```

def fetch_fear_greed():
try:
r = requests.get(“https://api.alternative.me/fng/?limit=1”, timeout=10)
r.raise_for_status()
data = r.json()
entry = data[“data”][0]
value = entry[“value”]
classification = entry[“value_classification”]
print(“Fear & Greed Index: “ + value + “ (” + classification + “)”)
return value + “/100 - “ + classification
except Exception as e:
print(“WARNING: Could not fetch Fear & Greed Index: “ + str(e))
return “N/A”

def fetch_rss(url, category):
articles = []
try:
req = urllib.request.Request(url, headers={“User-Agent”: “Mozilla/5.0”})
with urllib.request.urlopen(req, timeout=10) as response:
content = response.read().decode(“utf-8”, errors=“ignore”)

```
    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        return articles

    items = channel.findall("item")
    count = 0
    for item in items:
        title = item.findtext("title", "").strip()
        description = item.findtext("description", "").strip()
        if title and description and len(description) > 30:
            articles.append({
                "category": category,
                "title": title,
                "description": description[:300],
            })
            count += 1
            if count >= 3:
                break

    print("[" + category.upper() + "] " + url.split("/")[2] + " -> " + str(count) + " articles")

except Exception as e:
    print("WARNING: Could not fetch " + url + ": " + str(e))

return articles
```

def fetch_news():
all_articles = []
for category, feeds in RSS_FEEDS.items():
for feed_url in feeds:
articles = fetch_rss(feed_url, category)
all_articles.extend(articles)
if len([a for a in all_articles if a[“category”] == category]) >= 6:
break

```
print("Total articles: " + str(len(all_articles)))
return all_articles
```

def format_with_claude(articles, prices, fear_greed, test=False):
if not articles:
return None

```
articles_text = ""
for i, article in enumerate(articles, 1):
    articles_text += str(i) + ". [" + article["category"].upper() + "] " + article["title"] + "\n"
    articles_text += "   Summary: " + article["description"] + "\n\n"

today = datetime.utcnow().strftime("%B %d, %Y | %H:%M UTC")
btc = prices.get("BTC", "N/A")
eth = prices.get("ETH", "N/A")
spx = prices.get("SPX", "N/A")
gold = prices.get("Gold", "N/A")
silver = prices.get("Silver", "N/A")
oil = prices.get("Oil", "N/A")
prefix = "TEST POST - " if test else ""

prompt = "You are a sharp market analyst writing for a Telegram channel. Your job is to tell readers exactly what to do based on current data and historical patterns. Be direct, confident and actionable. Never say buy or sell directly but make the implication crystal clear.\n\n"
prompt += "STYLE EXAMPLES - write exactly like this:\n"
prompt += "-> BTC bleeding while oil explodes. This combo drops crypto 10%+ historically. Reduce exposure now, buy back lower.\n"
prompt += "-> Oil at $96 with Hormuz blocked. Last 4 times this happened - $120 in 30 days. Energy exposure makes sense here.\n"
prompt += "-> S&P 500 at record but war escalating. Shift to defensive sectors - energy, healthcare, utilities. Growth stocks will bleed.\n"
prompt += "-> Gold breaking higher. War plus supply shock historically pushes metals 8-12% up. This is the hedge you want right now.\n"
prompt += "-> DeFi lost $13B in 2 days. Stay out completely until market stabilizes. Cash is a position.\n\n"
prompt += "Write the post using this exact format:\n\n"
prompt += prefix + "Raw Brief - " + today + "\n"
prompt += "------------------------------\n"
prompt += "BTC " + btc + "\n"
prompt += "ETH " + eth + "\n"
prompt += "S&P 500 " + spx + "\n"
prompt += "Gold " + gold + "\n"
prompt += "Silver " + silver + "\n"
prompt += "Oil " + oil + "\n"
prompt += "Fear & Greed " + fear_greed + "\n"
prompt += "------------------------------\n"
prompt += "[SENTIMENT] Crypto\n"
prompt += "-> [bullet] [emoji]\n"
prompt += "-> [bullet] [emoji]\n"
prompt += "------------------------------\n"
prompt += "[SENTIMENT] Stocks\n"
prompt += "-> [bullet] [emoji]\n"
prompt += "-> [bullet] [emoji]\n"
prompt += "------------------------------\n"
prompt += "[SENTIMENT] Commodities\n"
prompt += "-> [bullet] [emoji]\n"
prompt += "-> [bullet] [emoji]\n"
prompt += "------------------------------\n"
prompt += "Historical data only. Not financial advice. DYOR.\n\n"
prompt += "RULES:\n"
prompt += "1. Exactly 2 bullets per section\n"
prompt += "2. Every bullet must include a specific price, number or percentage\n"
prompt += "3. Emoji at the END of each bullet\n"
prompt += "4. Max 20 words per bullet - short and punchy\n"
prompt += "5. Every bullet must include historical context\n"
prompt += "6. Every bullet must end with a clear actionable direction:\n"
prompt += "   BULLISH: 'Load up or miss it.', 'Smart money is already in.', 'This is the entry.', 'Energy exposure makes sense here.', 'This is the hedge you want.'\n"
prompt += "   BEARISH: 'Reduce exposure now.', 'Get out before the herd.', 'Cash is a position.', 'Stay out until stabilized.', 'Protect your capital.'\n"
prompt += "   NEUTRAL: 'Watch this level closely.', 'Next move decides everything.', 'Tight stop losses here.'\n"
prompt += "7. Replace [SENTIMENT] with the correct emoji label based on overall outlook for that section:\n"
prompt += "   🟢 Bullish — if data suggests upward move\n"
prompt += "   🔴 Bearish — if data suggests downward move\n"
prompt += "   🟡 Neutral — if mixed signals\n"
prompt += "8. No URLs, no source names, no markdown\n"
prompt += "9. Crypto: only Bitcoin, Ethereum, major ETFs, crypto regulation\n"
prompt += "10. Stocks: only top S&P 500 companies or major bank earnings\n"
prompt += "11. Commodities: only gold, silver, oil, natural gas, wheat, copper\n"
prompt += "12. If institutional data is available such as ETF inflows or central bank buying - always lead with it\n"
prompt += "13. Write in English\n\n"
prompt += "Articles:\n" + articles_text + "\n"
prompt += "Write only the post. Nothing else."

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
message = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=1500,
    messages=[{"role": "user", "content": prompt}],
)
return message.content[0].text.strip()
```

def send_to_telegram(text, chat_id=None):
target = chat_id if chat_id else TELEGRAM_CHANNEL_ID
url = “https://api.telegram.org/bot” + TELEGRAM_BOT_TOKEN + “/sendMessage”
payload = {
“chat_id”: target,
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
print(“Message sent! ID: “ + str(msg_id))
return True
else:
desc = result.get(“description”, “Unknown error”)
print(“ERROR: Telegram rejected - “ + desc)
return False
except urllib.error.HTTPError as e:
body = e.read().decode(“utf-8”)
error_data = json.loads(body) if body else {}
desc = error_data.get(“description”, e.reason)
print(“ERROR “ + str(e.code) + “: “ + desc)
return False
except Exception as e:
print(“ERROR: Unexpected failure - “ + str(e))
return False

def send_message_to_chat(chat_id, text):
url = “https://api.telegram.org/bot” + TELEGRAM_BOT_TOKEN + “/sendMessage”
payload = {“chat_id”: chat_id, “text”: text}
try:
data = json.dumps(payload).encode(“utf-8”)
req = urllib.request.Request(
url,
data=data,
headers={“Content-Type”: “application/json”},
method=“POST”,
)
urllib.request.urlopen(req, timeout=10)
except Exception as e:
print(“ERROR sending message to chat: “ + str(e))

def notify_admin(message):
if ADMIN_CHAT_ID:
send_message_to_chat(ADMIN_CHAT_ID, message)

def _attempt_post(test=False, test_chat_id=None):
check_env()
verify_telegram_bot()

```
prices = fetch_prices()
fear_greed = fetch_fear_greed()
articles = fetch_news()

if not articles:
    print("No articles found.")
    return False

post_text = format_with_claude(articles, prices, fear_greed, test=test)

if not post_text:
    print("Claude returned no content.")
    return False

print("-" * 55)
print(post_text)
print("-" * 55)

if test and test_chat_id:
    return send_to_telegram(post_text, chat_id=test_chat_id)
else:
    return send_to_telegram(post_text)
```

def run_bot(test=False, test_chat_id=None):
now = datetime.utcnow().strftime(”%Y-%m-%d %H:%M UTC”)
print(”\n” + “=” * 55)
if test:
print(“TEST RUN - “ + now)
else:
print(“Running bot - “ + now)
print(”=” * 55)

```
for attempt in range(1, MAX_RETRIES + 1):
    if attempt > 1:
        print("\nRetry " + str(attempt - 1) + " - waiting 5 minutes...")
        time.sleep(RETRY_INTERVAL_SECONDS)

    print("\n--- Attempt " + str(attempt) + "/" + str(MAX_RETRIES) + " ---")

    try:
        success = _attempt_post(test=test, test_chat_id=test_chat_id)
    except Exception as e:
        error_msg = str(e)
        print("UNEXPECTED ERROR: " + error_msg)
        if "credit balance is too low" in error_msg:
            notify_admin("RAW BRIEF BOT ALERT: Anthropic API credits too low. Top up at console.anthropic.com")
        success = False

    if success:
        print("\n" + "=" * 55)
        if test:
            print("Test post delivered to your chat.")
        else:
            print("Done! Post delivered to channel.")
        print("=" * 55)
        return

    print("Attempt " + str(attempt) + " failed.")

print("All " + str(MAX_RETRIES) + " attempts failed.")
```

def handle_commands():
offset = 0
print(“Command listener active.”)
time.sleep(5)
while True:
try:
url = “https://api.telegram.org/bot” + TELEGRAM_BOT_TOKEN + “/getUpdates?offset=” + str(offset) + “&timeout=30”
req = urllib.request.Request(url, method=“GET”)
with urllib.request.urlopen(req, timeout=35) as response:
result = json.loads(response.read().decode(“utf-8”))
if not result.get(“ok”):
time.sleep(5)
continue
for update in result.get(“result”, []):
offset = update[“update_id”] + 1
message = update.get(“message”, {})
text = message.get(“text”, “”)
chat_id = message.get(“chat”, {}).get(“id”)
if text.startswith(”/post”):
print(“Received /post from chat_id: “ + str(chat_id))
send_message_to_chat(chat_id, “Posting to channel now… Please wait.”)
threading.Thread(target=run_bot).start()
elif text.startswith(”/test”):
print(“Received /test from chat_id: “ + str(chat_id))
send_message_to_chat(chat_id, “Sending test post to you now… Please wait.”)
threading.Thread(target=run_bot, kwargs={“test”: True, “test_chat_id”: chat_id}).start()
elif text.startswith(”/start”):
send_message_to_chat(chat_id, “Raw Brief Bot active.\n\nCommands:\n/post - Post to channel now\n/test - Send test post to you only”)
except Exception as e:
print(“Command listener error: “ + str(e))
time.sleep(10)

def start_scheduler():
from apscheduler.schedulers.blocking import BlockingScheduler
scheduler = BlockingScheduler(timezone=“UTC”)

```
for hour in [8, 13, 20]:
    scheduler.add_job(run_bot, "cron", hour=hour, minute=0)
    print("Scheduled: " + str(hour).zfill(2) + ":00 UTC")

cmd_thread = threading.Thread(target=handle_commands, daemon=True)
cmd_thread.start()

print("\n" + "=" * 55)
print("Raw Brief Bot - Scheduler Active")
print("Posts at: 08:00, 13:00, 20:00 UTC")
print("/post - post to channel")
print("/test - send test post to you only")
print("=" * 55)

try:
    scheduler.start()
except (KeyboardInterrupt, SystemExit):
    print("\nScheduler stopped.")
```

if **name** == “**main**”:
check_env()
start_scheduler()
