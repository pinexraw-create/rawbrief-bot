"""
signal_engine.py — Raw Brief Bot
Confidence score от 4 компонента с динамични тегла.
Сглобява пълен сигнал за Claude prompt.
"""

import logging
from typing import Optional

from config import (
    ASSETS,
    WEIGHT_DEFAULT,
    WEIGHT_MAX,
    WEIGHT_MIN,
)
from data_fetcher import fetch_fear_greed, fetch_funding_rate, fetch_ohlc, fetch_ohlc_4h, fetch_all_prices
from database import get_weights
from technical_analysis import run_mtf_analysis, mtf_alignment_context

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# CONFIDENCE SCORE
# ──────────────────────────────────────────────
def calc_confidence(
    direction: str,
    rsi: Optional[float],
    dominance: float,
    fear_greed: Optional[dict],
    funding_rate: Optional[float],
    weights: dict,
) -> int:
    """
    Confidence 0-100 от 4 компонента:
    1. Signal dominance (0-40)
    2. RSI alignment с посоката (0-25)
    3. Fear & Greed alignment (0-20)
    4. Funding rate alignment (0-15) — само за crypto

    Всеки компонент се мащабира с dynamic weight.
    """
    score = 0.0

    w_rsi = weights.get("rsi_weight", WEIGHT_DEFAULT)
    w_fg  = weights.get("fear_greed_weight", WEIGHT_DEFAULT)
    w_fr  = weights.get("funding_weight", WEIGHT_DEFAULT)

    # Clamp weights
    w_rsi = max(WEIGHT_MIN, min(WEIGHT_MAX, w_rsi))
    w_fg  = max(WEIGHT_MIN, min(WEIGHT_MAX, w_fg))
    w_fr  = max(WEIGHT_MIN, min(WEIGHT_MAX, w_fr))

    # 1. Dominance → 0-40 points
    dominance_score = min(dominance * 80.0, 40.0)
    score += dominance_score

    # 2. RSI alignment → 0-25 points
    if rsi is not None:
        if direction == "BUY":
            if rsi < 30:
                rsi_score = 25.0
            elif rsi < 45:
                rsi_score = 15.0
            elif rsi < 55:
                rsi_score = 5.0
            else:
                rsi_score = 0.0
        else:  # SELL
            if rsi > 70:
                rsi_score = 25.0
            elif rsi > 55:
                rsi_score = 15.0
            elif rsi > 45:
                rsi_score = 5.0
            else:
                rsi_score = 0.0

        score += rsi_score * w_rsi

    # 3. Fear & Greed alignment → 0-20 points
    if fear_greed:
        fg_value = fear_greed.get("value", 50)
        if direction == "BUY":
            if fg_value < 25:
                fg_score = 20.0   # Extreme fear = buy opportunity
            elif fg_value < 45:
                fg_score = 10.0
            elif fg_value > 75:
                fg_score = 0.0    # Extreme greed = risky to buy
            else:
                fg_score = 5.0
        else:  # SELL
            if fg_value > 75:
                fg_score = 20.0   # Extreme greed = sell opportunity
            elif fg_value > 55:
                fg_score = 10.0
            elif fg_value < 25:
                fg_score = 0.0
            else:
                fg_score = 5.0

        score += fg_score * w_fg

    # 4. Funding rate alignment → 0-15 points (само crypto)
    if funding_rate is not None:
        if direction == "BUY":
            if funding_rate < -0.05:
                fr_score = 15.0  # Negative funding = longs paid, bullish
            elif funding_rate < 0:
                fr_score = 8.0
            elif funding_rate > 0.05:
                fr_score = 0.0   # High positive funding = crowded longs
            else:
                fr_score = 4.0
        else:  # SELL
            if funding_rate > 0.05:
                fr_score = 15.0  # High positive funding = shorts profitable
            elif funding_rate > 0:
                fr_score = 8.0
            elif funding_rate < -0.05:
                fr_score = 0.0
            else:
                fr_score = 4.0

        score += fr_score * w_fr

    # Нормализиране към 0-100
    # Максимален теоретичен score при max weights:
    # 40 + 25*2 + 20*2 + 15*2 = 140 → нормализираме към 100
    max_possible = 40 + 25 * w_rsi + 20 * w_fg + 15 * w_fr
    if max_possible > 0:
        normalized = (score / max_possible) * 100
    else:
        normalized = 50.0

    return max(10, min(95, round(normalized)))


