#!/usr/bin/env python3
"""
Raw Brief Bot v2
- Three different post formats by time: 11:00 (morning), 16:00 (midday), 23:00 (night)
- Morning/midday: signals + what to expect today
- Night: signals + what to expect tomorrow + economic calendar
- New RSS sources: Reuters, AP News, The Block
- Investing.com economic calendar for night post
- Smart Key Level: most critical asset at the moment
- Bullet verification: only facts from news
- New format: paragraphs, Bloomberg-style, emoji per asset
- ATR + S/R levels with minimum SL distance and R/R >= 1.5
- Sonnet model to prevent hallucinations
- Previous prices persisted across restarts
"""
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
from datetime import datetime, timedelta

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

PREV_PRICES_FILE = "/tmp/prev_prices.json"

RSS_FEEDS = {
    "crypto": [
        "https://cointelegraph.com/rss",
        "https://www.theblock.co/rss.xml",
        "https://coindesk.com/arc/outboundfeeds/rss/",
    ],
    "stocks": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rsshub.app/apnews/topics/business-news",
        "https://cnbc.com/id/20910258/device/rss/rss.html",
        "https://feeds.marketwatch.com/marketwatch/topstories",
    ],
    "commodities": [
        "https://feeds.reuters.com/reuters/commoditiesNews",
        "https://oilprice.com/rss/main",
        "https://www.kitco.com/rss/",
    ],
    "macro": [
        "https://feeds.reuters.com/reuters/economy",
        "https://feeds.marketwatch.com/marketwatch/economy-politics",
    ],
}

ECONOMIC_CALENDAR_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/economy-politics",
    "https://feeds.reuters.com/reuters/economy",
]

OHLC_TICKERS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SPX": "%5EGSPC",
    "Gold": "GC%3DF",
    "Silver": "SI%3DF",
    "Oil": "CL%3DF",
}

MIN_SL_PCT = {
    "BTC": 0.015,
    "ETH": 0.015,
    "SPX": 0.008,
    "Gold": 0.008,
    "Silver": 0.012,
    "Oil": 0.012,
}

ASSET_EMOJI = {
    "BTC": "🟠",
    "ETH": "🔵",
    "SPX": "📊",
    "Gold": "🥇",
    "Silver": "🥈",
    "Oil": "🛢",
}

