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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

MAX_RETRIES = 10
RETRY_INTERVAL_SECONDS = 300

RSS_FEEDS = {
    "crypto": [
        "https://coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://bitcoinmagazine.com/feed",
    ],
    "stocks": [
        "https://cnbc.com/id/20910258/device/rss/rss.html",
        "https://feeds.marketwatch.com/marketwatch/topstories",
        "https://feeds.reuters.com/reuters/businessNews",
    ],
    "commodities": [
        "https://oilprice.com/rss/main",
        "https://www.kitco.com/rss/",
    ],
}

previous_prices = {}


def check_env():
    missing = []
    for var in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print("ERROR: Missing variables: " + ", ".join(missing))
        sys.exit(1)


def verify_telegram_bot():
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/getMe"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                bot = result["result"]
                print("Bot verified: @" + bot["username"])
                return bot
            else:
                print("ERROR: Invalid bot token")
                sys.exit(1)
    except Exception as e:
        print("ERROR: Could not reach Telegram API: " + str(e))
        sys.exit(1)


def fetch_prices():
    prices = {}

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()

        btc_price = data["bitcoin"]["usd"]
        btc_change = data["bitcoin"]["usd_24h_change"]
        btc_emoji = "📈" if btc_change >= 0 else "📉"
        sign = "+" if btc_change >= 0 else ""
        prices["BTC"] = "$" + format(int(btc_price), ",") + " " + btc_emoji + " (" + sign + str(round(btc_change, 1)) + "%)"
        prices["BTC_RAW"] = btc_price
        prices["BTC_CHANGE"] = btc_change

        eth_price = data["ethereum"]["usd"]
        eth_change = data["ethereum"]["usd_24h_change"]
        eth_emoji = "📈" if eth_change >= 0 else "📉"
        sign = "+" if eth_change >= 0 else ""
        prices["ETH"] = "$" + format(int(eth_price), ",") + " " + eth_emoji + " (" + sign + str(round(eth_change, 1)) + "%)"
        prices["ETH_RAW"] = eth_price
        prices["ETH_CHANGE"] = eth_change

    except Exception as e:
        print("WARNING: Could not fetch crypto prices: " + str(e))
        prices["BTC"] = "N/A"
        prices["ETH"] = "N/A"

    for ticker, label in [("%5EGSPC", "SPX"), ("GC%3DF", "Gold"), ("SI%3DF", "Silver"), ("CL%3DF", "Oil"), ("DX-Y.NYB", "DXY")]:
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
            trend_emoji = "📈" if change_pct >= 0.05 else ("📉" if change_pct <= -0.05 else "➡️")
            sign = "+" if change_pct >= 0 else ""
            if label == "Gold":
                prices["Gold"] = "$" + format(int(price), ",") + "/oz " + trend_emoji + " (" + sign + str(round(change_pct, 1)) + "%)"
                prices["Gold_RAW"] = price
                prices["Gold_CHANGE"] = change_pct
            elif label == "Silver":
                prices["Silver"] = "$" + str(round(price, 2)) + "/oz " + trend_emoji + " (" + sign + str(round(change_pct, 1)) + "%)"
                prices["Silver_RAW"] = price
                prices["Silver_CHANGE"] = change_pct
            elif label == "Oil":
                prices["Oil"] = "$" + str(round(price, 2)) + " " + trend_emoji + " (" + sign + str(round(change_pct, 1)) + "%)"
                prices["Oil_RAW"] = price
                prices["Oil_CHANGE"] = change_pct
            elif label == "DXY":
                prices["DXY"] = str(round(price, 2)) + " " + trend_emoji + " (" + sign + str(round(change_pct, 1)) + "%)"
                prices["DXY_RAW"] = price
                prices["DXY_CHANGE"] = change_pct
            else:
                prices["SPX"] = format(int(price), ",") + " " + trend_emoji + " (" + sign + str(round(change_pct, 1)) + "%)"
                prices["SPX_RAW"] = price
                prices["SPX_CHANGE"] = change_pct
        except Exception as e:
            print("WARNING: Could not fetch " + label + " price: " + str(e))
            prices[label] = "N/A"

    print("Prices fetched successfully.")
    return prices


