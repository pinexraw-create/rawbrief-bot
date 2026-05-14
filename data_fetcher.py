"""
data_fetcher.py — Raw Brief Bot
Всички данни: CoinGecko, Yahoo Finance v8, Binance premiumIndex,
Fear & Greed. In-memory кеш с TTL + persistent fallback в Supabase.
"""

import logging
import time
from typing import Optional

import httpx

from config import (
    ASSETS,
    BINANCE_PREMIUM_URL,
    CACHE_TTL_CRYPTO,
    CACHE_TTL_FEAR_GREED,
    CACHE_TTL_OHLC,
    CACHE_TTL_YAHOO,
    COINGECKO_BASE,
    FEAR_GREED_URL,
    OHLC_LOOKBACK_DAYS,
    YAHOO_CHART_BASE,
)
from database import cache_price, get_cached_price

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# IN-MEMORY CACHE
# ──────────────────────────────────────────────
_cache: dict = {}  # key -> {"value": ..., "ts": float}


def _cache_get(key: str, ttl: int):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["value"]
    return None


def _cache_set(key: str, value) -> None:
    _cache[key] = {"value": value, "ts": time.time()}


# ──────────────────────────────────────────────
# HTTP CLIENT (sync, with retries)
# ──────────────────────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_client = httpx.Client(
    headers=_HEADERS,
    timeout=15.0,
    follow_redirects=True,
)


def _get_json(url: str, params: dict = None, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries):
        try:
            resp = _client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                wait = 2 ** attempt
                logger.warning("Rate limited %s — waiting %ss", url, wait)
                time.sleep(wait)
            else:
                logger.error("HTTP %s for %s", e.response.status_code, url)
                break
        except Exception as e:
            logger.error("Request error %s: %s", url, e)
            if attempt < retries - 1:
                time.sleep(1)
    return None


# ──────────────────────────────────────────────
# COINGECKO — CRYPTO PRICES
# ──────────────────────────────────────────────
def fetch_crypto_prices() -> dict:
    """Връща {asset: price} за BTC и ETH. TTL=60s."""
    cached = _cache_get("crypto_prices", CACHE_TTL_CRYPTO)
    if cached:
        return cached

    ids = ",".join(
        cfg["coingecko_id"]
        for cfg in ASSETS.values()
        if cfg["type"] == "crypto" and cfg["coingecko_id"]
    )
    data = _get_json(
        f"{COINGECKO_BASE}/simple/price",
        params={"ids": ids, "vs_currencies": "usd"},
    )

    result = {}
    if data:
        for asset, cfg in ASSETS.items():
            cg_id = cfg.get("coingecko_id")
            if cg_id and cg_id in data:
                price = data[cg_id].get("usd")
                if price:
                    result[asset] = float(price)
                    cache_price(asset, float(price))

    # Fallback към DB ако нещо липсва
    for asset, cfg in ASSETS.items():
        if cfg["type"] == "crypto" and asset not in result:
            fb = get_cached_price(asset)
            if fb:
                result[asset] = fb
                logger.warning("Using DB fallback for %s", asset)

    _cache_set("crypto_prices", result)
    return result


# ──────────────────────────────────────────────
# YAHOO FINANCE v8 — ТРАДИЦИОННИ АКТИВИ
# ──────────────────────────────────────────────
def _fetch_yahoo_price(ticker: str) -> Optional[float]:
    data = _get_json(
        f"{YAHOO_CHART_BASE}/{ticker}",
        params={"interval": "1m", "range": "1d"},
    )
    if not data:
        return None
    try:
        result = data["chart"]["result"]
        if not result:
            return None
        meta = result[0]["meta"]
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price else None
    except (KeyError, IndexError, TypeError):
        return None


