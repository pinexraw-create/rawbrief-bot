#!/usr/bin/env python3
"""
Raw Brief Bot v3
- RSI, EMA 20/50, Volume confirmation for precise signals
- Three post formats: morning, midday, night
- Human voice analysis
- Signal format: one line per asset
- R/R rounded to integer
- ATR + S/R + RSI + EMA for TP/SL
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
from datetime import datetime

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
        + "?interval=1d&range=60d"
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
        volumes = indicators.get("volume", [])

        candles = []
        for i, (h, l, c) in enumerate(zip(highs, lows, closes)):
            if h is not None and l is not None and c is not None:
                vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
                candles.append({"high": h, "low": l, "close": c, "volume": vol})

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


def calculate_rsi(candles, period=14):
    closes = [c["close"] for c in candles]
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def calculate_ema(candles, period):
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)


def calculate_volume_trend(candles, period=10):
    if len(candles) < period * 2:
        return "neutral"
    recent_vol = sum(c["volume"] for c in candles[-period:]) / period
    prev_vol = sum(c["volume"] for c in candles[-period * 2:-period]) / period
    if prev_vol == 0:
        return "neutral"
    ratio = recent_vol / prev_vol
    if ratio > 1.2:
        return "increasing"
    elif ratio < 0.8:
        return "decreasing"
    return "neutral"


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
    rsi = calculate_rsi(candles)
    ema20 = calculate_ema(candles, 20)
    ema50 = calculate_ema(candles, 50)
    vol_trend = calculate_volume_trend(candles)

    if atr is None or support is None or resistance is None:
        return None

    dist_to_support = current_price - support
    dist_to_resistance = resistance - current_price
    min_sl_dist = current_price * MIN_SL_PCT.get(asset, 0.01)

    # Direction logic: combine S/R distance + RSI + EMA trend
    bullish_signals = 0
    bearish_signals = 0

    # S/R distance
    if dist_to_support < dist_to_resistance:
        bullish_signals += 1
    else:
        bearish_signals += 1

    # RSI
    if rsi is not None:
        if rsi < 40:
            bullish_signals += 1
        elif rsi > 60:
            bearish_signals += 1

    # EMA trend
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            bullish_signals += 1
        else:
            bearish_signals += 1

    # Volume confirmation
    if vol_trend == "increasing":
        # Volume confirms the dominant direction
        if bullish_signals > bearish_signals:
            bullish_signals += 1
        else:
            bearish_signals += 1

    direction = "BUY" if bullish_signals >= bearish_signals else "SELL"

    if direction == "BUY":
        sl = current_price - max(min_sl_dist, min(dist_to_support * 1.1, atr))
        risk = current_price - sl
        tp_technical = min(resistance, current_price + (2.0 * atr))
        reward_technical = tp_technical - current_price
        tp = tp_technical if reward_technical >= 1.5 * risk else current_price + (1.5 * risk)
    else:
        sl = current_price + max(min_sl_dist, min(dist_to_resistance * 1.1, atr))
        risk = sl - current_price
        tp_technical = max(support, current_price - (2.0 * atr))
        reward_technical = current_price - tp_technical
        tp = tp_technical if reward_technical >= 1.5 * risk else current_price - (1.5 * risk)

    reward = abs(tp - current_price)
    rr = round(reward / risk) if risk > 0 else 0

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
        "rsi": rsi,
        "ema20": ema20,
        "ema50": ema50,
        "volume_trend": vol_trend,
        "bullish_signals": bullish_signals,
        "bearish_signals": bearish_signals,
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


PERSISTENT_PRICES_FILE = "/tmp/last_known_prices.json"
MARKET_CLOSED_ASSETS = {"SPX", "Gold", "Silver", "Oil"}


def is_market_weekend():
    return datetime.utcnow().weekday() in {5, 6}


def load_persistent_prices():
    try:
        with open(PERSISTENT_PRICES_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_persistent_prices(prices):
    try:
        with open(PERSISTENT_PRICES_FILE, "w") as f:
            json.dump({k: v for k, v in prices.items()}, f)
    except Exception:
        pass


def fetch_binance_funding_rate():
    cached = get_cache("funding_rates", max_age=3600)
    if cached:
        return cached
    rates = {}
    for symbol in ["BTCUSDT", "ETHUSDT"]:
        try:
            r = fetch_with_retry(
                "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=" + symbol,
                timeout=10
            )
            if r:
                data = r.json()
                rate = float(data.get("lastFundingRate", 0)) * 100
                rates[symbol.replace("USDT", "")] = round(rate, 4)
        except Exception:
            pass
    update_cache("funding_rates", rates)
    return rates


def fetch_prices():
    prices = {}
    persistent = load_persistent_prices()
    weekend = is_market_weekend()

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
                prices["BTC"] = "🟠 BTC  $" + format(int(btc_price), ",") + "  " + btc_emoji + " " + sign + str(round(btc_change, 1)) + "%"
                prices["BTC_RAW"] = btc_price
                prices["BTC_CHANGE"] = btc_change
                eth_price = data["ethereum"]["usd"]
                eth_change = data["ethereum"]["usd_24h_change"]
                eth_emoji = "📈" if eth_change >= 0 else "📉"
                sign = "+" if eth_change >= 0 else ""
                prices["ETH"] = "🔵 ETH  $" + format(int(eth_price), ",") + "  " + eth_emoji + " " + sign + str(round(eth_change, 1)) + "%"
                prices["ETH_RAW"] = eth_price
                prices["ETH_CHANGE"] = eth_change
                update_cache("crypto_prices", {k: v for k, v in prices.items()})
            except Exception:
                if "BTC_RAW" in persistent:
                    prices["BTC"] = "🟠 BTC  $" + format(int(persistent["BTC_RAW"]), ",") + "  ➡️"
                    prices["BTC_RAW"] = persistent["BTC_RAW"]
                if "ETH_RAW" in persistent:
                    prices["ETH"] = "🔵 ETH  $" + format(int(persistent["ETH_RAW"]), ",") + "  ➡️"
                    prices["ETH_RAW"] = persistent["ETH_RAW"]
        else:
            if "BTC_RAW" in persistent:
                prices["BTC"] = "🟠 BTC  $" + format(int(persistent["BTC_RAW"]), ",") + "  ➡️"
                prices["BTC_RAW"] = persistent["BTC_RAW"]
            if "ETH_RAW" in persistent:
                prices["ETH"] = "🔵 ETH  $" + format(int(persistent["ETH_RAW"]), ",") + "  ➡️"
                prices["ETH_RAW"] = persistent["ETH_RAW"]

    for ticker, label in [("%5EGSPC", "SPX"), ("GC%3DF", "Gold"), ("SI%3DF", "Silver"), ("CL%3DF", "Oil"), ("DX-Y.NYB", "DXY")]:
        cached = get_cache(label + "_price", max_age=300)
        if cached:
            prices[label] = cached
            if label + "_RAW" in persistent:
                prices[label + "_RAW"] = persistent[label + "_RAW"]
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
                closed_tag = " 🔒" if (weekend and label in MARKET_CLOSED_ASSETS) else ""
                if label == "Gold":
                    val = "🥇 Gold  $" + format(int(price), ",") + "  " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%" + closed_tag
                elif label == "Silver":
                    val = "🥈 Silver  $" + "{:.2f}".format(price) + "  " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%" + closed_tag
                elif label == "Oil":
                    val = "🛢 Oil  $" + "{:.2f}".format(price) + "  " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%" + closed_tag
                elif label == "DXY":
                    val = "💵 DXY  " + str(round(price, 2)) + "  " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%"
                else:
                    val = "📊 S&P 500  " + format(int(price), ",") + "  " + trend_emoji + " " + sign + str(round(change_pct, 1)) + "%" + closed_tag
                prices[label] = val
                prices[label + "_RAW"] = price
                prices[label + "_CHANGE"] = change_pct
                update_cache(label + "_price", val)
            except Exception:
                if label + "_RAW" in persistent:
                    prices[label + "_RAW"] = persistent[label + "_RAW"]
                    prices[label] = persistent.get(label, "N/A")
                else:
                    prices[label] = "N/A"
        else:
            if label + "_RAW" in persistent:
                prices[label + "_RAW"] = persistent[label + "_RAW"]
                prices[label] = persistent.get(label, "N/A")
            else:
                prices[label] = "N/A"

    save_persistent_prices(prices)
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


def fetch_news(max_age=900):
    cached = get_cache("news", max_age=max_age)
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


# Store price snapshots per post type for comparison
PRICE_SNAPSHOTS_FILE = "/tmp/price_snapshots.json"

def load_price_snapshots():
    try:
        with open(PRICE_SNAPSHOTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_price_snapshot(post_type, prices):
    snapshots = load_price_snapshots()
    snapshot = {}
    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil", "DXY"]:
        raw = prices.get(asset + "_RAW")
        if raw is not None:
            snapshot[asset] = raw
    snapshot["timestamp"] = datetime.utcnow().isoformat()
    snapshots[post_type] = snapshot
    try:
        with open(PRICE_SNAPSHOTS_FILE, "w") as f:
            json.dump(snapshots, f)
    except Exception:
        pass


# Predictions tracking for accuracy verification
PREDICTIONS_FILE = "/tmp/predictions.json"

def load_predictions():
    try:
        with open(PREDICTIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"daily": [], "history": []}


def save_predictions(data):
    try:
        with open(PREDICTIONS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def save_morning_predictions(all_levels, prices):
    """Save morning predictions for evening verification."""
    data = load_predictions()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    predictions = {"date": today, "predictions": []}
    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        lvl = all_levels.get(asset)
        raw_price = prices.get(asset + "_RAW")
        if not lvl or raw_price is None:
            continue
        predictions["predictions"].append({
            "asset": asset,
            "entry": raw_price,
            "direction": lvl["direction"],
            "tp": float(lvl["tp"]),
            "sl": float(lvl["sl"]),
        })

    data["daily"] = [predictions]
    save_predictions(data)


def verify_predictions(current_prices):
    """Check if morning predictions hit TP, SL, or neither. Return results."""
    data = load_predictions()
    if not data.get("daily"):
        return []

    daily = data["daily"][0]
    results = []

    for pred in daily.get("predictions", []):
        asset = pred["asset"]
        current = current_prices.get(asset + "_RAW")
        if current is None:
            continue

        entry = pred["entry"]
        direction = pred["direction"]
        tp = pred["tp"]
        sl = pred["sl"]

        if direction == "BUY":
            if current >= tp:
                result = "hit_tp"
            elif current <= sl:
                result = "hit_sl"
            elif current > entry:
                result = "in_profit"
            else:
                result = "in_loss"
        else:
            if current <= tp:
                result = "hit_tp"
            elif current >= sl:
                result = "hit_sl"
            elif current < entry:
                result = "in_profit"
            else:
                result = "in_loss"

        pct_move = ((current - entry) / entry) * 100
        if direction == "SELL":
            pct_move = -pct_move

        results.append({
            "asset": asset,
            "direction": direction,
            "entry": entry,
            "current": current,
            "tp": tp,
            "sl": sl,
            "result": result,
            "pct_move": round(pct_move, 2),
        })

    return results


def archive_daily_predictions(verified_results):
    """Archive today's verified predictions to history for weekly recap."""
    data = load_predictions()
    if not data.get("daily"):
        return

    today = datetime.utcnow().strftime("%Y-%m-%d")
    archive_entry = {
        "date": today,
        "results": verified_results,
    }
    if "history" not in data:
        data["history"] = []
    data["history"].append(archive_entry)
    # Keep only last 14 days
    data["history"] = data["history"][-14:]
    save_predictions(data)