ASSET_DISPLAY = {
    "BTC": "BTC",
    "ETH": "ETH",
    "SPX": "S&P 500",
    "Gold": "Gold",
    "Silver": "Silver",
    "Oil": "Oil",
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
        except Exception as e:
            time.sleep(2 ** attempt)
    return None


def load_previous_prices():
    global previous_prices
    try:
        with open(PREV_PRICES_FILE, "r") as f:
            previous_prices = json.load(f)
    except Exception:
        previous_prices = {}


def save_previous_prices():
    try:
        with open(PREV_PRICES_FILE, "w") as f:
            json.dump(previous_prices, f)
    except Exception:
        pass


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


def fetch_ohlc(asset, ticker):
    cache_key = "ohlc_" + asset
    cached = get_cache(cache_key, max_age=3600)
    if cached:
        return cached

    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + ticker
        + "?interval=1d&range=20d"
    )
    r = fetch_with_retry(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    if not r:
        return []

    try:
        data = r.json()
        result = data["chart"]["result"][0]
        indicators = result["indicators"]["quote"][0]
        highs = indicators.get("high", [])
        lows = indicators.get("low", [])
        closes = indicators.get("close", [])

        candles = []
        for h, l, c in zip(highs, lows, closes):
            if h is not None and l is not None and c is not None:
                candles.append({"high": h, "low": l, "close": c})

        update_cache(cache_key, candles)
        return candles
    except Exception as e:
        print("OHLC fetch error for " + asset + ": " + str(e))
        return []


def calculate_atr(candles, period=14):
    if len(candles) < 2:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if not true_ranges:
        return None
    recent = true_ranges[-period:]
    return sum(recent) / len(recent)


def calculate_support_resistance(candles, current_price):
    if len(candles) < 5:
        return None, None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    swing_highs = []
    swing_lows = []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append(lows[i])
    resistances_above = [h for h in swing_highs if h > current_price]
    resistance = min(resistances_above) if resistances_above else max(highs)
    supports_below = [l for l in swing_lows if l < current_price]
    support = max(supports_below) if supports_below else min(lows)
    return round(support, 2), round(resistance, 2)


def calculate_levels(asset, current_price, candles):
    if not candles or current_price is None:
        return None
    atr = calculate_atr(candles)
    support, resistance = calculate_support_resistance(candles, current_price)
    if atr is None or support is None or resistance is None:
        return None

    dist_to_support = current_price - support
    dist_to_resistance = resistance - current_price
    min_sl_dist = current_price * MIN_SL_PCT.get(asset, 0.01)

    if dist_to_support < dist_to_resistance:
        direction = "BUY"
        sl = current_price - max(min_sl_dist, min(dist_to_support * 1.1, atr))
        risk = current_price - sl
        tp_technical = min(resistance, current_price + (2.0 * atr))
        reward_technical = tp_technical - current_price
        tp = tp_technical if reward_technical >= 1.5 * risk else current_price + (1.5 * risk)
    else:
        direction = "SELL"
        sl = current_price + max(min_sl_dist, min(dist_to_resistance * 1.1, atr))
        risk = sl - current_price
        tp_technical = max(support, current_price - (2.0 * atr))
        reward_technical = current_price - tp_technical
        tp = tp_technical if reward_technical >= 1.5 * risk else current_price - (1.5 * risk)

    reward = abs(tp - current_price)
    rr = round(reward / risk, 2) if risk > 0 else 0

    def fmt(p):
        if asset in ["BTC", "ETH", "SPX", "Gold"]:
            return round(p, 0)
        return round(p, 2)

    return {
        "direction": direction,
        "tp": fmt(tp),
        "sl": fmt(sl),
        "rr": rr,
        "atr": round(atr, 2),
        "support": fmt(support),
        "resistance": fmt(resistance),
    }


def fetch_all_ohlc_levels(prices):
    cache_key = "all_levels"
    cached = get_cache(cache_key, max_age=3600)
    if cached:
        return cached

    all_levels = {}
    for asset, ticker in OHLC_TICKERS.items():
        raw_key = asset + "_RAW"
        current_price = prices.get(raw_key)
        if current_price is None:
            continue
        candles = fetch_ohlc(asset, ticker)
        if not candles:
            continue
        levels = calculate_levels(asset, current_price, candles)
        if levels:
            all_levels[asset] = levels

    update_cache(cache_key, all_levels)
    return all_levels


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
                prices["BTC"] = "🟠 BTC      $" + format(int(btc_price), ",") + "   " + btc_emoji + " " + sign + str(round(btc_change, 1)) + "%"
                prices["BTC_RAW"] = btc_price
                prices["BTC_CHANGE"] = btc_change

                eth_price = data["ethereum"]["usd"]
                eth_change = data["ethereum"]["usd_24h_change"]
                eth_emoji = "📈" if eth_change >= 0 else "📉"
                sign = "+" if eth_change >= 0 else ""
                prices["ETH"] = "🔵 ETH       $" + format(int(eth_price), ",") + "   " + eth_emoji + " " + sign + str(round(eth_change, 1)) + "%"
                prices["ETH_RAW"] = eth_price
                prices["ETH_CHANGE"] = eth_change
                update_cache("crypto_prices", {k: v for k, v in prices.items()})
            except Exception:
                prices["BTC"] = "🟠 BTC   N/A"
                prices["ETH"] = "🔵 ETH   N/A"
        else:
            prices["BTC"] = "🟠 BTC   N/A"
            prices["ETH"] = "🔵 ETH   N/A"

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
                    val = "🥇 Gold     $" + format(int(price), ",") + "   " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                elif label == "Silver":
                    val = "🥈 Silver   $" + str(round(price, 2)) + "   " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                elif label == "Oil":
                    val = "🛢 Oil      $" + str(round(price, 2)) + "   " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                elif label == "DXY":
                    val = "💵 DXY       " + str(round(price, 2)) + "   " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                else:
                    val = "📊 S&P 500    " + format(int(price), ",") + "   " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
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
            pub_date = item.findtext("pubDate", "").strip()
            if title and description and len(description) > 30:
                articles.append({
                    "category": category,
                    "title": title,
                    "description": description[:400],
                    "pub_date": pub_date,
                })
                count += 1
                if count >= 4:
                    break
    except Exception:
        pass
    return articles


def fetch_news():
    cached = get_cache("news", max_age=900)
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


def fetch_economic_calendar():
    cached = get_cache("economic_calendar", max_age=3600)
    if cached:
        return cached

    articles = []
    for url in ECONOMIC_CALENDAR_FEEDS:
        items = fetch_rss(url, "macro")
        articles.extend(items)

    update_cache("economic_calendar", articles)
    return articles


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


def build_ticker_block(prices, fear_greed_value, fear_greed_class):
    fg_str = str(fear_greed_value) + "   " + fear_greed_class if fear_greed_value else "N/A"
    lines = [
        prices.get("BTC", "🟠 BTC   N/A"),
        prices.get("ETH", "🔵 ETH   N/A"),
        prices.get("SPX", "📊 S&P 500   N/A"),
        prices.get("Gold", "🥇 Gold   N/A"),
        prices.get("Silver", "🥈 Silver   N/A"),
        prices.get("Oil", "🛢 Oil   N/A"),
        prices.get("DXY", "💵 DXY   N/A"),
        "😨 F&G      " + fg_str,
    ]
    return "\n".join(lines)


def build_signal_block(all_levels, prices):
    signal_emoji = {"BUY": "🟢", "SELL": "🔴"}
    lines = []

    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        lvl = all_levels.get(asset)
        raw_price = prices.get(asset + "_RAW")
        if not lvl or raw_price is None:
            continue

        direction = lvl["direction"]
        emoji = signal_emoji.get(direction, "🟡")
        asset_emoji = ASSET_EMOJI.get(asset, "")
        display = ASSET_DISPLAY.get(asset, asset)

        if asset in ["BTC", "ETH", "Gold"]:
            price_str = "$" + format(int(raw_price), ",")
            tp_str = "$" + format(int(lvl["tp"]), ",")
            sl_str = "$" + format(int(lvl["sl"]), ",")
        elif asset == "SPX":
            price_str = format(int(raw_price), ",")
            tp_str = format(int(lvl["tp"]), ",")
            sl_str = format(int(lvl["sl"]), ",")
        else:
            price_str = "$" + str(round(raw_price, 2))
            tp_str = "$" + str(round(float(lvl["tp"]), 2))
            sl_str = "$" + str(round(float(lvl["sl"]), 2))

        line = "{sig} {act}  {aem} {disp:<9} {price:<9}  TP {tp:<9}  SL {sl:<9}  R/R 1:{rr}".format(
            sig=emoji,
            act=direction,
            aem=asset_emoji,
            disp=display,
            price=price_str,
            tp=tp_str,
            sl=sl_str,
            rr=lvl["rr"],
        )
        lines.append(line)

    return "\n".join(lines)


def build_levels_context(all_levels):
    lines = []
    for asset, lvl in all_levels.items():
        lines.append(
            "{asset}: ATR={atr} | Support={support} | Resistance={resistance} | Direction={direction} | TP={tp} | SL={sl} | R/R={rr}".format(
                asset=asset,
                atr=lvl.get("atr", "?"),
                support=lvl.get("support", "?"),
                resistance=lvl.get("resistance", "?"),
                direction=lvl.get("direction", "?"),
                tp=lvl.get("tp", "?"),
                sl=lvl.get("sl", "?"),
                rr=lvl.get("rr", "?"),
            )
        )
    return "\n".join(lines)


def get_post_type(utc_hour):
    if utc_hour == 8:
        return "morning"
    elif utc_hour == 13:
        return "midday"
    elif utc_hour == 20:
        return "night"
    return "morning"


def format_with_claude(articles, prices, fear_greed_value, fear_greed_class, all_levels, post_type="morning", test=False):
    if not articles:
        return None

    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += str(i) + ". [" + article["category"].upper() + "] " + article["title"] + "\n"
        articles_text += "   " + article["description"] + "\n\n"

    today = datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")
    ticker_block = build_ticker_block(prices, fear_greed_value, fear_greed_class)
    signal_block = build_signal_block(all_levels, prices)
    levels_context = build_levels_context(all_levels)
    prefix = "TEST · " if test else ""

    calendar_text = ""
    if post_type == "night":
        calendar_articles = fetch_economic_calendar()
        if calendar_articles:
            calendar_text = "\nECONOMIC CALENDAR ARTICLES (for tomorrow's events section):\n"
            for i, a in enumerate(calendar_articles[:6], 1):
                calendar_text += str(i) + ". " + a["title"] + "\n"
                calendar_text += "   " + a["description"] + "\n\n"

    if post_type == "morning":
        timing_instruction = """POST TYPE: MORNING (08:00 UTC = 11:00 Bulgaria)
Focus: What is happening right now + what to watch and expect today.
Analysis style: Set up the day. What are the key levels to watch? What could move markets today?"""
        watch_instruction = """⚠️ WATCH TODAY · [ASSET_EMOJI] [ASSET] [PRICE]
→ Reclaim → [consequence] [emoji]
→ Reject → [consequence] [emoji]"""

    elif post_type == "midday":
        timing_instruction = """POST TYPE: MIDDAY (13:00 UTC = 16:00 Bulgaria)
Focus: How has the day evolved since morning + what to expect into the close.
Analysis style: Update on morning signals. What has changed? What is setting up for the close?"""
        watch_instruction = """⚠️ WATCH INTO CLOSE · [ASSET_EMOJI] [ASSET] [PRICE]
→ Holds → [consequence] [emoji]
→ Breaks → [consequence] [emoji]"""

    else:
        timing_instruction = """POST TYPE: NIGHT (20:00 UTC = 23:00 Bulgaria)
Focus: Day summary + what to expect tomorrow including scheduled economic events.
Analysis style: What happened today and why. What is the setup for tomorrow?"""
        watch_instruction = """⚠️ TOMORROW · Key Events
→ [🇺🇸/🇪🇺/🇨🇳] [Event] — [Time UTC] · [Expected impact] [emoji]
→ [🇺🇸/🇪🇺/🇨🇳] [Event] — [Time UTC] · [Expected impact] [emoji]
→ [ASSET_EMOJI] [ASSET] [PRICE] — [What to watch] [emoji]
Only use real scheduled events from economic calendar articles. If none found, use technical levels instead."""

    prompt = """You are a sharp market analyst writing for Raw Brief — a Telegram channel read by serious traders.
Write like the smartest trader you know. Every sentence earns its place or gets cut.

{timing_instruction}

CRITICAL RULES:
1. Only use facts explicitly stated in the news articles. Never invent events, numbers, or data.
2. If news is thin, use price action and DXY context only — no hallucinated facts.
3. Every paragraph: Line 1 = fact with specific number. Line 2 = why it matters. Line 3 = what happens next.
4. Sentiment (🔴🟢🟡) must be consistent with signal direction for that asset group.
5. DXY rising = bearish crypto/gold. DXY falling = bullish crypto/gold. Always factor in.
6. Do NOT modify the signal block. Copy it exactly as provided.
7. Do NOT modify the ticker block. Copy it exactly as provided.

TECHNICAL LEVELS (real 14-day OHLC + ATR data):
{levels_context}

SIGNAL BLOCK (copy exactly):
{signal_block}

Write the post in EXACTLY this format:

{prefix}Raw Brief · {today}
━━━━━━━━━━━━━━━━━━━━━━
{ticker_block}
━━━━━━━━━━━━━━━━━━━━━━
[SENTIMENT] CRYPTO — [Bearish/Bullish/Mixed]

🟠 [3 sentences about BTC: specific price fact → why it matters → what comes next]

🔵 [3 sentences about ETH: specific price fact → why it matters → what comes next]

━━━━━━━━━━━━━━━━━━━━━━
[SENTIMENT] STOCKS — [Bearish/Bullish/Mixed]

📊 [3 sentences about S&P: specific fact → why → what next]

[2 sentences about biggest stock news: what happened → what it means]

━━━━━━━━━━━━━━━━━━━━━━
[SENTIMENT] COMMODITIES — [Bearish/Bullish/Mixed]

🛢 [3 sentences about Oil: specific price fact → why → what next]

🥇 [3 sentences about Gold: specific price fact → why → what next]

━━━━━━━━━━━━━━━━━━━━━━
📡 SIGNALS
{signal_block}
━━━━━━━━━━━━━━━━━━━━━━
{watch_instruction}
━━━━━━━━━━━━━━━━━━━━━━
Not financial advice · DYOR 🌲

FORMAT RULES:
- Replace [SENTIMENT] with 🔴 or 🟢 or 🟡
- Sentiment must match signals for that asset group
- Plain sentences in analysis — no → arrows in paragraphs
- No source names, no URLs, no markdown
- English only
- Empty line between each paragraph block

News articles:
{articles_text}{calendar_text}

Write only the post. Nothing else.""".format(
        timing_instruction=timing_instruction,
        levels_context=levels_context,
        signal_block=signal_block,
        prefix=prefix,
        today=today,
        ticker_block=ticker_block,
        watch_instruction=watch_instruction,
        articles_text=articles_text,
        calendar_text=calendar_text,
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
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


def generate_flash_alert(asset, price, change_pct):
    now = datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")
    direction = "📈" if change_pct > 0 else "📉"
    change_str = "+" + str(round(change_pct, 1)) + "%" if change_pct > 0 else str(round(change_pct, 1)) + "%"

    templates = {
        "BTC": {
            "emoji": "🟠", "price_str": "$" + format(int(price), ","),
            "context_down": "Last 3 times BTC dropped this fast:\n→ Bottom formed within 4-6 hours\n→ Rebound of 8-12% followed within 48 hours",
            "context_up": "Last 3 times BTC spiked this fast:\n→ Momentum continued 24-48 hours\n→ Pullback of 5-8% before next leg up",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + " — holds here and bounces. Breaks and $" + str(int(price * 0.93)) + " is next.",
            "key_up": "Key Level: $" + str(int(price * 1.03)) + " — breaks above and momentum accelerates.",
        },
        "ETH": {
            "emoji": "🔵", "price_str": "$" + format(int(price), ","),
            "context_down": "ETH dropping faster than BTC. Last 4 times:\n→ ETH bottomed 2-3 hours before BTC\n→ Recovery was 15-20% within 72 hours",
            "context_up": "ETH outpacing BTC. Last 3 times:\n→ ETH ran another 20-30% within 2 weeks\n→ Altcoins followed with 2-3x moves",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + " — loses this and $" + str(int(price * 0.92)) + " comes fast.",
            "key_up": "Key Level: $" + str(int(price * 1.04)) + " — breaks above and ETH enters momentum phase.",
        },
        "Oil": {
            "emoji": "🛢", "price_str": "$" + str(round(price, 2)),
            "context_down": "Oil dropping fast signals demand destruction. Last 3 times:\n→ Further 8-12% decline followed\n→ Energy stocks underperformed 2-3 weeks",
            "context_up": "Oil spiking fast means supply shock. Last 3 times:\n→ Crude continued higher 2-3 weeks\n→ Energy stocks outperformed by 15-20%",
            "key_down": "Key Level: $" + str(round(price * 0.95, 2)) + " — breaks below and demand destruction kicks in.",
            "key_up": "Key Level: $" + str(round(price * 1.05, 2)) + " — breaks above and energy stocks explode.",
        },
        "Gold": {
            "emoji": "🥇", "price_str": "$" + format(int(price), ","),
            "context_down": "Gold dropping signals risk-on shift. Last 3 times:\n→ Gold stabilized within 24-48 hours\n→ Stocks rallied as fear eased",
            "context_up": "Gold spiking signals panic buying. Last 4 times:\n→ Gold ran another 8-12% within 2 weeks\n→ Stocks dropped 5-8%",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + " — breaks below and profit taking accelerates.",
            "key_up": "Key Level: $" + str(int(price * 1.03)) + " — breaks above and uncharted territory.",
        },
        "Silver": {
            "emoji": "🥈", "price_str": "$" + str(round(price, 2)),
            "context_down": "Silver dropping fast. Last 3 times:\n→ Gold followed lower within 48 hours\n→ Both metals stabilized after 5-8% drop",
            "context_up": "Silver outpacing gold signals industrial demand. Last 3 times:\n→ Gold followed 5-8% within 48 hours\n→ Both metals ran 2-3 weeks",
            "key_down": "Key Level: $" + str(round(price * 0.95, 2)) + " — loses this and selling accelerates.",
            "key_up": "Key Level: $" + str(round(price * 1.05, 2)) + " — breaks above and momentum phase.",
        },
        "SPX": {
            "emoji": "📊", "price_str": format(int(price), ","),
            "context_down": "Institutional selling detected. Last 3 times:\n→ Further 5-8% drop followed\n→ Recovery took 2-3 weeks minimum",
            "context_up": "Institutional buying detected. Last 3 times:\n→ Rally continued 1-2 weeks\n→ Tech and growth led higher",
            "key_down": "Key Level: " + str(int(price * 0.97)) + " — breaks below and panic selling accelerates.",
            "key_up": "Key Level: " + str(int(price * 1.03)) + " — breaks above and all-time high back in play.",
        },
    }

    if asset not in templates:
        return None

    t = templates[asset]
    context = t["context_up"] if change_pct > 0 else t["context_down"]
    key_level = t["key_up"] if change_pct > 0 else t["key_down"]

    post = "⚡ FLASH ALERT · " + now + "\n"
    post += "━━━━━━━━━━━━━━━━━━━━━━\n"
    post += t["emoji"] + " " + asset + " " + t["price_str"] + " " + direction + " (" + change_str + " in the last hour)\n"
    post += "━━━━━━━━━━━━━━━━━━━━━━\n"
    post += context + "\n\n"
    post += "💡 " + key_level + "\n"
    post += "━━━━━━━━━━━━━━━━━━━━━━\n"
    post += "Not financial advice · DYOR 🌲"

    return post


def generate_fear_greed_alert(value):
    now = datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")
    post = "⚡ FLASH ALERT · " + now + "\n"
    post += "━━━━━━━━━━━━━━━━━━━━━━\n"
    post += "😱 Fear & Greed: " + str(value) + " — EXTREME FEAR\n"
    post += "━━━━━━━━━━━━━━━━━━━━━━\n"
    post += "Last 5 times Fear & Greed hit this level:\n"
    post += "→ Markets bottomed within 48-72 hours\n"
    post += "→ BTC rebounded 20-30% within 30 days\n"
    post += "→ S&P 500 recovered 10-15% within 6 weeks\n\n"
    post += "Extreme fear = extreme opportunity. Historically.\n\n"
    post += "💡 Key Level: BTC $70,000 — line between recovery and capitulation.\n"
    post += "━━━━━━━━━━━━━━━━━━━━━━\n"
    post += "Not financial advice · DYOR 🌲"
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
                        save_previous_prices()
                        continue
                    last_flash_alert[asset] = time.time()

                alert_text = generate_flash_alert(asset, current_price, hourly_change)
                if alert_text:
                    send_to_telegram(alert_text)

        previous_prices[asset] = current_price

    save_previous_prices()

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


def generate_weekly_recap(prices, fear_greed_value, fear_greed_class):
    now = datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")
    articles = fetch_news()
    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += str(i) + ". [" + article["category"].upper() + "] " + article["title"] + "\n"
        articles_text += "   " + article["description"] + "\n\n"

    ticker_block = build_ticker_block(prices, fear_greed_value, fear_greed_class)

    prompt = "You are a sharp market analyst writing a WEEKLY RECAP for Raw Brief Telegram channel.\n"
    prompt += "CRITICAL: Only reference facts from the articles provided. No invented events.\n\n"
    prompt += "Weekly Recap · " + now + "\n━━━━━━━━━━━━━━━━━━━━━━\n"
    prompt += ticker_block + "\n"
    prompt += "━━━━━━━━━━━━━━━━━━━━━━\n"
    prompt += "THIS WEEK\n\n"
    prompt += "[3 sentences on biggest crypto move: what happened → why → what it means]\n\n"
    prompt += "[3 sentences on biggest stock move: what happened → why → what it means]\n\n"
    prompt += "[3 sentences on biggest commodity move: what happened → why → what it means]\n\n"
    prompt += "━━━━━━━━━━━━━━━━━━━━━━\n"
    prompt += "NEXT WEEK — Watch For\n\n"
    prompt += "→ [Key level or scheduled event with specific price]\n"
    prompt += "→ [Key level or scheduled event with specific price]\n"
    prompt += "→ [Key level or scheduled event with specific price]\n\n"
    prompt += "━━━━━━━━━━━━━━━━━━━━━━\n"
    prompt += "[🔴 Bearish / 🟢 Bullish / 🟡 Mixed] Overall Sentiment\n"
    prompt += "━━━━━━━━━━━━━━━━━━━━━━\n"
    prompt += "Not financial advice · DYOR 🌲\n\n"
    prompt += "RULES: Specific prices in every paragraph. English only. Plain sentences — no arrows in analysis.\n\n"
    prompt += "Articles:\n" + articles_text + "\nWrite only the recap. Nothing else."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _attempt_post(test=False, test_chat_id=None, post_type=None):
    check_env()
    verify_telegram_bot()
    prices = fetch_prices()
    fear_greed_value, fear_greed_class = fetch_fear_greed()
    articles = fetch_news()
    check_flash_alerts(prices, fear_greed_value)

    all_levels = fetch_all_ohlc_levels(prices)

    if not articles:
        return False

    if post_type is None:
        utc_hour = datetime.utcnow().hour
        post_type = get_post_type(utc_hour)

    post_text = format_with_claude(
        articles, prices, fear_greed_value, fear_greed_class,
        all_levels, post_type=post_type, test=test
    )
    if not post_text:
        return False

    if test and test_chat_id:
        return send_to_telegram(post_text, chat_id=test_chat_id)
    else:
        return send_to_telegram(post_text)


def run_bot(test=False, test_chat_id=None, post_type=None):
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            wait = min(RETRY_INTERVAL_SECONDS * (2 ** (attempt - 2)), 3600)
            time.sleep(wait)

        try:
            success = _attempt_post(test=test, test_chat_id=test_chat_id, post_type=post_type)
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
                    elif text.startswith("/morning"):
                        send_message_to_chat(chat_id, "Sending morning post... Please wait.")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id, "post_type": "morning"}, daemon=True).start()
                    elif text.startswith("/midday"):
                        send_message_to_chat(chat_id, "Sending midday post... Please wait.")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id, "post_type": "midday"}, daemon=True).start()
                    elif text.startswith("/night"):
                        send_message_to_chat(chat_id, "Sending night post... Please wait.")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id, "post_type": "night"}, daemon=True).start()
                    elif text.startswith("/test"):
                        send_message_to_chat(chat_id, "Sending test post... Please wait.")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id}, daemon=True).start()
                    elif text.startswith("/recap"):
                        send_message_to_chat(chat_id, "Generating weekly recap... Please wait.")
                        threading.Thread(target=run_weekly_recap, daemon=True).start()
                    elif text.startswith("/start"):
                        send_message_to_chat(chat_id, "Raw Brief Bot active.\n\nCommands:\n/post — Post to channel\n/test — Test post\n/morning — Test morning format\n/midday — Test midday format\n/night — Test night format\n/recap — Weekly recap")
        except Exception as e:
            time.sleep(10)


def start_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(lambda: run_bot(post_type="morning"), "cron", hour=8, minute=0)
    scheduler.add_job(lambda: run_bot(post_type="midday"), "cron", hour=13, minute=0)
    scheduler.add_job(lambda: run_bot(post_type="night"), "cron", hour=20, minute=0)

    scheduler.add_job(check_flash_alerts_job, "interval", minutes=60)
    scheduler.add_job(run_weekly_recap, "cron", day_of_week="sun", hour=20, minute=0)

    threading.Thread(target=handle_commands, daemon=True).start()
    threading.Thread(target=health_check_server, daemon=True).start()

    print("Raw Brief Bot v2 Active")
    print("Morning:  08:00 UTC — 11:00 Bulgaria")
    print("Midday:   13:00 UTC — 16:00 Bulgaria")
    print("Night:    20:00 UTC — 23:00 Bulgaria")
    print("Alerts:   every 60 minutes")
    print("Recap:    Sunday 20:00 UTC")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Stopped.")


if __name__ == "__main__":
    check_env()
    load_previous_prices()
    start_scheduler()
