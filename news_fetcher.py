"""
news_fetcher.py — Raw Brief Bot
RSS парсване от 11 feeds, дедупликация, macro keyword filter,
economic events за следващия ден.
"""

import hashlib
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import feedparser

from config import MACRO_KEYWORDS, RSS_FEEDS, CACHE_TTL_YAHOO

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# IN-MEMORY CACHE
# ──────────────────────────────────────────────
_news_cache: dict = {}
_seen_hashes: set = set()   # дедупликация за деня


def _cache_get(key: str, ttl: int):
    entry = _news_cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["value"]
    return None


def _cache_set(key: str, value) -> None:
    _news_cache[key] = {"value": value, "ts": time.time()}


def _hash_entry(title: str, link: str) -> str:
    raw = f"{title.lower().strip()}{link.strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


# ──────────────────────────────────────────────
# ПАРСВАНЕ НА ЕДИН FEED
# ──────────────────────────────────────────────
def _parse_feed(url: str, max_age_hours: int = 24) -> list:
    """Парсва един RSS feed, връща list от article dicts."""
    try:
        parsed = feedparser.parse(url, request_headers={
            "User-Agent": "RawBriefBot/1.0 (Telegram channel market signals)"
        })
    except Exception as e:
        logger.error("Feed parse error %s: %s", url, e)
        return []

    if parsed.get("bozo") and not parsed.get("entries"):
        logger.warning("Bozo feed: %s", url)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    articles = []

    for entry in parsed.get("entries", []):
        title = entry.get("title", "").strip()
        link  = entry.get("link", "").strip()
        if not title or not link:
            continue

        # Дата
        pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub_parsed:
            try:
                pub_dt = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            except Exception:
                pass

        # Дедупликация
        h = _hash_entry(title, link)
        if h in _seen_hashes:
            continue
        _seen_hashes.add(h)

        summary = entry.get("summary", "") or entry.get("description", "")
        # Почистване на HTML tags (базово)
        summary = _strip_html(summary)[:500]

        articles.append({
            "title": title,
            "link": link,
            "summary": summary,
            "source": _source_from_url(url),
            "published": entry.get("published", ""),
        })

    return articles


def _strip_html(text: str) -> str:
    """Махаме базови HTML тагове."""
    import re
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def _source_from_url(feed_url: str) -> str:
    """Извлича source name от feed URL."""
    mapping = {
        "reuters.com": "Reuters",
        "cnbc.com": "CNBC",
        "marketwatch.com": "MarketWatch",
        "apnews.com": "AP News",
        "cointelegraph.com": "CoinTelegraph",
        "theblock.co": "The Block",
        "coindesk.com": "CoinDesk",
        "oilprice.com": "OilPrice",
        "kitco.com": "Kitco",
    }
    for domain, name in mapping.items():
        if domain in feed_url:
            return name
    return "News"


# ──────────────────────────────────────────────
# FETCH ALL NEWS
# ──────────────────────────────────────────────
def fetch_all_news(max_age_hours: int = 6, max_per_feed: int = 5) -> list:
    """
    Парсва всички RSS feeds. TTL=300s кеш.
    Връща list от articles, сортирани по relevance.
    """
    cached = _cache_get("all_news", CACHE_TTL_YAHOO)
    if cached:
        return cached

    all_articles = []
    for url in RSS_FEEDS:
        try:
            articles = _parse_feed(url, max_age_hours=max_age_hours)
            all_articles.extend(articles[:max_per_feed])
        except Exception as e:
            logger.error("Error fetching %s: %s", url, e)

    # Ограничаваме до 40 статии общо
    result = all_articles[:40]
    _cache_set("all_news", result)
    logger.info("Fetched %d news articles from %d feeds", len(result), len(RSS_FEEDS))
    return result


# ──────────────────────────────────────────────
# MACRO FILTER
# ──────────────────────────────────────────────
def detect_macro_events(articles: list) -> list:
    """
    Връща статии с macro keywords — Fed, FOMC, CPI, NFP и т.н.
    """
    macro_hits = []
    for article in articles:
        text = (article["title"] + " " + article["summary"]).lower()
        matched = [kw for kw in MACRO_KEYWORDS if kw in text]
        if matched:
            macro_hits.append({
                **article,
                "macro_keywords": matched,
            })
    return macro_hits


def has_macro_event(articles: list) -> bool:
    return len(detect_macro_events(articles)) > 0


def get_macro_warning(articles: list) -> Optional[str]:
    """
    Връща warning string ако има macro event в новините.
    """
    hits = detect_macro_events(articles)
    if not hits:
        return None

    keywords = set()
    for h in hits:
        keywords.update(h.get("macro_keywords", []))

    kw_display = ", ".join(sorted(keywords)[:5]).upper()
    return f"⚠️ Macro risk in play: {kw_display} — signals carry higher uncertainty today"


