#!/usr/bin/env python3
"""
Crypto, Stock & Commodity News Bot
Fetches financial news via NewsAPI, formats with Claude Haiku, sends to Telegram.

Setup requirements:
  1. NEWS_API_KEY       — from https://newsapi.org
  2. ANTHROPIC_API_KEY  — from https://console.anthropic.com
  3. TELEGRAM_BOT_TOKEN — from @BotFather on Telegram
  4. TELEGRAM_CHANNEL_ID — channel username (e.g. @mychannel) or numeric ID

  The bot MUST be added as an Administrator to your Telegram channel before running.

Run:
  python3 crypto_news_bot.py
"""

import os
import sys
import time
import requests
import anthropic
import urllib.request
import urllib.error
import json
from datetime import datetime, timedelta


NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")


def check_env():
    missing = []
    for var in ["NEWS_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"  ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)


def verify_telegram_bot() -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                bot = result["result"]
                print(f"  Bot verified: @{bot['username']} ({bot['first_name']})")
                return bot
            else:
                print(f"  ERROR: Invalid bot token — {result.get('description')}")
                sys.exit(1)
    except Exception as e:
        print(f"  ERROR: Could not reach Telegram API — {e}")
        sys.exit(1)


def fetch_prices() -> dict:
    """Fetch live prices for BTC, S&P 500, and Gold."""
    prices = {}

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        btc_price = data["bitcoin"]["usd"]
        btc_change = data["bitcoin"]["usd_24h_change"]
        sign = "+" if btc_change >= 0 else ""
        prices["BTC"] = f"${btc_price:,.0f} ({sign}{btc_change:.1f}%)"
    except Exception as e:
        print(f"  WARNING: Could not fetch BTC price: {e}")
        prices["BTC"] = "N/A"

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


def fetch_fear_greed() -> str:
    """Fetch the Crypto Fear & Greed Index from alternative.me."""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        entry = data["data"][0]
        value = entry["value"]
        classification = entry["value_classification"]
        print(f"  Fear & Greed Index: {value} ({classification})")
        return f"{value}/100 — {classification}"
    except Exception as e:
        print(f"  WARNING: Could not fetch Fear & Greed Index: {e}")
        return "N/A"


def fetch_news() -> list[dict]:
    """Fetch crypto, stock, and commodity news from NewsAPI."""
    base_url = "https://newsapi.org/v2/everything"
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

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


def format_with_claude(articles: list[dict], prices: dict, fear_greed: str) -> str:
    """Use Claude Haiku to format articles into a punchy Telegram post."""
    if not articles:
        return None

    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += (
            f"{i}. [{article['category'].upper()}] {article['title']}\n"
            f"   Summary: {article['description']}\n\n"
        )

    today = datetime.utcnow().strftime("%B %d, %Y")
    btc = prices.get("BTC", "N/A")
    spx = prices.get("SPX", "N/A")
    gold = prices.get("Gold", "N/A")

    prompt = f"""You are a financial news editor for a beginner-friendly Telegram channel. Produce today's post using EXACTLY the template below.

TEMPLATE (copy structure exactly, fill in bullets from the articles):

🌍 Raw Brief — {today}
——————————————————
🟠 BTC {btc}
📊 S&P 500 {spx}
🟡 Gold {gold}
😱 Fear & Greed {fear_greed}
——————————————————
🪙 Crypto
→ [bullet] [emoji]
→ [bullet] [emoji]
——————————————————
📈 Stocks
→ [bullet] [emoji]
→ [bullet] [emoji]
——————————————————
🛢 Commodities
→ [bullet] [emoji]
→ [bullet] [emoji]
——————————————————
Not financial advice. DYOR. 🌲

BULLET RULES — follow every one:
1. Exactly 2 bullets per section, never more or fewer
2. Every bullet MUST include a specific number, price, or % — no vague statements ever
3. Emoji goes at the END: '→ Text here 🚀' — never at the start
4. Emojis: 🚀 bullish, 🔴 bearish, 👀 watch, ⚠️ risk, 📉 down, 📈 up
5. Max 15 words per bullet
6. If a company or term may be unfamiliar to beginners, add 2–3 words of context in brackets immediately after the name. Examples