def fetch_traditional_prices() -> dict:
    """Връща {asset: price} за SPX, Gold, Silver, Oil, DXY. TTL=300s."""
    cached = _cache_get("traditional_prices", CACHE_TTL_YAHOO)
    if cached:
        return cached

    result = {}
    for asset, cfg in ASSETS.items():
        if cfg["type"] in ("equity", "commodity", "fx") and cfg.get("yahoo_ticker"):
            price = _fetch_yahoo_price(cfg["yahoo_ticker"])
            if price:
                result[asset] = price
                cache_price(asset, price)
            else:
                fb = get_cached_price(asset)
                if fb:
                    result[asset] = fb
                    logger.warning("Using DB fallback for %s", asset)

    _cache_set("traditional_prices", result)
    return result


def fetch_all_prices() -> dict:
    """Обединява крипто + традиционни цени."""
    prices = {}
    prices.update(fetch_traditional_prices())
    prices.update(fetch_crypto_prices())
    return prices


# ──────────────────────────────────────────────
# YAHOO FINANCE v8 — OHLC (за технически анализ)
# ──────────────────────────────────────────────
def fetch_ohlc(asset: str) -> Optional[dict]:
    """
    Връща OHLC dict за актива за последните OHLC_LOOKBACK_DAYS дни.
    Format: {opens, highs, lows, closes, volumes, timestamps}
    TTL=3600s.
    """
    cache_key = f"ohlc_{asset}"
    cached = _cache_get(cache_key, CACHE_TTL_OHLC)
    if cached:
        return cached

    cfg = ASSETS.get(asset, {})
    ticker = cfg.get("yahoo_ticker")

    # Crypto: използваме CoinGecko OHLC
    if cfg.get("type") == "crypto" and cfg.get("coingecko_id"):
        return _fetch_coingecko_ohlc(asset, cfg["coingecko_id"])

    if not ticker:
        return None

    data = _get_json(
        f"{YAHOO_CHART_BASE}/{ticker}",
        params={"interval": "1d", "range": f"{OHLC_LOOKBACK_DAYS}d"},
    )
    if not data:
        return None

    try:
        result_list = data["chart"]["result"]
        if not result_list:
            return None
        res = result_list[0]
        timestamps = res.get("timestamp", [])
        indicators = res.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]

        ohlc = {
            "timestamps": timestamps,
            "opens":   [v if v is not None else 0.0 for v in quote.get("open", [])],
            "highs":   [v if v is not None else 0.0 for v in quote.get("high", [])],
            "lows":    [v if v is not None else 0.0 for v in quote.get("low", [])],
            "closes":  [v if v is not None else 0.0 for v in quote.get("close", [])],
            "volumes": [v if v is not None else 0   for v in quote.get("volume", [])],
        }

        # Филтрираме нули в closes
        valid = [(o, h, l, c, v, t) for o, h, l, c, v, t in zip(
            ohlc["opens"], ohlc["highs"], ohlc["lows"],
            ohlc["closes"], ohlc["volumes"], ohlc["timestamps"]
        ) if c and c > 0]

        if len(valid) < 20:
            return None

        ohlc = {
            "opens":      [x[0] for x in valid],
            "highs":      [x[1] for x in valid],
            "lows":       [x[2] for x in valid],
            "closes":     [x[3] for x in valid],
            "volumes":    [x[4] for x in valid],
            "timestamps": [x[5] for x in valid],
        }

        _cache_set(cache_key, ohlc)
        return ohlc

    except (KeyError, IndexError, TypeError) as e:
        logger.error("OHLC parse error for %s: %s", asset, e)
        return None