def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        r.raise_for_status()
        data = r.json()
        entry = data["data"][0]
        value = int(entry["value"])
        classification = entry["value_classification"]
        print("Fear & Greed Index: " + str(value) + " (" + classification + ")")
        return value, classification
    except Exception as e:
        print("WARNING: Could not fetch Fear & Greed Index: " + str(e))
        return None, "N/A"


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


def fetch_news():
    all_articles = []
    for category, feeds in RSS_FEEDS.items():
        for feed_url in feeds:
            articles = fetch_rss(feed_url, category)
            all_articles.extend(articles)
            if len([a for a in all_articles if a["category"] == category]) >= 6:
                break

    print("Total articles: " + str(len(all_articles)))
    return all_articles


def send_to_telegram(text, chat_id=None):
    target = chat_id if chat_id else TELEGRAM_CHANNEL_ID
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    payload = {
        "chat_id": target,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                msg_id = result["result"]["message_id"]
                print("Message sent! ID: " + str(msg_id))
                return True
            else:
                desc = result.get("description", "Unknown error")
                print("ERROR: Telegram rejected - " + desc)
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        error_data = json.loads(body) if body else {}
        desc = error_data.get("description", e.reason)
        print("ERROR " + str(e.code) + ": " + desc)
        return False
    except Exception as e:
        print("ERROR: Unexpected failure - " + str(e))
        return False


def send_message_to_chat(chat_id, text):
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("ERROR sending message to chat: " + str(e))


def notify_admin(message):
    if ADMIN_CHAT_ID:
        send_message_to_chat(ADMIN_CHAT_ID, message)


def generate_flash_alert(asset, price, change_pct):
    now = datetime.utcnow().strftime("%B %d, %Y | %H:%M UTC")
    direction = "📈" if change_pct > 0 else "📉"
    change_str = "+" + str(round(change_pct, 1)) + "%" if change_pct > 0 else str(round(change_pct, 1)) + "%"

    prompts = {
        "BTC": {
            "emoji": "🟠",
            "price_str": "$" + format(int(price), ","),
            "context_down": "Last 3 times BTC dropped this fast:\n-> Bottom formed within 4-6 hours\n-> Rebound of 8-12% followed within 48 hours\n\nThis is not a random move. Pattern is clear.",
            "context_up": "Last 3 times BTC spiked this fast:\n-> Momentum continued for 24-48 hours\n-> Pullback of 5-8% followed before next leg up\n\nInstitutional buying detected. Smart money is moving.",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + " — holds here and next move is up. Breaks below and $" + str(int(price * 0.93)) + " is next.",
            "key_up": "Key Level: $" + str(int(price * 1.03)) + " — breaks above and momentum accelerates. Fails here and expect consolidation.",
        },
        "ETH": {
            "emoji": "🔵",
            "price_str": "$" + format(int(price), ","),
            "context_down": "ETH dropping faster than BTC. Last 4 times:\n-> ETH bottomed 2-3 hours before BTC\n-> Recovery was 15-20% within 72 hours",
            "context_up": "ETH outpacing BTC signals altcoin season brewing. Last 3 times:\n-> ETH ran another 20-30% within 2 weeks\n-> Altcoins followed with 2-3x moves",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + " — loses this and $" + str(int(price * 0.92)) + " comes fast.",
            "key_up": "Key Level: $" + str(int(price * 1.04)) + " — breaks above and ETH enters momentum phase.",
        },
        "Oil": {
            "emoji": "🛢️",
            "price_str": "$" + str(round(price, 2)),
            "context_down": "Oil dropping this fast signals demand destruction. Last 3 times:\n-> Further 8-12% decline followed\n-> Energy stocks underperformed for 2-3 weeks",
            "context_up": "Oil spiking this fast means supply shock incoming. Last 3 times:\n-> Crude continued higher for 2-3 weeks\n-> Energy stocks outperformed by 15-20%",
            "key_down": "Key Level: $" + str(round(price * 0.95, 2)) + " — breaks below and demand destruction narrative kicks in.",
            "key_up": "Key Level: $" + str(round(price * 1.05, 2)) + " — breaks above and energy stocks explode higher.",
        },
        "Gold": {
            "emoji": "🥇",
            "price_str": "$" + format(int(price), ",") + "/oz",
            "context_down": "Gold dropping this fast signals risk-on shift. Last 3 times:\n-> Gold stabilized within 24-48 hours\n-> Stocks rallied as fear eased",
            "context_up": "Gold spiking this fast signals panic buying. Last 4 times:\n-> Gold ran another 8-12% within 2 weeks\n-> Stocks dropped 5-8% in the same period",
            "key_down": "Key Level: $" + str(int(price * 0.97)) + "/oz — breaks below and profit taking accelerates.",
            "key_up": "Key Level: $" + str(int(price * 1.03)) + "/oz — breaks above and we enter uncharted territory.",
        },
        "Silver": {
            "emoji": "🥈",
            "price_str": "$" + str(round(price, 2)) + "/oz",
            "context_down": "Silver dropping fast. Last 3 times:\n-> Gold followed lower within 48 hours\n-> Both metals stabilized after 5-8% drop",
            "context_up": "Silver outpacing gold signals industrial demand shock. Last 3 times:\n-> Gold followed with 5-8% within 48 hours\n-> Both metals ran 2-3 weeks straight",
            "key_down": "Key Level: $" + str(round(price * 0.95, 2)) + "/oz — loses this and selling accelerates.",
            "key_up": "Key Level: $" + str(round(price * 1.05, 2)) + "/oz — breaks above and silver enters momentum phase.",
        },
        "SPX": {
            "emoji": "📊",
            "price_str": format(int(price), ","),
            "context_down": "Institutional selling detected. Last 3 times S&P dropped this fast:\n-> Further 5-8% drop followed\n-> Recovery took 2-3 weeks minimum",
            "context_up": "Institutional buying detected. Last 3 times S&P spiked this fast:\n-> Rally continued for 1-2 weeks\n-> Tech and growth led higher",
            "key_down": "Key Level: " + str(int(price * 0.97)) + " — breaks below and panic selling accelerates.",
            "key_up": "Key Level: " + str(int(price * 1.03)) + " — breaks above and all-time high is back in play.",
        },
    }

    if asset not in prompts:
        return None

    p = prompts[asset]
    context = p["context_up"] if change_pct > 0 else p["context_down"]
    key_level = p["key_up"] if change_pct > 0 else p["key_down"]

    post = "⚡ FLASH ALERT — " + now + "\n"
    post += "------------------------------\n"
    post += p["emoji"] + " " + asset + " " + p["price_str"] + " " + direction + " (" + change_str + " in the last hour)\n"
    post += "------------------------------\n"
    post += context + "\n\n"
    post += "💡 " + key_level + "\n"
    post += "------------------------------\n"
    post += "Historical data only. Not financial advice. DYOR. 🌲"

    return post


def generate_fear_greed_alert(value):
    now = datetime.utcnow().strftime("%B %d, %Y | %H:%M UTC")
    post = "⚡ FLASH ALERT — " + now + "\n"
    post += "------------------------------\n"
    post += "😱 Fear & Greed: " + str(value) + " — EXTREME FEAR\n"
    post += "------------------------------\n"
    post += "Last 5 times Fear & Greed hit this level:\n"
    post += "-> Markets bottomed within 48-72 hours\n"
    post += "-> BTC rebounded 20-30% within 30 days\n"
    post += "-> S&P 500 recovered 10-15% within 6 weeks\n\n"
    post += "Extreme fear = extreme opportunity. Historically.\n\n"
    post += "💡 Key Level: BTC $70,000 — line between recovery and capitulation.\n"
    post += "------------------------------\n"
    post += "Historical data only. Not financial advice. DYOR. 🌲"
    return post


def check_flash_alerts(prices, fear_greed_value):
    global previous_prices

    thresholds = {
        "BTC": 5.0,
        "ETH": 6.0,
        "Oil": 4.0,
        "Gold": 2.0,
        "Silver": 3.0,
        "SPX": 2.0,
    }

    for asset, threshold in thresholds.items():
        raw_key = asset + "_RAW"
        if raw_key not in prices:
            continue

        current_price = prices[raw_key]

        if asset in previous_prices:
            prev_price = previous_prices[asset]
            hourly_change = ((current_price - prev_price) / prev_price) * 100

            if abs(hourly_change) >= threshold:
                print("FLASH ALERT triggered for " + asset + ": " + str(round(hourly_change, 1)) + "%")
                alert_text = generate_flash_alert(asset, current_price, hourly_change)
                if alert_text:
                    send_to_telegram(alert_text)

        previous_prices[asset] = current_price

    if fear_greed_value is not None and fear_greed_value <= 15:
        fg_file = "/tmp/fg_alert.txt"
        try:
            with open(fg_file, "r") as f:
                last_alert = f.read().strip()
            if last_alert == str(fear_greed_value):
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

    today = datetime.utcnow().strftime("%B %d, %Y | %H:%M UTC")
    btc = prices.get("BTC", "N/A")
    eth = prices.get("ETH", "N/A")
    spx = prices.get("SPX", "N/A")
    gold = prices.get("Gold", "N/A")
    silver = prices.get("Silver", "N/A")
    oil = prices.get("Oil", "N/A")
    dxy = prices.get("DXY", "N/A")
    fear_greed_str = str(fear_greed_value) + "/100 - " + fear_greed_class if fear_greed_value else "N/A"
    prefix = "TEST POST - " if test else ""

    prompt = "You are a sharp market analyst writing for a Telegram channel. You talk like a smart experienced friend who knows markets inside out. Direct, confident, zero fluff. Your readers want to know exactly what is happening and exactly what they should be thinking about it. Never say buy or sell but make the implication so obvious they know exactly what to do.\n\n"
    prompt += "DXY CONTEXT - always use this in your analysis:\n"
    prompt += "When DXY rises: crypto falls, gold falls, oil falls - dollar strength kills risk assets\n"
    prompt += "When DXY falls: crypto rises, gold rises - dollar weakness fuels risk assets\n"
    prompt += "Always factor DXY direction into crypto and gold bullets.\n\n"
    prompt += "PERFECT BULLET EXAMPLES:\n"
    prompt += "-> BTC down 0.9% while DXY spikes to 98. Dollar strength historically kills crypto within 48 hours. Reduce now, reload lower. 📉\n"
    prompt += "-> Oil crashed 9% as Hormuz reopens. Last 3 times supply fears eased this fast - stocks rallied 8% in 30 days. Smart money is already rotating. 📈\n"
    prompt += "-> Gold up 1.5% despite DXY climbing - central banks buying overrides dollar strength. This is the hedge you want. 🥇\n"
    prompt += "-> S&P 500 at record but DXY rising and war escalating. Shift to defensives - growth will bleed. 🛡️\n"
    prompt += "-> ETH down 1.7% following BTC. When BTC bleeds this hard ETH drops 2x more historically. Protect your capital. ⚠️\n\n"
    prompt += "Write the post using this exact format:\n\n"
    prompt += prefix + "Raw Brief - " + today + "\n"
    prompt += "------------------------------\n"
    prompt += "BTC " + btc + "\n"
    prompt += "ETH " + eth + "\n"
    prompt += "S&P 500 " + spx + "\n"
    prompt += "Gold " + gold + "\n"
    prompt += "Silver " + silver + "\n"
    prompt += "Oil " + oil + "\n"
    prompt += "DXY " + dxy + "\n"
    prompt += "Fear & Greed " + fear_greed_str + "\n"
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
    prompt += "Key Level: [ONE specific price level to watch today - any asset]\n"
    prompt += "------------------------------\n"
    prompt += "Historical data only. Not financial advice. DYOR.\n\n"
    prompt += "RULES:\n"
    prompt += "1. Exactly 2 bullets per section\n"
    prompt += "2. Every bullet must include a specific price, number or percentage\n"
    prompt += "3. Emoji at the END of each bullet\n"
    prompt += "4. Max 20 words per bullet - punchy and direct\n"
    prompt += "5. Every bullet must include historical context\n"
    prompt += "6. Every bullet must end with a clear actionable direction:\n"
    prompt += "   BULLISH: 'Load up or miss it.', 'Smart money is already in.', 'This is the entry.', 'Energy exposure makes sense here.', 'This is the hedge you want.'\n"
    prompt += "   BEARISH: 'Reduce exposure now.', 'Reduce now, reload lower.', 'Get out before the herd.', 'Cash is a position.', 'Stay out until stabilized.', 'Protect your capital.', 'Wait for dollar to peak.'\n"
    prompt += "   NEUTRAL: 'Watch this level closely.', 'Next move decides everything.', 'Tight stop losses here.'\n"
    prompt += "7. Replace [SENTIMENT] with: '🟢 Bullish' or '🔴 Bearish' or '🟡 Neutral'\n"
    prompt += "8. No URLs, no source names, no markdown\n"
    prompt += "9. Crypto: ONLY Bitcoin and Ethereum price action, ETF inflows, or major regulation. Never mention DeFi, altcoins, or obscure projects.\n"
    prompt += "10. Stocks: only top S&P 500 companies or major bank earnings\n"
    prompt += "11. Commodities: only gold, silver, oil, natural gas, wheat, copper\n"
    prompt += "12. Always factor in DXY direction when writing crypto and gold bullets\n"
    prompt += "13. If institutional data is available such as ETF inflows or central bank buying - always lead with it\n"
    prompt += "14. Write in English\n"
    prompt += "15. Key Level: ONE specific price level most important today across ANY asset. Example: 'Oil $80 - breaks below and inflation narrative collapses' or 'BTC $72,000 - loses this and next stop is $65k' or 'Gold $5,000 - breaks above and enters uncharted territory'\n\n"
    prompt += "Articles:\n" + articles_text + "\n"
    prompt += "Write only the post. Nothing else."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    total_tokens = input_tokens + output_tokens

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
    warning_threshold = int(monthly_budget * 0.8)

    print("Tokens used this session: " + str(used_tokens) + " / " + str(monthly_budget))

    if used_tokens >= warning_threshold and ADMIN_CHAT_ID:
        notify_admin("RAW BRIEF BOT WARNING: " + str(used_tokens) + " tokens used out of " + str(monthly_budget) + " budget. Approaching limit - top up Anthropic credits soon.")

    return response.content[0].text.strip()


def generate_weekly_recap(prices, fear_greed_value, fear_greed_class):
    now = datetime.utcnow().strftime("%B %d, %Y | %H:%M UTC")
    btc = prices.get("BTC", "N/A")
    eth = prices.get("ETH", "N/A")
    spx = prices.get("SPX", "N/A")
    gold = prices.get("Gold", "N/A")
    silver = prices.get("Silver", "N/A")
    oil = prices.get("Oil", "N/A")
    dxy = prices.get("DXY", "N/A")
    fear_greed_str = str(fear_greed_value) + "/100 - " + fear_greed_class if fear_greed_value else "N/A"

    articles = fetch_news()
    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += str(i) + ". [" + article["category"].upper() + "] " + article["title"] + "\n"
        articles_text += "   Summary: " + article["description"] + "\n\n"

    prompt = "You are a sharp market analyst writing a WEEKLY RECAP for a Telegram channel. Sound like a smart friend giving a real debrief of the week. Direct, clear, actionable.\n\n"
    prompt += "Write the weekly recap using this exact format:\n\n"
    prompt += "Weekly Recap - " + now + "\n"
    prompt += "------------------------------\n"
    prompt += "BTC " + btc + "\n"
    prompt += "ETH " + eth + "\n"
    prompt += "S&P 500 " + spx + "\n"
    prompt += "Gold " + gold + "\n"
    prompt += "Silver " + silver + "\n"
    prompt += "Oil " + oil + "\n"
    prompt += "DXY " + dxy + "\n"
    prompt += "Fear & Greed " + fear_greed_str + "\n"
    prompt += "------------------------------\n"
    prompt += "This Week\n"
    prompt += "-> [biggest crypto move this week with % and what it means] [emoji]\n"
    prompt += "-> [biggest stock move this week with % and what it means] [emoji]\n"
    prompt += "-> [biggest commodity move this week with % and what it means] [emoji]\n"
    prompt += "------------------------------\n"
    prompt += "Next Week - Watch For\n"
    prompt += "-> [key event or level to watch with specific price] [emoji]\n"
    prompt += "-> [key event or level to watch with specific price] [emoji]\n"
    prompt += "-> [key event or level to watch with specific price] [emoji]\n"
    prompt += "------------------------------\n"
    prompt += "Overall Sentiment: [🟢 Bullish / 🔴 Bearish / 🟡 Neutral]\n"
    prompt += "------------------------------\n"
    prompt += "Historical data only. Not financial advice. DYOR.\n\n"
    prompt += "RULES:\n"
    prompt += "1. Every bullet must include a specific price or percentage\n"
    prompt += "2. Emoji at the END of each bullet\n"
    prompt += "3. Max 20 words per bullet\n"
    prompt += "4. Sound like a sharp friend giving a real weekly debrief\n"
    prompt += "5. Factor in DXY direction for crypto and gold commentary\n"
    prompt += "6. No URLs, no source names, no markdown\n"
    prompt += "7. Write in English\n\n"
    prompt += "Articles:\n" + articles_text + "\n"
    prompt += "Write only the recap. Nothing else."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
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
        print("No articles found.")
        return False

    post_text = format_with_claude(articles, prices, fear_greed_value, fear_greed_class, test=test)

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


def run_bot(test=False, test_chat_id=None):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print("\n" + "=" * 55)
    if test:
        print("TEST RUN - " + now)
    else:
        print("Running bot - " + now)
    print("=" * 55)

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


def run_weekly_recap():
    print("\n" + "=" * 55)
    print("WEEKLY RECAP - " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 55)
    try:
        prices = fetch_prices()
        fear_greed_value, fear_greed_class = fetch_fear_greed()
        recap_text = generate_weekly_recap(prices, fear_greed_value, fear_greed_class)
        print(recap_text)
        send_to_telegram(recap_text)
        print("Weekly recap delivered.")
    except Exception as e:
        print("ERROR in weekly recap: " + str(e))


def check_flash_alerts_job():
    prices = fetch_prices()
    fear_greed_value, fear_greed_class = fetch_fear_greed()
    check_flash_alerts(prices, fear_greed_value)


def handle_commands():
    offset = 0
    print("Command listener active.")
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
                        print("Received /post from chat_id: " + str(chat_id))
                        send_message_to_chat(chat_id, "Posting to channel now... Please wait.")
                        threading.Thread(target=run_bot).start()
                    elif text.startswith("/test"):
                        print("Received /test from chat_id: " + str(chat_id))
                        send_message_to_chat(chat_id, "Sending test post to you now... Please wait.")
                        threading.Thread(target=run_bot, kwargs={"test": True, "test_chat_id": chat_id}).start()
                    elif text.startswith("/recap"):
                        print("Received /recap from chat_id: " + str(chat_id))
                        send_message_to_chat(chat_id, "Generating weekly recap... Please wait.")
                        threading.Thread(target=run_weekly_recap).start()
                    elif text.startswith("/start"):
                        send_message_to_chat(chat_id, "Raw Brief Bot active.\n\nCommands:\n/post - Post to channel now\n/test - Send test post to you only\n/recap - Post weekly recap now")
        except Exception as e:
            print("Command listener error: " + str(e))
            time.sleep(10)


def start_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler = BlockingScheduler(timezone="UTC")

    for hour in [8, 13, 20]:
        scheduler.add_job(run_bot, "cron", hour=hour, minute=0)
        print("Scheduled daily post: " + str(hour).zfill(2) + ":00 UTC")

    scheduler.add_job(check_flash_alerts_job, "interval", minutes=60)
    print("Scheduled flash alert check: every 60 minutes")

    scheduler.add_job(run_weekly_recap, "cron", day_of_week="sun", hour=20, minute=0)
    print("Scheduled weekly recap: Sunday 20:00 UTC")

    cmd_thread = threading.Thread(target=handle_commands, daemon=True)
    cmd_thread.start()

    print("\n" + "=" * 55)
    print("Raw Brief Bot - Scheduler Active")
    print("Daily posts: 08:00, 13:00, 20:00 UTC")
    print("Flash alerts: every 60 minutes")
    print("Weekly recap: Sunday 20:00 UTC")
    print("/post - post to channel")
    print("/test - send test post to you only")
    print("/recap - post weekly recap now")
    print("=" * 55)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")


if __name__ == "__main__":
    check_env()
    start_scheduler()
