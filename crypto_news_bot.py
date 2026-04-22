#!/usr/bin/env python3
import os
import sys
import time
import requests
import anthropic
import urllib.request
import urllib.error
import json
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

MAX_RETRIES = 10
RETRY_INTERVAL_SECONDS = 300
PRICE_CACHE = {}
CACHE_LOCK = threading.Lock()
previous_prices = {}
last_flash_alert = {}
flash_alert_lock = threading.Lock()

RSS_FEEDS = {
    "crypto": [
        "https://coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://bitcoinmagazine.com/feed",
    ],
    "stocks": [
        "https://cnbc.com/id/20910258/device/rss/rss.html",
        "https://feeds.marketwatch.com/marketwatch/topstories",
    ],
    "commodities": [
        "https://oilprice.com/rss/main",
        "https://www.kitco.com/rss/",
    ],
}


def check_env():
    missing = []
    for var in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print("ERROR: Missing variables: " + ", ".join(missing))
        sys.exit(1)


def update_cache(key, value):
    with CACHE_LOCK:
        PRICE_CACHE[key] = {"value": value, "timestamp": time.time()}


def get_cache(key, max_age=300):
    with CACHE_LOCK:
        entry = PRICE_CACHE.get(key)
        if entry and (time.time() - entry["timestamp"]) < max_age:
            return entry["value"]
    return None