# ──────────────────────────────────────────────
# FULL SIGNAL ASSEMBLY
# ──────────────────────────────────────────────
def build_signal(asset: str) -> Optional[dict]:
    """
    Сглобява пълен сигнал за актива с MTF анализ.
    Връща None ако посоката е NEUTRAL или данните са недостатъчни.
    """
    # Цена
    prices = fetch_all_prices()
    current_price = prices.get(asset)
    if not current_price:
        logger.error("No price for %s", asset)
        return None

    # OHLC daily + 4H
    ohlc_daily = fetch_ohlc(asset)
    if not ohlc_daily:
        logger.error("No daily OHLC for %s", asset)
        return None

    ohlc_4h = fetch_ohlc_4h(asset)  # None ако недостъпно — graceful

    # Тегла от Supabase
    weights = get_weights(asset)

    # MTF анализ
    analysis = run_mtf_analysis(asset, ohlc_daily, ohlc_4h, current_price, weights)
    if not analysis:
        return None

    direction = analysis["direction"]
    if direction == "NEUTRAL":
        logger.info("%s signal is NEUTRAL — skipping", asset)
        return None

    # Fear & Greed (само за crypto)
    fear_greed = None
    if ASSETS[asset]["type"] == "crypto":
        fear_greed = fetch_fear_greed()

    # Funding rate (само за crypto)
    funding_rate = fetch_funding_rate(asset)

    # Base confidence
    confidence = calc_confidence(
        direction=direction,
        rsi=analysis.get("rsi"),
        dominance=analysis.get("dominance", 0.0),
        fear_greed=fear_greed,
        funding_rate=funding_rate,
        weights=weights,
    )

    # MTF adjustment
    confidence_adj = analysis.get("mtf_confidence_adj", 0)
    confidence = max(10, min(95, confidence + confidence_adj))

    # CONFLICT → блокираме сигнала ако confidence падне под 35
    if analysis.get("mtf_alignment") == "CONFLICT" and confidence < 35:
        logger.info(
            "%s signal blocked — MTF conflict + low confidence (%d)",
            asset, confidence
        )
        return None

    tp_sl = analysis.get("tp_sl")
    if not tp_sl:
        return None

    return {
        "asset": asset,
        "asset_name": ASSETS[asset]["name"],
        "asset_emoji": ASSETS[asset]["emoji"],
        "asset_type": ASSETS[asset]["type"],
        "direction": direction,
        "current_price": current_price,
        "entry": tp_sl["entry"],
        "take_profit": tp_sl["tp"],
        "stop_loss": tp_sl["sl"],
        "risk": tp_sl["risk"],
        "reward": tp_sl["reward"],
        "rr_ratio": tp_sl["rr"],
        "atr": tp_sl["atr"],
        "confidence": confidence,
        "rsi": analysis.get("rsi"),
        "ema20": analysis.get("ema20"),
        "ema50": analysis.get("ema50"),
        "volume_trend": analysis.get("volume_trend"),
        "support": analysis.get("support"),
        "resistance": analysis.get("resistance"),
        "all_supports": analysis.get("all_supports", []),
        "all_resistances": analysis.get("all_resistances", []),
        "bullish_score": analysis.get("bullish_score"),
        "bearish_score": analysis.get("bearish_score"),
        "dominance": analysis.get("dominance"),
        "mtf_alignment": analysis.get("mtf_alignment", "NO_4H"),
        "mtf_4h_direction": analysis.get("mtf_4h_direction"),
        "mtf_context": mtf_alignment_context(analysis),
        "fear_greed": fear_greed,
        "funding_rate": funding_rate,
        "weights": weights,
    }