def fetch_ohlc_4h(asset: str) -> Optional[dict]:
    """
    Връща 4H OHLC за последните 20 дни.
    Yahoo: interval=4h, range=20d
    CoinGecko: interval=4h не е на free tier — използваме hourly и resample.
    TTL=300s (по-кратък от daily защото се мени по-бързо).
    """
    cache_key = f"ohlc_4h_{asset}"
    cached = _cache_get(cache_key, CACHE_TTL_YAHOO)
    if cached:
        return cached

    cfg = ASSETS.get(asset, {})

    # Crypto — CoinGecko hourly и resample до 4H
    if cfg.get("type") == "crypto" and cfg.get("coingecko_id"):
        result = _fetch_coingecko_4h(asset, cfg["coingecko_id"])
        if result:
            _cache_set(cache_key, result)
        return result

    # Traditional — Yahoo 4h
    ticker = cfg.get("yahoo_ticker")
    if not ticker:
        return None

    data = _get_json(
        f"{YAHOO_CHART_BASE}/{ticker}",
        params={"interval": "1h", "range": "20d"},
    )
    if not data:
        return None

    try:
        result_list = data["chart"]["result"]
        if not result_list:
            return None
        res = result_list[0]
        timestamps = res.get("timestamp", [])
        quote = res.get("indicators", {}).get("quote", [{}])[0]

        opens   = quote.get("open", [])
        highs   = quote.get("high", [])
        lows    = quote.get("low", [])
        closes  = quote.get("close", [])
        volumes = quote.get("volume", [])

        # Resample 1H → 4H
        ohlc_4h = _resample_to_4h(opens, highs, lows, closes, volumes, timestamps)
        if ohlc_4h and len(ohlc_4h["closes"]) >= 10:
            _cache_set(cache_key, ohlc_4h)
            return ohlc_4h
        return None

    except (KeyError, IndexError, TypeError) as e:
        logger.error("4H OHLC parse error for %s: %s", asset, e)
        return None


def _fetch_coingecko_4h(asset: str, cg_id: str) -> Optional[dict]:
    """CoinGecko hourly data → resample до 4H."""
    data = _get_json(
        f"{COINGECKO_BASE}/coins/{cg_id}/market_chart",
        params={"vs_currency": "usd", "days": "20", "interval": "hourly"},
    )
    if not data:
        return None

    try:
        prices = data.get("prices", [])
        if len(prices) < 20:
            return None

        # prices = [[timestamp_ms, price], ...]
        timestamps = [int(p[0] / 1000) for p in prices]
        closes_h   = [float(p[1]) for p in prices]
        # CoinGecko hourly няма OHLC — използваме close като proxy
        opens_h  = closes_h[:]
        highs_h  = closes_h[:]
        lows_h   = closes_h[:]
        vols_h   = [0.0] * len(closes_h)

        return _resample_to_4h(opens_h, highs_h, lows_h, closes_h, vols_h, timestamps)

    except (KeyError, ValueError, TypeError) as e:
        logger.error("CoinGecko 4H parse error for %s: %s", asset, e)
        return None


def _resample_to_4h(
    opens: list, highs: list, lows: list,
    closes: list, volumes: list, timestamps: list,
) -> Optional[dict]:
    """Resample hourly bars → 4H bars (group by 4)."""
    n = len(closes)
    if n < 4:
        return None

    r_opens, r_highs, r_lows, r_closes, r_vols, r_ts = [], [], [], [], [], []

    i = 0
    while i + 3 < n:
        chunk_o = [opens[j]   for j in range(i, i + 4) if opens[j]]
        chunk_h = [highs[j]   for j in range(i, i + 4) if highs[j]]
        chunk_l = [lows[j]    for j in range(i, i + 4) if lows[j]]
        chunk_c = [closes[j]  for j in range(i, i + 4) if closes[j]]
        chunk_v = [volumes[j] for j in range(i, i + 4)]

        if not chunk_c:
            i += 4
            continue

        r_opens.append(chunk_o[0] if chunk_o else chunk_c[0])
        r_highs.append(max(chunk_h) if chunk_h else chunk_c[-1])
        r_lows.append(min(chunk_l) if chunk_l else chunk_c[-1])
        r_closes.append(chunk_c[-1])
        r_vols.append(sum(chunk_v))
        r_ts.append(timestamps[i])
        i += 4

    if len(r_closes) < 10:
        return None

    return {
        "opens": r_opens,
        "highs": r_highs,
        "lows": r_lows,
        "closes": r_closes,
        "volumes": r_vols,
        "timestamps": r_ts,
    }


