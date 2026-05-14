"""
config.py — Raw Brief Bot
Централна конфигурация. Всички ENV vars, константи и дефиниции на активи.
"""

import os
from typing import Final

# ──────────────────────────────────────────────
# CORE CREDENTIALS (от Railway Environment)
# ──────────────────────────────────────────────
TELEGRAM_TOKEN: Final[str] = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL_ID: Final[str] = os.environ["TELEGRAM_CHANNEL_ID"]
ANTHROPIC_API_KEY: Final[str] = os.environ["ANTHROPIC_API_KEY"]
DATABASE_URL: Final[str] = os.environ["DATABASE_URL"]

# ──────────────────────────────────────────────
# ANTHROPIC MODEL
# ──────────────────────────────────────────────
CLAUDE_MODEL: Final[str] = "claude-sonnet-4-5"
CLAUDE_MAX_TOKENS: Final[int] = 2000

# ──────────────────────────────────────────────
# SCHEDULE (UTC)
# ──────────────────────────────────────────────
MORNING_HOUR: Final[int] = 8
MIDDAY_HOUR: Final[int] = 13
EVENING_HOUR: Final[int] = 20
WEEKLY_RECAP_HOUR: Final[int] = 22
WEEKLY_RECAP_DAY: Final[str] = "sun"          # APScheduler day_of_week

# ──────────────────────────────────────────────
# CACHE TTL (секунди)
# ──────────────────────────────────────────────
CACHE_TTL_CRYPTO: Final[int] = 60
CACHE_TTL_YAHOO: Final[int] = 300
CACHE_TTL_OHLC: Final[int] = 3600
CACHE_TTL_FEAR_GREED: Final[int] = 3600

# ──────────────────────────────────────────────
# FLASH ALERT THRESHOLDS (% промяна за 1 час)
# ──────────────────────────────────────────────
FLASH_THRESHOLDS: Final[dict] = {
    "BTC":    5.0,
    "ETH":    6.0,
    "Gold":   2.0,
    "Silver": 3.0,
    "Oil":    4.0,
    "SPX":    2.0,
}
FEAR_GREED_ALERT_THRESHOLD: Final[int] = 15

# ──────────────────────────────────────────────
# ТЕХНИЧЕСКИ АНАЛИЗ — ПЕРИОДИ
# ──────────────────────────────────────────────
RSI_PERIOD: Final[int] = 14
EMA_SHORT: Final[int] = 20
EMA_LONG: Final[int] = 50
ATR_PERIOD: Final[int] = 14
VOLUME_AVG_PERIOD: Final[int] = 10
OHLC_LOOKBACK_DAYS: Final[int] = 60
SR_SWING_WINDOW: Final[int] = 5           # bars за swing high/low

# ──────────────────────────────────────────────
# SIGNAL — МИНИМАЛНИ SL РАЗСТОЯНИЯ (%)
# ──────────────────────────────────────────────
MIN_SL_PCT: Final[dict] = {
    "BTC":    0.015,
    "ETH":    0.015,
    "SPX":    0.008,
    "Gold":   0.008,
    "Silver": 0.012,
    "Oil":    0.012,
}

# ──────────────────────────────────────────────
# SIGNAL — R/R
# ──────────────────────────────────────────────
MIN_RR_RATIO: Final[int] = 3              # минимум 1:3
TP_ATR_MULTIPLIER: Final[float] = 3.0    # TP = current + 3*ATR (BUY)

# ──────────────────────────────────────────────
# SELF-LEARNING — ГРАНИЦИ НА ТЕГЛА
# ──────────────────────────────────────────────
WEIGHT_MIN: Final[float] = 0.5
WEIGHT_MAX: Final[float] = 2.0
WEIGHT_DEFAULT: Final[float] = 1.0
ACCURACY_LOW_THRESHOLD: Final[float] = 0.55   # под 55% → вдига RSI + F&G тегло

# ──────────────────────────────────────────────
# PREDICTION ARCHIVE — ИСТОРИЯ
# ──────────────────────────────────────────────
PREDICTION_HISTORY_DAYS: Final[int] = 14