def build_all_signals(assets: list = None) -> dict:
    """
    Сглобява сигнали за всички (или избрани) активи.
    Връща dict {asset: signal_dict или None}.
    """
    if assets is None:
        assets = list(ASSETS.keys())

    results = {}
    for asset in assets:
        try:
            signal = build_signal(asset)
            results[asset] = signal
        except Exception as e:
            logger.error("Signal build failed for %s: %s", asset, e)
            results[asset] = None

    return results


# ──────────────────────────────────────────────
# WEEKEND CHECK
# ──────────────────────────────────────────────
def is_weekend_locked(asset: str, weekday: int) -> bool:
    """weekday: 0=Monday, 6=Sunday. Locked = Saturday(5) или Sunday(6)."""
    if not ASSETS.get(asset, {}).get("weekend_locked", False):
        return False
    return weekday >= 5


# ──────────────────────────────────────────────
# SIGNAL SUMMARY (за Claude prompt context)
# ──────────────────────────────────────────────
def signal_to_prompt_context(signal: dict) -> str:
    """
    Преобразува signal dict в структуриран текст за Claude.
    Plain English — без забранените думи.
    """
    asset = signal["asset"]
    direction = signal["direction"]
    price = signal["current_price"]
    tp = signal["take_profit"]
    sl = signal["stop_loss"]
    rr = signal["rr_ratio"]
    conf = signal["confidence"]
    rsi = signal.get("rsi")
    ema20 = signal.get("ema20")
    ema50 = signal.get("ema50")
    vol = signal.get("volume_trend", "normal")
    sup = signal.get("support")
    res = signal.get("resistance")
    fg = signal.get("fear_greed")
    fr = signal.get("funding_rate")

    lines = [
        f"Asset: {asset} ({signal['asset_name']})",
        f"Direction: {direction}",
        f"Current price: {price}",
        f"Entry: {signal['entry']}",
        f"Take profit: {tp}",
        f"Stop loss: {sl}",
        f"Risk/Reward: 1:{rr}",
        f"Confidence: {conf}%",
    ]

    # Технически контекст (описателно)
    if rsi is not None:
        if rsi < 35:
            lines.append(f"Price has been beaten down hard (RSI-equiv: {rsi:.0f}) — historically a zone where buyers step in")
        elif rsi > 65:
            lines.append(f"Price has been running hot (RSI-equiv: {rsi:.0f}) — getting into territory where sellers appear")
        else:
            lines.append(f"Price sitting in neutral territory (RSI-equiv: {rsi:.0f})")

    if ema20 and ema50:
        if ema20 > ema50:
            lines.append("Short-term trend is above the longer-term trend — buyers in control")
        else:
            lines.append("Short-term trend has fallen below the longer-term trend — sellers in control")

    if vol == "high":
        lines.append("Volume is noticeably above recent average — conviction behind this move")
    elif vol == "low":
        lines.append("Volume is thin — move lacks strong conviction")

    if sup:
        lines.append(f"Key floor below: {sup:.2f}")
    if res:
        lines.append(f"Sellers keep showing up near: {res:.2f}")

    if fg:
        lines.append(f"Fear & Greed: {fg['value']} ({fg['label']})")

    if fr is not None:
        if fr > 0.05:
            lines.append(f"Futures funding positive ({fr:.3f}%) — longs paying shorts, crowded trade")
        elif fr < -0.05:
            lines.append(f"Futures funding negative ({fr:.3f}%) — shorts paying longs, contrarian setup")
        else:
            lines.append(f"Futures funding neutral ({fr:.3f}%)")

    # MTF alignment
    mtf_ctx = signal.get("mtf_context", "")
    if mtf_ctx:
        lines.append(f"Multi-timeframe: {mtf_ctx}")

    return "\n".join(lines)