def _fetch_coingecko_ohlc(asset: str, cg_id: str) -> Optional[dict]:
    """CoinGecko OHLC endpoint — връща daily bars."""
    cache_key = f"ohlc_{asset}"
    data = _get_json(
        f"{COINGECKO_BASE}/coins/{cg_id}/ohlc",
        params={"vs_currency": "usd", "days": str(OHLC_LOOKBACK_DAYS)},
    )
    if not data or not isinstance(data, list):
        return None

    try:
        opens      = [float(bar[1]) for bar in data]
        highs      = [float(bar[2]) for bar in data]
        lows       = [float(bar[3]) for bar in data]
        closes     = [float(bar[4]) for bar in data]
        timestamps = [int(bar[0] / 1000) for bar in data]
        volumes    = [0.0] * len(closes)  # CoinGecko OHLC не дава volume

        if len(closes) < 20:
            return None

        ohlc = {
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": volumes,
            "timestamps": timestamps,
        }
        _cache_set(cache_key, ohlc)
        return ohlc
    except (IndexError, ValueError, TypeError) as e:
        logger.error("CoinGecko OHLC parse error for %s: %s", asset, e)
        return None


# ──────────────────────────────────────────────
# BINANCE FUTURES — FUNDING RATE
# ──────────────────────────────────────────────
def fetch_funding_rate(asset: str) -> Optional[float]:
    """Връща funding rate % за BTC/ETH. None за non-crypto."""
    cfg = ASSETS.get(asset, {})
    symbol = cfg.get("binance_symbol")
    if not symbol:
        return None

    cache_key = f"funding_{asset}"
    cached = _cache_get(cache_key, CACHE_TTL_CRYPTO)
    if cached is not None:
        return cached

    data = _get_json(BINANCE_PREMIUM_URL, params={"symbol": symbol})
    if not data:
        return None

    try:
        rate = float(data.get("lastFundingRate", 0)) * 100  # в %
        _cache_set(cache_key, rate)
        return rate
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────
# FEAR & GREED INDEX
# ──────────────────────────────────────────────
def fetch_fear_greed() -> Optional[dict]:
    """
    Връща {"value": int, "label": str}.
    TTL=3600s.
    """
    cached = _cache_get("fear_greed", CACHE_TTL_FEAR_GREED)
    if cached:
        return cached

    data = _get_json(FEAR_GREED_URL)
    if not data:
        return None

    try:
        item = data["data"][0]
        result = {
            "value": int(item["value"]),
            "label": item["value_classification"],
        }
        _cache_set("fear_greed", result)
        return result
    except (KeyError, IndexError, ValueError, TypeError):
        return None


# ──────────────────────────────────────────────
# PRICE CHANGE CONTEXT (за сесийни сравнения)
# ──────────────────────────────────────────────
def calculate_price_change(current: float, reference: float) -> dict:
    """Изчислява абсолютна и процентна промяна."""
    if not reference or reference == 0:
        return {"abs": 0.0, "pct": 0.0, "direction": "flat"}

    abs_change = current - reference
    pct_change = (abs_change / reference) * 100
    direction = "up" if pct_change > 0.05 else ("down" if pct_change < -0.05 else "flat")

    return {
        "abs": round(abs_change, 4),
        "pct": round(pct_change, 3),
        "direction": direction,
    }


def format_price(asset: str, price: float) -> str:
    """Форматира цена спрямо актива."""
    if asset in ("BTC",):
        return f"${price:,.0f}"
    elif asset in ("ETH",):
        return f"${price:,.0f}"
    elif asset in ("SPX",):
        return f"{price:,.0f}"
    elif asset in ("Gold",):
        return f"${price:,.0f}"
    elif asset in ("Silver",):
        return f"${price:.2f}"
    elif asset in ("Oil",):
        return f"${price:.2f}"
    elif asset in ("DXY",):
        return f"{price:.2f}"
    return f"{price:.4f}"