# ──────────────────────────────────────────────
# АКТИВИ — ПЪЛНИ ДЕФИНИЦИИ
# ──────────────────────────────────────────────
ASSETS: Final[dict] = {
    "BTC": {
        "name": "Bitcoin",
        "type": "crypto",
        "coingecko_id": "bitcoin",
        "binance_symbol": "BTCUSDT",
        "yahoo_ticker": None,
        "emoji": "₿",
        "weekend_locked": False,
    },
    "ETH": {
        "name": "Ethereum",
        "type": "crypto",
        "coingecko_id": "ethereum",
        "binance_symbol": "ETHUSDT",
        "yahoo_ticker": None,
        "emoji": "Ξ",
        "weekend_locked": False,
    },
    "SPX": {
        "name": "S&P 500",
        "type": "equity",
        "coingecko_id": None,
        "binance_symbol": None,
        "yahoo_ticker": "^GSPC",
        "emoji": "📈",
        "weekend_locked": True,
    },
    "Gold": {
        "name": "Gold",
        "type": "commodity",
        "coingecko_id": None,
        "binance_symbol": None,
        "yahoo_ticker": "GC=F",
        "emoji": "🥇",
        "weekend_locked": True,
    },
    "Silver": {
        "name": "Silver",
        "type": "commodity",
        "coingecko_id": None,
        "binance_symbol": None,
        "yahoo_ticker": "SI=F",
        "emoji": "🥈",
        "weekend_locked": True,
    },
    "Oil": {
        "name": "WTI Crude Oil",
        "type": "commodity",
        "coingecko_id": None,
        "binance_symbol": None,
        "yahoo_ticker": "CL=F",
        "emoji": "🛢️",
        "weekend_locked": True,
    },
    "DXY": {
        "name": "US Dollar Index",
        "type": "fx",
        "coingecko_id": None,
        "binance_symbol": None,
        "yahoo_ticker": "DX-Y.NYB",
        "emoji": "💵",
        "weekend_locked": True,
    },
}

# ──────────────────────────────────────────────
# MACRO КЛЮЧОВИ ДУМИ (за news filter)
# ──────────────────────────────────────────────
MACRO_KEYWORDS: Final[list] = [
    "fed", "fomc", "cpi", "nfp", "gdp", "powell",
    "inflation", "interest rate", "federal reserve",
    "jobs report", "unemployment", "rate decision",
    "rate hike", "rate cut", "quantitative",
]

# ──────────────────────────────────────────────
# RSS FEEDS
# ──────────────────────────────────────────────
RSS_FEEDS: Final[list] = [
    # Macro / Traditional
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/commoditiesNews",
    "https://feeds.reuters.com/reuters/economy",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://apnews.com/rss/apf-business",
    # Crypto
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    # Commodities
    "https://oilprice.com/rss/main",
    "https://www.kitco.com/rss/kitco-gold-silver-mining-news.rss",
]

# ──────────────────────────────────────────────
# API ENDPOINTS
# ──────────────────────────────────────────────
COINGECKO_BASE: Final[str] = "https://api.coingecko.com/api/v3"
BINANCE_PREMIUM_URL: Final[str] = "https://fapi.binance.com/fapi/v1/premiumIndex"
FEAR_GREED_URL: Final[str] = "https://api.alternative.me/fng/?limit=1"
YAHOO_CHART_BASE: Final[str] = "https://query1.finance.yahoo.com/v8/finance/chart"

# ──────────────────────────────────────────────
# ФАЙЛОВЕ (локални fallback-и)
# ──────────────────────────────────────────────
LAST_KNOWN_PRICES_FILE: Final[str] = "last_known_prices.json"
PREDICTIONS_FILE: Final[str] = "predictions.json"
LEARNING_WEIGHTS_FILE: Final[str] = "learning_weights.json"

# ──────────────────────────────────────────────
# ЗАБРАНЕНИ ДУМИ В АНАЛИЗА (plain-English enforcement)
# ──────────────────────────────────────────────
FORBIDDEN_ANALYSIS_WORDS: Final[list] = [
    "RSI", "EMA", "ATR", "support", "resistance",
    "overbought", "oversold", "momentum", "trendline",
    "crossover", "breakout", "retest", "confluence",
    "divergence", "indicators",
]