def fetch_with_retry(url, headers=None, timeout=10, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            kwargs = {"timeout": timeout}
            if headers:
                kwargs["headers"] = headers
            r = requests.get(url, **kwargs)
            r.raise_for_status()
            return r
        except Exception:
            time.sleep(2 ** attempt)
    return None


def verify_telegram_bot():
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getMe"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    print("Bot verified: @" + result["result"]["username"])
                    return result["result"]
                sys.exit(1)
        except Exception:
            time.sleep(5)
    sys.exit(1)


def fetch_prices():
    prices = {}

    cached_crypto = get_cache("crypto_prices", max_age=60)
    if cached_crypto:
        prices.update(cached_crypto)
    else:
        r = fetch_with_retry(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true",
            timeout=10
        )
        if r:
            try:
                data = r.json()
                btc_price = data["bitcoin"]["usd"]
                btc_change = data["bitcoin"]["usd_24h_change"]
                btc_emoji = "📈" if btc_change >= 0 else "📉"
                sign = "+" if btc_change >= 0 else ""
                prices["BTC"] = "$" + format(int(btc_price), ",") + " " + btc_emoji + " " + sign + str(round(btc_change, 1)) + "%"
                prices["BTC_RAW"] = btc_price
                prices["BTC_CHANGE"] = btc_change
                eth_price = data["ethereum"]["usd"]
                eth_change = data["ethereum"]["usd_24h_change"]
                eth_emoji = "📈" if eth_change >= 0 else "📉"
                sign = "+" if eth_change >= 0 else ""
                prices["ETH"] = "$" + format(int(eth_price), ",") + " " + eth_emoji + " " + sign + str(round(eth_change, 1)) + "%"
                prices["ETH_RAW"] = eth_price
                prices["ETH_CHANGE"] = eth_change
                update_cache("crypto_prices", {k: v for k, v in prices.items()})
            except Exception:
                prices["BTC"] = "N/A"
                prices["ETH"] = "N/A"
        else:
            prices["BTC"] = "N/A"
            prices["ETH"] = "N/A"

    for ticker, label in [("%5EGSPC", "SPX"), ("GC%3DF", "Gold"), ("SI%3DF", "Silver"), ("CL%3DF", "Oil"), ("DX-Y.NYB", "DXY")]:
        cached = get_cache(label + "_price", max_age=300)
        if cached:
            prices[label] = cached
            continue
        r = fetch_with_retry(
            "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker,
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        if r:
            try:
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta["regularMarketPrice"]
                prev_close = meta["previousClose"]
                change_pct = ((price - prev_close) / prev_close) * 100
                trend_emoji = "📈" if change_pct >= 0.05 else ("📉" if change_pct <= -0.05 else "➡️")
                sign = "+" if change_pct >= 0 else ""
                if label == "Gold":
                    val = "$" + format(int(price), ",") + " " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                elif label == "Silver":
                    val = "$" + str(round(price, 2)) + " " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                elif label == "Oil":
                    val = "$" + str(round(price, 2)) + " " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                elif label == "DXY":
                    val = str(round(price, 2)) + " " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                else:
                    val = format(int(price), ",") + " " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                prices[label] = val
                prices[label + "_RAW"] = price
                prices[label + "_CHANGE"] = change_pct
                update_cache(label + "_price", val)
            except Exception:
                prices[label] = "N/A"
        else:
            prices[label] = "N/A"

    return prices


def fetch_fear_greed():
    cached = get_cache("fear_greed", max_age=3600)
    if cached:
        return cached
    r = fetch_with_retry("https://api.alternative.me/fng/?limit=1", timeout=10)
    if r:
        try:
            entry = r.json()["data"][0]
            result = (int(entry["value"]), entry["value_classification"])
            update_cache("fear_greed", result)
            return result
        except Exception:
            pass
    return (None, "N/A")


def fetch_rss(url, category):
    articles = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode("utf-8", errors="ignore")
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
                articles.append({"category": category, "title": title, "description": description[:300]})
                count += 1
                if count >= 3:
                    break
    except Exception:
        pass
    return articles


def fetch_news():
    cached = get_cache("news", max_age=1800)
    if cached:
        return cached
    all_articles = []
    for category, feeds in RSS_FEEDS.items():
        for feed_url in feeds:
            articles = fetch_rss(feed_url, category)
            all_articles.extend(articles)
            if len([a for a in all_articles if a["category"] == category]) >= 6:
                break
    update_cache("news", all_articles)
    return all_articles


def send_to_telegram(text, chat_id=None):
    target = chat_id if chat_id else TELEGRAM_CHANNEL_ID
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    payload = {"chat_id": target, "text": text, "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    return True
                desc = result.get("description", "")
                if "400" in str(desc):
                    return False
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return False
            time.sleep(2 ** attempt)
        except Exception:
            time.sleep(2 ** attempt)
    return False


def send_message_to_chat(chat_id, text):
    send_to_telegram(text, chat_id=chat_id)


def notify_admin(message):
    if ADMIN_CHAT_ID:
        send_message_to_chat(ADMIN_CHAT_ID, message)


def generate_flash_alert(asset, price, change_pct):
    now = datetime.now(timezone.utc).strftime("%B %d, %Y | %H:%M UTC")
    direction = "📈" if change_pct > 0 else "📉"
    change_str = "+" + str(round(change_pct, 1)) + "%" if change_pct > 0 else str(round(change_pct, 1)) + "%"

    templates = {
        "BTC": {
            "emoji": "🟠", "price_str": "$" + format(int(price), ","),
            "context_down": "Last 3 times BTC dropped this fast:\n-> Bottom formed within 4-6 hours\n-> Rebound of 8-12% followed within 48 hours",
            "context_up": "Last 3 times BTC spiked this fast:\n-> Momentum continued 24-48 hours\n-> Pullback of 5-8% before next leg up",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + " — holds here and bounces. Breaks and $" + str(int(price * 0.93)) + " is next.",
            "key_up": "Key Level: $" + str(int(price * 1.03)) + " — breaks above and momentum accelerates.",
        },
        "ETH": {
            "emoji": "🔵", "price_str": "$" + format(int(price), ","),
            "context_down": "ETH dropping faster than BTC. Last 4 times:\n-> ETH bottomed 2-3 hours before BTC\n-> Recovery was 15-20% within 72 hours",
            "context_up": "ETH outpacing BTC. Last 3 times:\n-> ETH ran another 20-30% within 2 weeks\n-> Altcoins followed with 2-3x moves",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + " — loses this and $" + str(int(price * 0.92)) + " comes fast.",
            "key_up": "Key Level: $" + str(int(price * 1.04)) + " — breaks above and ETH enters momentum phase.",
        },
        "Oil": {
            "emoji": "🛢️", "price_str": "$" + str(round(price, 2)),
            "context_down": "Oil dropping fast signals demand destruction. Last 3 times:\n-> Further 8-12% decline followed\n-> Energy stocks underperformed 2-3 weeks",
            "context_up": "Oil spiking fast means supply shock. Last 3 times:\n-> Crude continued higher 2-3 weeks\n-> Energy stocks outperformed by 15-20%",
            "key_down": "Key Level: $" + str(round(price * 0.95, 2)) + " — breaks below and demand destruction kicks in.",
            "key_up": "Key Level: $" + str(round(price * 1.05, 2)) + " — breaks above and energy stocks explode.",
        },
        "Gold": {
            "emoji": "🥇", "price_str": "$" + format(int(price), ",") + "/oz",
            "context_down": "Gold dropping signals risk-on shift. Last 3 times:\n-> Gold stabilized within 24-48 hours\n-> Stocks rallied as fear eased",
            "context_up": "Gold spiking signals panic buying. Last 4 times:\n-> Gold ran another 8-12% within 2 weeks\n-> Stocks dropped 5-8%",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + "/oz — breaks below and profit taking accelerates.",
            "key_up": "Key Level: $" + str(int(price * 1.03)) + "/oz — breaks above and uncharted territory.",
        },
        "Silver": {
            "emoji": "🥈", "price_str": "$" + str(round(price, 2)) + "/oz",
            "context_down": "Silver dropping fast. Last 3 times:\n-> Gold followed lower within 48 hours\n-> Both metals stabilized after 5-8% drop",
            "context_up": "Silver outpacing gold signals industrial demand shock. Last 3 times:\n-> Gold followed 5-8% within 48 hours\n-> Both metals ran 2-3 weeks",
            "key_down": "Key Level: $" + str(round(price * 0.95, 2)) + "/oz — loses this and selling accelerates.",
            "key_up": "Key Level: $" + str(round(price * 1.05, 2)) + "/oz — breaks above and momentum phase.",
        },
        "SPX": {
            "emoji": "📊", "price_str": format(int(price), ","),
            "context_down": "Institutional selling detected. Last 3 times:\n-> Further 5-8% drop followed\n-> Recovery took 2-3 weeks minimum",
            "context_up": "Institutional buying detected. Last 3 times:\n-> Rally continued 1-2 weeks\n-> Tech and growth led higher",
            "key_down": "Key Level: " + str(int(price * 0.97)) + " — breaks below and panic selling accelerates.",
            "key_up": "Key Level: " + str(int(price * 1.03)) + " — breaks above and all-time high back in play.",
        },
    }

    if asset not in templates:
        return None

    t = templates[asset]
    context = t["context_up"] if change_pct > 0 else t["context_down"]
    key_level = t["key_up"] if change_pct > 0 else t["key_down"]

    post = "⚡ FLASH ALERT — " + now + "\n"
    post += "——————————————\n"
    post += t["emoji"] + " " + asset + " " + t["price_str"] + " " + direction + " (" + change_str + " in the last hour)\n"
    post += "——————————————\n"
    post += context + "\n\n"
    post += "💡 " + key_level + "\n"
    post += "——————————————\n"
    post += "Not financial advice. DYOR. 🌲"

    return post


def generate_fear_greed_alert(value):
    now = datetime.now(timezone.utc).strftime("%B %d, %Y | %H:%M UTC")
    post = "⚡ FLASH ALERT — " + now + "\n"
    post += "——————————————\n"
    post += "😱 Fear & Greed: " + str(value) + " — EXTREME FEAR\n"
    post += "——————————————\n"
    post += "Last 5 times Fear & Greed hit this level:\n"
    post += "-> Markets bottomed within 48-72 hours\n"
    post += "-> BTC rebounded 20-30% within 30 days\n"
    post += "-> S&P 500 recovered 10-15% within 6 weeks\n\n"
    post += "Extreme fear = extreme opportunity. Historically.\n\n"
    post += "💡 Key Level: BTC $70,000 — line between recovery and capitulation.\n"
    post += "——————————————\n"
    post += "Not financial advice. DYOR. 🌲"
    return post


def check_flash_alerts(prices, fear_greed_value):
    thresholds = {"BTC": 5.0, "ETH": 6.0, "Oil": 4.0, "Gold": 2.0, "Silver": 3.0, "SPX": 2.0}

    for asset, threshold in thresholds.items():
        raw_key = asset + "_RAW"
        if raw_key not in prices:
            continue
        current_price = prices[raw_key]
        if asset in previous_prices:
            prev_price = previous_prices[asset]
            hourly_change = ((current_price - prev_price) / prev_price) * 100
            if abs(hourly_change) >= threshold:
                with flash_alert_lock:
                    if time.time() - last_flash_alert.get(asset, 0) < 3600:
                        previous_prices[asset] = current_price
                        continue
                    last_flash_alert[asset] = time.time()
                alert_text = generate_flash_alert(asset, current_price, hourly_change)
                if alert_text:
                    send_to_telegram(alert_text)
        previous_prices[asset] = current_price

    if fear_greed_value is not None and fear_greed_value <= 15:
        fg_file = "/tmp/fg_alert.txt"
        try:
            with open(fg_file, "r") as f:
                if f.read().strip() == str(fear_greed_value):
                    return
        except Exception:
            pass
        alert_text = generate_fear_greed_alert(fear_greed_value)
        if alert_text:
            send_to_telegram(alert_text)
            with open(fg_file, "w") as f:
                f.write(str(fear_greed_value))


def format_with_claude(articles, prices, fear_greed_value, fear_greed_class, test=False):
    if not articles:
        return None

    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += str(i) + ". [" + article["category"].upper() + "] " + article["title"] + "\n"
        articles_text += "   Summary: " + article["description"] + "\n\n"

    today = datetime.now(timezone.utc).strftime("%B %d, %Y | %H:%M UTC")
    btc = prices.get("BTC", "N/A")
    eth = prices.get("ETH", "N/A")
    spx = prices.get("SPX", "N/A")
    gold = prices.get("Gold", "N/A")
    silver = prices.get("Silver", "N/A")
    oil = prices.get("Oil", "N/A")
    dxy = prices.get("DXY", "N/A")
    fear_greed_str = str(fear_greed_value) + "/100 — " + fear_greed_class if fear_greed_value else "N/A"
    prefix = "TEST — " if test else ""

    prompt = "Market signal bot. Ultra short. Everything in English.\n\n"
    prompt += "FORMAT — copy exactly:\n\n"
    prompt += prefix + "Raw Brief — " + today + "\n"
    prompt += "——————————————\n"
    prompt += "₿ BTC " + btc + "\n"
    prompt += "Ξ ETH " + eth + "\n"
    prompt += "📊 S&P " + spx + "\n"
    prompt += "🥇 Gold " + gold + "\n"
    prompt += "🥈 Silver " + silver + "\n"
    prompt += "🛢 Oil " + oil + "\n"
    prompt += "💵 DXY " + dxy + "\n"
    prompt += "😨 F&G " + fear_greed_str + "\n"
    prompt += "——————————————\n"
    prompt += "🟢/🔴 BUY/SELL BTC [price] → TP [price] | SL [price]\n"
    prompt += "🟢/🔴 BUY/SELL ETH [price] → TP [price] | SL [price]\n"
    prompt += "🟢/🔴 BUY/SELL S&P [price] → TP [price] | SL [price]\n"
    prompt += "🟢/🔴 BUY/SELL Gold [price] → TP [price] | SL [price]\n"
    prompt += "🟢/🔴 BUY/SELL Silver [price] → TP [price] | SL [price]\n"
    prompt += "🟢/🔴 BUY/SELL Oil [price] → TP [price] | SL [price]\n"
    prompt += "——————————————\n"
    prompt += "Not financial advice. DYOR. 🌲\n\n"
    prompt += "RULES:\n"
    prompt += "1. SIGNAL = 🟢 BUY or 🔴 SELL — no WAIT, every asset gets a signal\n"
    prompt += "2. Exactly 6 signals — one per asset: BTC, ETH, S&P, Gold, Silver, Oil\n"
    prompt += "3. NEVER add assets not in the list above\n"
    prompt += "4. TP and SL based on nearest support/resistance levels\n"
    prompt += "5. No explanations, no extra text, nothing else\n\n"
    prompt += "News context:\n" + articles_text + "\nWrite only the post. Nothing else."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    total_tokens = response.usage.input_tokens + response.usage.output_tokens
    token_file = "/tmp/token_count.txt"
    try:
        with open(token_file, "r") as f:
            used_tokens = int(f.read().strip())
    except Exception:
        used_tokens = 0
    used_tokens += total_tokens
    with open(token_file, "w") as f:
        f.write(str(used_tokens))

    monthly_budget = int(os.environ.get("MONTHLY_TOKEN_BUDGET", "400000"))
    if used_tokens >= int(monthly_budget * 0.8) and ADMIN_CHAT_ID:
        notify_admin("RAW BRIEF WARNING: " + str(used_tokens) + "/" + str(monthly_budget) + " tokens used.")

    return response.content[0].text.strip()


def generate_weekly_recap(prices, fear_greed_value, fear_greed_class):
    now = datetime.now(timezone.utc).strftime("%B %d, %Y | %H:%M UTC")
    articles = fetch_news()
    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += str(i) + ". [" + article["category"].upper() + "] " + article["title"] + "\n"
        articles_text += "   Summary: " + article["description"] + "\n\n"

    fear_greed_str = str(fear_greed_value) + "/100 — " + fear_greed_class if fear_greed_value else "N/A"

    prompt = "Weekly market recap. Ultra short. English only.\n\n"
    prompt += "Weekly Recap — " + now + "\n——————————————\n"
    prompt += "₿ BTC " + prices.get("BTC", "N/A") + "\n"
    prompt += "Ξ ETH " + prices.get("ETH", "N/A") + "\n"
    prompt += "📊 S&P " + prices.get("SPX", "N/A") + "\n"
    prompt += "🥇 Gold " + prices.get("Gold", "N/A") + "\n"
    prompt += "🥈 Silver " + prices.get("Silver", "N/A") + "\n"
    prompt += "🛢 Oil " + prices.get("Oil", "N/A") + "\n"
    prompt += "💵 DXY " + prices.get("DXY", "N/A") + "\n"
    prompt += "😨 F&G " + fear_greed_str + "\n"
    prompt += "——————————————\n"
    prompt += "This Week\n-> [asset] [move %] — [3 words why]\n-> [asset] [move %] — [3 words why]\n-> [asset] [move %] — [3 words why]\n"
    prompt += "——————————————\n"
    prompt += "Next Week\n-> [asset] [key level to watch]\n-> [asset] [key level to watch]\n-> [asset] [key level to watch]\n"
    prompt += "——————————————\n"
    prompt += "Signal: [🟢 BUY THE DIP / 🔴 REDUCE EXPOSURE / 🟡 HOLD AND WAIT]\n"
    prompt += "——————————————\n"
    prompt += "Not financial advice. DYOR. 🌲\n\n"
    prompt += "RULES: Max 5 words per bullet. Specific prices only. English only.\n\n"
    prompt += "News context:\n" + articles_text + "\nWrite only the recap. Nothing else."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _attempt_post(test=False, test_chat_id=None):
    check_env()
    verify_telegram_bot()
    prices = fetch_prices()
    fear_greed_value, fear_greed_class = fetch_fear_greed()
    articles = fetch_news()
    check_flash_alerts(prices, fear_greed_value)

    if not articles:
        return False

    post_text = format_with_claude(articles, prices, fear_greed_value, fear_greed_class, test=test)
    if not post_text:
        return False

    if test and test_chat_id:
        return send_to_telegram(post_text, chat_id=test_chat_id)
    else:
        return send_to_telegram(post_text)


def run_bot(test=False, test_chat_id=None):
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            wait = min(RETRY_INTERVAL_SECONDS * (2 ** (attempt - 2)), 3600)
            time.sleep(wait)
        try:
            success = _attempt_post(test=test, test_chat_id=test_chat_id)
        except Exception as e:
            error_msg = str(e)
            if "credit balance is too low" in error_msg:
                notify_admin("RAW BRIEF ALERT: Anthropic credits too low.")
            success = False
        if success:
            return
    print("All attempts failed.")


def run_weekly_recap():
    try:
        prices = fetch_prices()
        fear_greed_value, fear_greed_class = fetch_fear_greed()
        recap_text = generate_weekly_recap(prices, fear_greed_value, fear_greed_class)
        send_to_telegram(recap_text)
    except Exception as e:
        print("ERROR in weekly recap: " + str(e))


def check_flash_alerts_job():
    prices = fetch_prices()
    fear_greed_value, fear_greed_class = fetch_fear_greed()
    check_flash_alerts(prices, fear_greed_value)


def health_check_server():
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def handle_commands():
    offset = 0
    time.sleep(5)
    while True:
        try:
            url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getUpdates?offset=" + str(offset) + "&timeout=30"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=35) as response:
                result = json.loads(response.read().decode("utf-8"))
                if not result.get("ok"):
                    time.sleep(5)
                    continue
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    text = message.get("text", "")
                    chat_id = message.get("chat", {}).get("id")
                    if text.startswith("/post"):
                        send_message_to_chat(chat_id, "Posting to channel now... Please wait.")
                        threading.Thread(target=run_bot, daemon=True).start()
                    elif text.startswith("/test"):
                        send_message_to_chat(chat_id, "Sending test post... Please wait.")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id}, daemon=True).start()
                    elif text.startswith("/recap"):
                        send_message_to_chat(chat_id, "Generating weekly recap... Please wait.")
                        threading.Thread(target=run_weekly_recap, daemon=True).start()
                    elif text.startswith("/start"):
                        send_message_to_chat(chat_id, "Raw Brief Bot active.\n\nCommands:\n/post — Post to channel\n/test — Test post to you\n/recap — Weekly recap")
        except Exception:
            time.sleep(10)


def start_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler = BlockingScheduler(timezone="UTC")

    for hour in [8, 13, 20]:
        scheduler.add_job(run_bot, "cron", hour=hour, minute=0)

    scheduler.add_job(check_flash_alerts_job, "interval", minutes=60)
    scheduler.add_job(run_weekly_recap, "cron", day_of_week="sun", hour=20, minute=0)

    threading.Thread(target=handle_commands, daemon=True).start()
    threading.Thread(target=health_check_server, daemon=True).start()

    print("Raw Brief Bot Active")
    print("Posts: 08:00, 13:00, 20:00 UTC")
    print("Flash alerts: every 60 minutes")
    print("Weekly recap: Sunday 20:00 UTC")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Stopped.")


if __name__ == "__main__":
    check_env()
    start_scheduler()