def build_verification_block(verified_results):
    """Format verification results for the night post."""
    if not verified_results:
        return ""

    asset_emoji = {"BTC": "🟠", "ETH": "🔵", "SPX": "📊", "Gold": "🥇", "Silver": "🥈", "Oil": "🛢"}
    lines = []

    for r in verified_results:
        asset = r["asset"]
        emoji = asset_emoji.get(asset, "")
        direction = r["direction"]
        result = r["result"]
        pct = r["pct_move"]
        sign = "+" if pct >= 0 else ""

        if result == "hit_tp":
            status = "✅ hit TP"
        elif result == "hit_sl":
            status = "❌ hit SL"
        elif result == "in_profit":
            status = "🟡 still open, " + sign + str(pct) + "% in profit"
        else:
            status = "🟡 still open, " + sign + str(pct) + "% in loss"

        lines.append("{emoji} {asset} {dir} — {status}".format(
            emoji=emoji, asset=asset, dir=direction, status=status
        ))

    return "\n".join(lines)


def calculate_weekly_accuracy():
    """Calculate weekly accuracy stats from history."""
    data = load_predictions()
    history = data.get("history", [])
    if not history:
        return None

    # Last 7 days
    week = history[-7:]
    total = 0
    wins = 0
    losses = 0
    open_trades = 0

    asset_stats = {}

    for day in week:
        for r in day.get("results", []):
            total += 1
            asset = r["asset"]
            if asset not in asset_stats:
                asset_stats[asset] = {"wins": 0, "losses": 0, "open": 0}

            if r["result"] == "hit_tp":
                wins += 1
                asset_stats[asset]["wins"] += 1
            elif r["result"] == "hit_sl":
                losses += 1
                asset_stats[asset]["losses"] += 1
            else:
                open_trades += 1
                asset_stats[asset]["open"] += 1

    closed = wins + losses
    accuracy = round((wins / closed) * 100) if closed > 0 else 0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "open": open_trades,
        "accuracy": accuracy,
        "by_asset": asset_stats,
    }

