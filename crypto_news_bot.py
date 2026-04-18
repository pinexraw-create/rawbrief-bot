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
from datetime import datetime, timedelta

NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

MAX_RETRIES = 10
RETRY_INTERVAL_SECONDS = 5 * 60


def check_env():
    missing = []
    for var in ["NEWS_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]:
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
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        btc_price = data["bitcoin"]["usd"]
        btc_change = data["bitcoin"]["usd_24h_change"]
        sign = "+" if btc_change >= 0 else ""
        prices["BTC"] = "$" + format(int(btc_price), ",") + " (" + sign + str(round(btc_change, 1)) + "%)"
    except Exception as e:
        print("WARNING: Could not fetch BTC price: " + str(e))
        prices["BTC"] = "N/A"

    for ticker, label in [("%5EGSPC", "SPX"), ("GC%3DF", "Gold")]:
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
            change_