# ──────────────────────────────────────────────
# CRYPTO NEWS FILTER
# ──────────────────────────────────────────────
def filter_crypto_news(articles: list, limit: int = 5) -> list:
    """Филтрира crypto-специфични новини."""
    crypto_keywords = [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain",
        "defi", "nft", "altcoin", "stablecoin", "binance", "coinbase",
        "sec", "etf", "halving", "mining",
    ]
    result = []
    for article in articles:
        text = (article["title"] + " " + article["summary"]).lower()
        if any(kw in text for kw in crypto_keywords):
            result.append(article)
        if len(result) >= limit:
            break
    return result


def filter_commodity_news(articles: list, limit: int = 3) -> list:
    """Филтрира commodity новини (Gold, Oil, Silver)."""
    keywords = [
        "gold", "silver", "oil", "crude", "brent", "wti",
        "opec", "commodities", "precious metals",
    ]
    result = []
    for article in articles:
        text = (article["title"] + " " + article["summary"]).lower()
        if any(kw in text for kw in keywords):
            result.append(article)
        if len(result) >= limit:
            break
    return result


def filter_equity_news(articles: list, limit: int = 3) -> list:
    """Филтрира equity/macro новини (SPX, DXY)."""
    keywords = [
        "s&p", "sp500", "nasdaq", "dow", "stocks", "equities",
        "wall street", "fed", "treasury", "dollar", "dxy",
        "earnings", "gdp", "cpi", "inflation",
    ]
    result = []
    for article in articles:
        text = (article["title"] + " " + article["summary"]).lower()
        if any(kw in text for kw in keywords):
            result.append(article)
        if len(result) >= limit:
            break
    return result


# ──────────────────────────────────────────────
# NEWS SUMMARY ЗА CLAUDE PROMPT
# ──────────────────────────────────────────────
def news_to_prompt_context(articles: list, max_articles: int = 8) -> str:
    """Форматира новините за вграждане в Claude prompt."""
    if not articles:
        return "No significant news in the last 6 hours."

    lines = ["Recent market news:"]
    for i, a in enumerate(articles[:max_articles], 1):
        lines.append(f"{i}. [{a['source']}] {a['title']}")
        if a.get("summary"):
            lines.append(f"   {a['summary'][:200]}")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# ECONOMIC EVENTS (следващия ден)
# ──────────────────────────────────────────────
# Статичен календар на major recurring events — ботът ги засича от новините
KNOWN_ECONOMIC_EVENTS = [
    {"keyword": "fomc", "display": "FOMC Meeting / Rate Decision"},
    {"keyword": "cpi", "display": "CPI Inflation Report"},
    {"keyword": "nfp", "display": "Non-Farm Payrolls"},
    {"keyword": "gdp", "display": "GDP Report"},
    {"keyword": "pce", "display": "PCE Price Index"},
    {"keyword": "powell", "display": "Fed Chair Powell Speech"},
    {"keyword": "jobless claims", "display": "Jobless Claims"},
    {"keyword": "pmi", "display": "PMI Data"},
    {"keyword": "retail sales", "display": "Retail Sales"},
    {"keyword": "opec", "display": "OPEC Meeting"},
]


def detect_upcoming_events(articles: list) -> list:
    """
    Засича upcoming economic events от новините.
    Използва се за вечерния пост — events за утре.
    """
    tomorrow_keywords = ["tomorrow", "next week", "upcoming", "scheduled", "expected", "forecast"]
    found = []

    for article in articles:
        text = (article["title"] + " " + article["summary"]).lower()
        is_upcoming = any(kw in text for kw in tomorrow_keywords)

        for event in KNOWN_ECONOMIC_EVENTS:
            if event["keyword"] in text:
                if is_upcoming or any(kw in text for kw in ["wednesday", "thursday", "friday", "monday", "tuesday"]):
                    if event["display"] not in [e["display"] for e in found]:
                        found.append(event)

    return found


def format_upcoming_events(events: list) -> str:
    """Форматира events за вечерния пост."""
    if not events:
        return "No major scheduled events detected for tomorrow."

    lines = ["📅 Events to watch:"]
    for e in events[:5]:
        lines.append(f"  • {e['display']}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# DAILY SEEN HASH RESET
# ──────────────────────────────────────────────
def reset_daily_hashes() -> None:
    """Нулира seen hashes в началото на деня (08:00 UTC)."""
    global _seen_hashes
    _seen_hashes = set()
    logger.info("Daily news hashes reset")