def build_price_movement_context(prices, post_type):
    """Build context showing price movement since last post."""
    snapshots = load_price_snapshots()
    lines = []

    if post_type == "midday":
        prev = snapshots.get("morning", {})
        label = "since morning open"
    elif post_type == "night":
        prev = snapshots.get("midday", snapshots.get("morning", {}))
        label = "since midday"
    else:
        return ""

    if not prev:
        return ""

    asset_display = {
        "BTC": "BTC", "ETH": "ETH", "SPX": "S&P 500",
        "Gold": "Gold", "Silver": "Silver", "Oil": "Oil", "DXY": "DXY"
    }

    for asset, display in asset_display.items():
        current = prices.get(asset + "_RAW")
        previous = prev.get(asset)
        if current and previous and previous != 0:
            pct = ((current - previous) / previous) * 100
            sign = "+" if pct >= 0 else ""
            direction = "📈" if pct >= 0.05 else ("📉" if pct <= -0.05 else "➡️")
            lines.append("{disp}: {dir} {sign}{pct}% {label}".format(
                disp=display,
                dir=direction,
                sign=sign,
                pct=round(pct, 1),
                label=label,
            ))

    if lines:
        return "\nPRICE MOVEMENT CONTEXT (" + label + "):\n" + "\n".join(lines) + "\n"
    return ""


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
    fg_str = str(fear_greed_value) + "  " + fear_greed_class if fear_greed_value else "N/A"
    lines = [
        prices.get("BTC", "🟠 BTC  N/A"),
        prices.get("ETH", "🔵 ETH  N/A"),
        prices.get("SPX", "📊 S&P 500  N/A"),
        prices.get("Gold", "🥇 Gold  N/A"),
        prices.get("Silver", "🥈 Silver  N/A"),
        prices.get("Oil", "🛢 Oil  N/A"),
        prices.get("DXY", "💵 DXY  N/A"),
        "😨 F&G  " + fg_str,
    ]
    return "\n".join(lines)


def build_signal_block(all_levels, prices):
    signal_emoji = {"BUY": "🟢", "SELL": "🔴"}
    lines = []

    asset_display = {
        "BTC": "BTC",
        "ETH": "ETH",
        "SPX": "S&P 500",
        "Gold": "Gold",
        "Silver": "Silver",
        "Oil": "Oil",
    }

    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        lvl = all_levels.get(asset)
        raw_price = prices.get(asset + "_RAW")
        if not lvl or raw_price is None:
            continue

        direction = lvl["direction"]
        sig = signal_emoji.get(direction, "🟡")
        display = asset_display.get(asset, asset)

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

        lines.append(
            "{sig} {act} {disp} {price} | TP {tp} | SL {sl} | R/R 1:{rr}".format(
                sig=sig,
                act=direction,
                disp=display,
                price=price_str,
                tp=tp_str,
                sl=sl_str,
                rr=lvl["rr"],
            )
        )

    return "\n".join(lines)


def build_levels_context(all_levels):
    lines = []
    for asset, lvl in all_levels.items():
        lines.append(
            "{asset}: ATR={atr} | Support={support} | Resistance={resistance} | RSI={rsi} | EMA20={ema20} | EMA50={ema50} | Volume={vol} | Direction={direction} | TP={tp} | SL={sl} | R/R=1:{rr}".format(
                asset=asset,
                atr=lvl.get("atr", "?"),
                support=lvl.get("support", "?"),
                resistance=lvl.get("resistance", "?"),
                rsi=lvl.get("rsi", "?"),
                ema20=lvl.get("ema20", "?"),
                ema50=lvl.get("ema50", "?"),
                vol=lvl.get("volume_trend", "?"),
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


def format_with_claude(articles, prices, fear_greed_value, fear_greed_class, all_levels, post_type="morning", test=False, verified_results=None):
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
    movement_context = build_price_movement_context(prices, post_type)

    # Funding rate context
    funding_rates = fetch_binance_funding_rate()
    funding_text = ""
    if funding_rates:
        parts = []
        for asset, rate in funding_rates.items():
            sentiment = "overleveraged longs" if rate > 0.01 else ("shorts getting squeezed" if rate < -0.01 else "neutral")
            parts.append("{asset} funding: {rate}% ({sentiment})".format(asset=asset, rate=rate, sentiment=sentiment))
        if parts:
            funding_text = "\nFUNDING RATES: " + " | ".join(parts) + "\n"

    calendar_text = ""
    verification_text = ""
    if post_type == "night":
        calendar_articles = fetch_economic_calendar()
        if calendar_articles:
            calendar_text = "\nECONOMIC CALENDAR ARTICLES:\n"
            for i, a in enumerate(calendar_articles[:6], 1):
                calendar_text += str(i) + ". " + a["title"] + "\n"
                calendar_text += "   " + a["description"] + "\n\n"

        if verified_results:
            verification_text = "\nMORNING PREDICTIONS VERIFICATION (actual results today):\n"
            verification_text += build_verification_block(verified_results)
            verification_text += "\n"

    if post_type == "morning":
        timing_instruction = """MORNING POST (11:00 Bulgaria): Set up the day.
- Focus on what is happening RIGHT NOW as markets open
- What are the key levels and events to watch today?
- Forward looking — what could move markets today?"""
        watch_label = "⚠️ WATCH TODAY"
        watch_detail = "Most critical level to watch today. Forward looking."
    elif post_type == "midday":
        timing_instruction = """MIDDAY POST (16:00 Bulgaria): Midday update.
- How has the day evolved since morning? Use PRICE MOVEMENT CONTEXT for actual moves.
- What has CHANGED since morning open?
- What is setting up for the close?"""
        watch_label = "⚠️ WATCH INTO CLOSE"
        watch_detail = "Most critical level into today's close. Reference morning levels if relevant."
    else:
        timing_instruction = """NIGHT POST (23:00 Bulgaria): Day wrap-up + tomorrow setup.
- Summarize what actually happened today using PRICE MOVEMENT CONTEXT
- What were the biggest moves and why?
- What should traders watch tomorrow? Include scheduled economic events."""
        watch_label = "⚠️ TOMORROW"
        watch_detail = "Key events and levels for tomorrow. Use real scheduled events from economic calendar if available."

    prompt = """You are writing for Raw Brief — a Telegram market analysis channel.

VOICE: You're a sharp trader texting a friend who trusts your analysis. Casual but confident. Real talk. Short sentences. Sometimes one word. Never corporate, never robotic. Examples of good tone: "BTC is stuck. $78K is the wall — been there three times." / "Gold doing what Gold does — everyone's scared, it goes up." / "Oil dropped 1.5% but nobody's panicking yet. Watch the $90 level." Admit uncertainty when it's there: "Honestly, the market is confused today." Use "we" sometimes: "We're watching $78K." Add personality: "The Fed is being the Fed again."

{timing_instruction}

CRITICAL RULES:
1. Only use facts from news articles. Never invent data or events.
2. Use PRICE MOVEMENT CONTEXT to make midday/night posts different from morning.
3. Morning = forward looking. Midday = what changed + what's next. Night = day summary + tomorrow.
4. Sentiment (🔴🟢🟡) must match signal direction for that group.
5. DXY rising = bearish crypto/gold. Always factor this in.
6. If funding rate data is available — negative funding = potential BTC squeeze up. Positive = overleveraged longs.
7. Copy signal block and ticker block EXACTLY. Do not change numbers.

TECHNICAL DATA:
{levels_context}
{movement_context}{funding_text}{verification_text}
SIGNAL BLOCK (copy exactly):
{signal_block}

Write EXACTLY in this format — no empty lines between bullets:

{prefix}Raw Brief · {today}
━━━━━━━━━━
{ticker_block}
━━━━━━━━━━
[🔴/🟢/🟡] CRYPTO—[Bearish/Bullish/Mixed]
🟠 [BTC: one fact with price—one consequence. Max 15 words.]
🔵 [ETH: one fact with price—one consequence. Max 15 words.]
━━━━━━━━━━
[🔴/🟢/🟡] STOCKS—[Bearish/Bullish/Mixed]
📊 [S&P: one fact with number—one consequence. Max 15 words.]
[Biggest stock news: what happened—what it means. Max 15 words.]
━━━━━━━━━━
[🔴/🟢/🟡] COMMODITIES—[Bearish/Bullish/Mixed]
🛢 [Oil: one fact with price—one consequence. Max 15 words.]
🥇 [Gold: one fact with price—one consequence. Max 15 words.]
━━━━━━━━━━
{results_section}📡 SIGNALS
{signal_block}
━━━━━━━━━━
{watch_label}[ASSET EMOJI] [ASSET] [PRICE]
→[If A happens]→[consequence] [emoji]
→[If B happens]→[consequence] [emoji]
━━━━━━━━━━
Not financial advice · DYOR 🌲

STYLE: No empty lines between bullets. Use — to connect ideas. Max 15 words per bullet. No jargon. English only.

NIGHT POST ONLY: If MORNING PREDICTIONS VERIFICATION is provided, include a section BEFORE the SIGNALS block formatted exactly like this:
📋 TODAY'S RESULTS
[Copy the verification lines as-is from the data. For each line, add a brief one-liner explanation if the prediction failed — e.g. "Fed hawkish stance reversed direction". If there are no failed predictions, just show the results clean.]
━━━━━━━━━━

{watch_detail}

News articles:
{articles_text}{calendar_text}

Write only the post. Nothing else.""".format(
        timing_instruction=timing_instruction,
        levels_context=levels_context,
        movement_context=movement_context,
        verification_text=verification_text,
        results_section="📋 TODAY'S RESULTS\n[insert results here]\n━━━━━━━━━━\n" if verified_results else "",
        signal_block=signal_block,
        prefix=prefix,
        today=today,
        ticker_block=ticker_block,
        watch_label=watch_label,
        watch_detail=watch_detail,
        articles_text=articles_text,
        calendar_text=calendar_text,
        funding_text=funding_text,
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
            "emoji": "🟠",
            "price_str": "$" + format(int(price), ","),
            "context_down": "Last 3 times BTC dropped this fast:\n→ Bottom formed within 4-6 hours\n→ Rebound of 8-12% followed within 48 hours",
            "context_up": "Last 3 times BTC spiked this fast:\n→ Momentum continued 24-48 hours\n→ Pullback of 5-8% before next leg up",
            "key_down": "Watch $" + str(int(price * 0.97)) + " — holds here or next stop is $" + str(int(price * 0.93)) + ".",
            "key_up": "Watch $" + str(int(price * 1.03)) + " — breaks above and momentum accelerates.",
        },
        "ETH": {
            "emoji": "🔵",
            "price_str": "$" + format(int(price), ","),
            "context_down": "ETH dropping faster than BTC. Last 4 times:\n→ ETH bottomed 2-3 hours before BTC\n→ Recovery was 15-20% within 72 hours",
            "context_up": "ETH outpacing BTC. Last 3 times:\n→ ETH ran another 20-30% within 2 weeks\n→ Altcoins followed with 2-3x moves",
            "key_down": "Watch $" + str(int(price * 0.97)) + " — loses this and $" + str(int(price * 0.92)) + " comes fast.",
            "key_up": "Watch $" + str(int(price * 1.04)) + " — breaks above and ETH enters momentum phase.",
        },
        "Oil": {
            "emoji": "🛢",
            "price_str": "$" + str(round(price, 2)),
            "context_down": "Oil dropping fast signals demand destruction. Last 3 times:\n→ Further 8-12% decline followed\n→ Energy stocks underperformed 2-3 weeks",
            "context_up": "Oil spiking fast means supply shock. Last 3 times:\n→ Crude continued higher 2-3 weeks\n→ Energy stocks outperformed by 15-20%",
            "key_down": "Watch $" + str(round(price * 0.95, 2)) + " — breaks below and demand destruction kicks in.",
            "key_up": "Watch $" + str(round(price * 1.05, 2)) + " — breaks above and energy stocks follow.",
        },
        "Gold": {
            "emoji": "🥇",
            "price_str": "$" + format(int(price), ","),
            "context_down": "Gold dropping signals risk-on shift. Last 3 times:\n→ Gold stabilized within 24-48 hours\n→ Stocks rallied as fear eased",
            "context_up": "Gold spiking signals panic buying. Last 4 times:\n→ Gold ran another 8-12% within 2 weeks\n→ Stocks dropped 5-8%",
            "key_down": "Watch $" + str(int(price * 0.97)) + " — breaks below and profit taking accelerates.",
            "key_up": "Watch $" + str(int(price * 1.03)) + " — breaks above and uncharted territory.",
        },
        "Silver": {
            "emoji": "🥈",
            "price_str": "$" + str(round(price, 2)),
            "context_down": "Silver dropping fast. Last 3 times:\n→ Gold followed lower within 48 hours\n→ Both metals stabilized after 5-8% drop",
            "context_up": "Silver outpacing gold — industrial demand signal. Last 3 times:\n→ Gold followed 5-8% within 48 hours\n→ Both metals ran 2-3 weeks",
            "key_down": "Watch $" + str(round(price * 0.95, 2)) + " — loses this and selling accelerates.",
            "key_up": "Watch $" + str(round(price * 1.05, 2)) + " — breaks above and momentum phase begins.",
        },
        "SPX": {
            "emoji": "📊",
            "price_str": format(int(price), ","),
            "context_down": "Institutional selling detected. Last 3 times:\n→ Further 5-8% drop followed\n→ Recovery took 2-3 weeks minimum",
            "context_up": "Institutional buying detected. Last 3 times:\n→ Rally continued 1-2 weeks\n→ Tech and growth led higher",
            "key_down": "Watch " + str(int(price * 0.97)) + " — breaks below and panic selling accelerates.",
            "key_up": "Watch " + str(int(price * 1.03)) + " — breaks above and all-time high back in play.",
        },
    }

    if asset not in templates:
        return None

    t = templates[asset]
    context = t["context_up"] if change_pct > 0 else t["context_down"]
    key_level = t["key_up"] if change_pct > 0 else t["key_down"]

    post = "⚡ FLASH ALERT · " + now + "\n"
    post += "━━━━━━━━━━━━━━\n"
    post += t["emoji"] + " " + asset + " " + t["price_str"] + " " + direction + " (" + change_str + " in the last hour)\n"
    post += "━━━━━━━━━━━━━━\n"
    post += context + "\n\n"
    post += "💡 " + key_level + "\n"
    post += "━━━━━━━━━━━━━━\n"
    post += "Not financial advice · DYOR 🌲"

    return post


def generate_fear_greed_alert(value):
    now = datetime.utcnow().strftime("%B %d, %Y · %H:%M UTC")
    post = "⚡ FLASH ALERT · " + now + "\n"
    post += "━━━━━━━━━━━━━━\n"
    post += "😱 Fear & Greed: " + str(value) + " — EXTREME FEAR\n"
    post += "━━━━━━━━━━━━━━\n"
    post += "Last 5 times it hit this level:\n"
    post += "→ Markets bottomed within 48-72 hours\n"
    post += "→ BTC rebounded 20-30% within 30 days\n"
    post += "→ S&P 500 recovered 10-15% within 6 weeks\n\n"
    post += "Extreme fear = extreme opportunity. Historically.\n\n"
    post += "💡 Watch BTC $70,000 — that's the line between recovery and capitulation.\n"
    post += "━━━━━━━━━━━━━━\n"
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

    accuracy_stats = calculate_weekly_accuracy()
    accuracy_section = ""
    if accuracy_stats and accuracy_stats["total"] > 0:
        asset_emoji = {"BTC": "🟠", "ETH": "🔵", "SPX": "📊", "Gold": "🥇", "Silver": "🥈", "Oil": "🛢"}
        lines = []
        for asset, stats in accuracy_stats["by_asset"].items():
            emoji = asset_emoji.get(asset, "")
            closed = stats["wins"] + stats["losses"]
            if closed > 0:
                acc = round((stats["wins"] / closed) * 100)
                lines.append("{emoji} {asset}: {wins}/{closed} ({acc}%)".format(
                    emoji=emoji, asset=asset,
                    wins=stats["wins"], closed=closed, acc=acc
                ))
        accuracy_lines = "\n".join(lines)
        accuracy_section = """━━━━━━━━━━
📊 ACCURACY THIS WEEK
Overall: {acc}% ({wins}/{closed} closed trades)
{asset_lines}
━━━━━━━━━━
""".format(
            acc=accuracy_stats["accuracy"],
            wins=accuracy_stats["wins"],
            closed=accuracy_stats["wins"] + accuracy_stats["losses"],
            asset_lines=accuracy_lines,
        )

    prompt = """You are writing a WEEKLY RECAP for Raw Brief Telegram channel.
VOICE: Sharp, experienced trader talking to a friend. Human. Direct. Not robotic.
CRITICAL: Only use facts from the articles. Never invent data.

Write EXACTLY in this format — no empty lines between bullets:

Weekly Recap · {now}
━━━━━━━━━━
{ticker_block}
━━━━━━━━━━
📅 THIS WEEK
🟠 [BTC: biggest move with number—what it meant. Max 15 words.]
🔵 [ETH: biggest move with number—what it meant. Max 15 words.]
📊 [S&P: biggest move with number—what it meant. Max 15 words.]
🥇 [Gold: biggest move with number—what it meant. Max 15 words.]
🛢 [Oil: biggest move with number—what it meant. Max 15 words.]
{accuracy_section}━━━━━━━━━━
🔮 NEXT WEEK — Watch For
→[Key level or scheduled event with specific price] [emoji]
→[Key level or scheduled event with specific price] [emoji]
→[Key level or scheduled event with specific price] [emoji]
━━━━━━━━━━
[🔴 Bearish / 🟢 Bullish / 🟡 Mixed] Overall Sentiment
━━━━━━━━━━
Not financial advice · DYOR 🌲

STYLE: No empty lines between bullets. Specific prices always. English only. Max 15 words per line.

Articles:
{articles_text}

Write only the recap. Nothing else. Copy the ACCURACY section EXACTLY as given if provided.""".format(
        now=now,
        ticker_block=ticker_block,
        accuracy_section=accuracy_section,
        articles_text=articles_text,
    )

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
    news_max_age = 900 if post_type == "morning" else 300
    articles = fetch_news(max_age=news_max_age)
    check_flash_alerts(prices, fear_greed_value)
    all_levels = fetch_all_ohlc_levels(prices)

    if not articles:
        return False

    if post_type is None:
        post_type = get_post_type(datetime.utcnow().hour)

    if post_type == "morning" and not test:
        save_morning_predictions(all_levels, prices)

    verified_results = None
    if post_type == "night":
        verified_results = verify_predictions(prices)

    post_text = format_with_claude(
        articles, prices, fear_greed_value, fear_greed_class,
        all_levels, post_type=post_type, test=test,
        verified_results=verified_results
    )
    if not post_text:
        return False

    if post_type == "night" and verified_results and not test:
        archive_daily_predictions(verified_results)

    save_price_snapshot(post_type, prices)

    if test and test_chat_id:
        return send_to_telegram(post_text, chat_id=test_chat_id)
    return send_to_telegram(post_text)


def run_bot(test=False, test_chat_id=None, post_type=None):
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            wait = min(RETRY_INTERVAL_SECONDS * (2 ** (attempt - 2)), 3600)
            time.sleep(wait)
        try:
            success = _attempt_post(test=test, test_chat_id=test_chat_id, post_type=post_type)
        except Exception as e:
            if "credit balance is too low" in str(e):
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
                        send_message_to_chat(chat_id, "Posting now... Please wait.")
                        threading.Thread(target=run_bot, daemon=True).start()
                    elif text.startswith("/morning"):
                        send_message_to_chat(chat_id, "Morning post incoming...")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id, "post_type": "morning"}, daemon=True).start()
                    elif text.startswith("/midday"):
                        send_message_to_chat(chat_id, "Midday post incoming...")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id, "post_type": "midday"}, daemon=True).start()
                    elif text.startswith("/night"):
                        send_message_to_chat(chat_id, "Night post incoming...")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id, "post_type": "night"}, daemon=True).start()
                    elif text.startswith("/test"):
                        send_message_to_chat(chat_id, "Test post incoming...")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id}, daemon=True).start()
                    elif text.startswith("/recap"):
                        send_message_to_chat(chat_id, "Weekly recap incoming...")
                        threading.Thread(target=run_weekly_recap, daemon=True).start()
                    elif text.startswith("/start"):
                        send_message_to_chat(chat_id, "Raw Brief Bot v3 active.\n\n/post — Post to channel\n/test — Test post\n/morning — Morning format\n/midday — Midday format\n/night — Night format\n/recap — Weekly recap")
        except Exception:
            time.sleep(10)


def start_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(lambda: run_bot(post_type="morning"), "cron", hour=8, minute=0)
    scheduler.add_job(lambda: run_bot(post_type="midday"), "cron", hour=13, minute=0)
    scheduler.add_job(lambda: run_bot(post_type="night"), "cron", hour=20, minute=0)
    scheduler.add_job(check_flash_alerts_job, "interval", minutes=60)
    scheduler.add_job(run_weekly_recap, "cron", day_of_week="sun", hour=22, minute=0)

    threading.Thread(target=handle_commands, daemon=True).start()
    threading.Thread(target=health_check_server, daemon=True).start()

    print("Raw Brief Bot v3 Active")
    print("Morning:  08:00 UTC — 11:00 Bulgaria")
    print("Midday:   13:00 UTC — 16:00 Bulgaria")
    print("Night:    20:00 UTC — 23:00 Bulgaria")
    print("Alerts:   every 60 minutes")
    print("Recap:    Sunday 20:00 UTC")
    print("Commands: /morning /midday /night /test /post /recap")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Stopped.")


if __name__ == "__main__":
    check_env()
    load_previous_prices()
    start_scheduler()
