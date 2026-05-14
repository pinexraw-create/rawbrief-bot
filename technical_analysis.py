"""
technical_analysis.py — Raw Brief Bot
RSI 14, EMA 20/50, ATR 14, Volume trend, Support/Resistance,
Signal direction — всичко from scratch без TA-Lib.
"""

import logging
import math
from typing import Optional

from config import (
    ATR_PERIOD,
    EMA_LONG,
    EMA_SHORT,
    MIN_SL_PCT,
    RSI_PERIOD,
    SR_SWING_WINDOW,
    TP_ATR_MULTIPLIER,
    VOLUME_AVG_PERIOD,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# EMA
# ──────────────────────────────────────────────
def calc_ema(values: list, period: int) -> list:
    """
    EMA от scratch. Multiplier = 2/(period+1).
    Връща list със същата дължина — първите (period-1) са None.
    """
    if len(values) < period:
        return [None] * len(values)

    k = 2.0 / (period + 1)
    result = [None] * len(values)

    # Seed: SMA на първите `period` стойности
    seed = sum(values[:period]) / period
    result[period - 1] = seed

    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)

    return result


# ──────────────────────────────────────────────
# RSI 14 (EMA метод на gains/losses)
# ──────────────────────────────────────────────
def calc_rsi(closes: list, period: int = RSI_PERIOD) -> list:
    """
    RSI базиран на EMA на gains/losses (Wilder smoothing).
    Връща list — първите period стойности са None.
    """
    if len(closes) < period + 1:
        return [None] * len(closes)

    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    result = [None] * len(closes)
    k = 1.0 / period  # Wilder: 1/period вместо 2/(period+1)

    # Seed: SMA на първите `period` gains/losses
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = gains[i] * k + avg_gain * (1 - k)
        avg_loss = losses[i] * k + avg_loss * (1 - k)

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1 + rs))

        result[i + 1] = round(rsi, 2)

    return result


# ──────────────────────────────────────────────
# ATR 14
# ──────────────────────────────────────────────
def calc_atr(highs: list, lows: list, closes: list, period: int = ATR_PERIOD) -> list:
    """
    ATR от true range. True range = max(H-L, |H-PrevC|, |L-PrevC|).
    Връща list — първите period стойности са None.
    """
    n = len(closes)
    if n < period + 1:
        return [None] * n

    trs = [None]
    for i in range(1, n):
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    result = [None] * n
    # Seed: SMA на първите period TR
    valid_trs = [t for t in trs[1:period + 1] if t is not None]
    if len(valid_trs) < period:
        return result

    atr = sum(valid_trs) / period
    result[period] = round(atr, 6)

    k = (period - 1) / period  # Wilder smoothing
    for i in range(period + 1, n):
        if trs[i] is not None:
            atr = trs[i] / period + atr * k
            result[i] = round(atr, 6)

    return result


# ──────────────────────────────────────────────
# VOLUME TREND
# ──────────────────────────────────────────────
def calc_volume_trend(volumes: list, avg_period: int = VOLUME_AVG_PERIOD) -> Optional[str]:
    """
    Сравнява средния volume на последните avg_period бара
    с предходните avg_period бара.
    Връща: "high" / "low" / "normal"
    """
    if len(volumes) < avg_period * 2:
        return "normal"

    recent = volumes[-avg_period:]
    previous = volumes[-(avg_period * 2):-avg_period]

    recent_avg = sum(recent) / avg_period if avg_period > 0 else 0
    prev_avg = sum(previous) / avg_period if avg_period > 0 else 0

    if prev_avg == 0:
        return "normal"

    ratio = recent_avg / prev_avg
    if ratio > 1.3:
        return "high"
    elif ratio < 0.7:
        return "low"
    return "normal"


# ──────────────────────────────────────────────
# SUPPORT / RESISTANCE (swing highs/lows)
# ──────────────────────────────────────────────
def find_support_resistance(
    highs: list,
    lows: list,
    closes: list,
    window: int = SR_SWING_WINDOW,
) -> dict:
    """
    Swing highs → resistance levels
    Swing lows  → support levels
    Връща най-близкия support под и resistance над текущата цена.
    """
    if len(closes) < window * 2 + 1:
        return {"support": None, "resistance": None}

    current = closes[-1]
    swing_highs = []
    swing_lows = []

    for i in range(window, len(highs) - window):
        # Swing high: highest в прозореца
        if highs[i] == max(highs[i - window:i + window + 1]):
            swing_highs.append(highs[i])
        # Swing low: lowest в прозореца
        if lows[i] == min(lows[i - window:i + window + 1]):
            swing_lows.append(lows[i])

    # Намираме най-близкия support (под цената)
    supports_below = [s for s in swing_lows if s < current]
    support = max(supports_below) if supports_below else None

    # Намираме най-близкия resistance (над цената)
    resistances_above = [r for r in swing_highs if r > current]
    resistance = min(resistances_above) if resistances_above else None

    return {
        "support": support,
        "resistance": resistance,
        "all_supports": sorted(supports_below, reverse=True)[:3],
        "all_resistances": sorted(resistances_above)[:3],
    }


# ──────────────────────────────────────────────
# SIGNAL DIRECTION (4-компонентен counter)
# ──────────────────────────────────────────────
def calc_signal_direction(
    current_price: float,
    rsi: Optional[float],
    ema20: Optional[float],
    ema50: Optional[float],
    volume_trend: str,
    sr: dict,
    weights: dict,
) -> dict:
    """
    Bullish/bearish counter от 4 компонента с динамични тегла.
    Връща: {"direction": "BUY"|"SELL"|"NEUTRAL", "bullish": float, "bearish": float}
    """
    bullish = 0.0
    bearish = 0.0

    w_sr  = weights.get("sr_weight", 1.0)
    w_rsi = weights.get("rsi_weight", 1.0)
    w_ema = weights.get("ema_weight", 1.0)
    w_vol = weights.get("volume_weight", 1.0)

    # 1. S/R distance
    support    = sr.get("support")
    resistance = sr.get("resistance")
    if support and resistance:
        dist_to_sup = abs(current_price - support) / current_price
        dist_to_res = abs(resistance - current_price) / current_price
        if dist_to_sup < dist_to_res:
            bullish += 1.0 * w_sr   # ближо до support → bullish
        else:
            bearish += 1.0 * w_sr

    # 2. RSI зони
    if rsi is not None:
        if rsi < 40:
            bullish += 1.0 * w_rsi
        elif rsi > 60:
            bearish += 1.0 * w_rsi
        # 40-60 = neutral, не добавя

    # 3. EMA crossover
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            bullish += 1.0 * w_ema
        elif ema20 < ema50:
            bearish += 1.0 * w_ema

    # 4. Volume confirmation
    if volume_trend == "high":
        # Висок volume подсилва доминиращата посока
        if bullish > bearish:
            bullish += 0.5 * w_vol
        elif bearish > bullish:
            bearish += 0.5 * w_vol
    elif volume_trend == "low":
        # Нисък volume отслабва сигнала
        bullish *= 0.85
        bearish *= 0.85

    total = bullish + bearish
    if total == 0:
        return {"direction": "NEUTRAL", "bullish": 0.0, "bearish": 0.0}

    dominance = abs(bullish - bearish) / total

    if bullish > bearish and dominance > 0.15:
        direction = "BUY"
    elif bearish > bullish and dominance > 0.15:
        direction = "SELL"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "bullish": round(bullish, 3),
        "bearish": round(bearish, 3),
        "dominance": round(dominance, 3),
    }


# ──────────────────────────────────────────────
# TP / SL / R:R ИЗЧИСЛЕНИЯ
# ──────────────────────────────────────────────
def calc_tp_sl(
    asset: str,
    direction: str,
    current_price: float,
    atr: float,
    sr: dict,
) -> Optional[dict]:
    """
    TP и SL се изчисляват изцяло от пазарната структура.
    Никога не форсираме R/R — показваме реалното число.
    Връща None ако R/R е под 1.5 (сигналът не си заслужава риска).

    BUY:
      SL = current - max(min_sl_dist, min(dist_to_support*1.1, atr))
      TP = min(resistance, current + 3*ATR) — реалното ниво, не форсирано

    SELL:
      SL = current + max(min_sl_dist, min(dist_to_resistance*1.1, atr))
      TP = max(support, current - 3*ATR)
    """
    MIN_VIABLE_RR = 1.5   # под това не публикуваме сигнал

    min_sl_pct = MIN_SL_PCT.get(asset, 0.012)
    min_sl_dist = current_price * min_sl_pct

    support    = sr.get("support")
    resistance = sr.get("resistance")

    if direction == "BUY":
        # SL — от пазарната структура
        dist_to_support = abs(current_price - support) if support else atr
        raw_sl_dist = min(dist_to_support * 1.1, atr)
        sl_dist = max(min_sl_dist, raw_sl_dist)
        sl = current_price - sl_dist

        # TP — реалното следващо ниво
        atr_tp = current_price + TP_ATR_MULTIPLIER * atr
        if resistance and resistance > current_price:
            tp = min(resistance, atr_tp)
        else:
            tp = atr_tp

        risk   = current_price - sl
        reward = tp - current_price

    else:  # SELL
        # SL
        dist_to_resistance = abs(resistance - current_price) if resistance else atr
        raw_sl_dist = min(dist_to_resistance * 1.1, atr)
        sl_dist = max(min_sl_dist, raw_sl_dist)
        sl = current_price + sl_dist

        # TP
        atr_tp = current_price - TP_ATR_MULTIPLIER * atr
        if support and support < current_price:
            tp = max(support, atr_tp)
        else:
            tp = atr_tp

        risk   = sl - current_price
        reward = current_price - tp

    # Реалното R/R — без форсиране
    if risk <= 0:
        return None

    rr_raw = reward / risk
    rr = math.floor(rr_raw * 10) / 10   # закръгляме до 1 десетична

    # Блокираме сигнала ако R/R не си заслужава
    if rr_raw < MIN_VIABLE_RR:
        logger.info(
            "%s %s skipped — R/R %.2f below minimum %.1f",
            asset, direction, rr_raw, MIN_VIABLE_RR
        )
        return None

    return {
        "entry": round(current_price, 4),
        "tp": round(tp, 4),
        "sl": round(sl, 4),
        "risk": round(risk, 4),
        "reward": round(reward, 4),
        "rr": rr,
        "atr": round(atr, 4),
    }


# ──────────────────────────────────────────────
# FULL ANALYSIS — ЕДИННА ТОЧКА ЗА ВЛИЗАНЕ
# ──────────────────────────────────────────────
def run_analysis(asset: str, ohlc: dict, current_price: float, weights: dict) -> Optional[dict]:
    """
    Изчислява всички индикатори и връща пълен analysis dict.
    Връща None ако данните са недостатъчни.
    """
    closes  = ohlc.get("closes", [])
    highs   = ohlc.get("highs", [])
    lows    = ohlc.get("lows", [])
    volumes = ohlc.get("volumes", [])

    if len(closes) < EMA_LONG + 5:
        logger.warning("Insufficient OHLC data for %s: %d bars", asset, len(closes))
        return None

    # Изчисления
    ema20_series = calc_ema(closes, EMA_SHORT)
    ema50_series = calc_ema(closes, EMA_LONG)
    rsi_series   = calc_rsi(closes)
    atr_series   = calc_atr(highs, lows, closes)

    # Последни стойности
    ema20 = next((v for v in reversed(ema20_series) if v is not None), None)
    ema50 = next((v for v in reversed(ema50_series) if v is not None), None)
    rsi   = next((v for v in reversed(rsi_series)   if v is not None), None)
    atr   = next((v for v in reversed(atr_series)   if v is not None), None)

    if atr is None or atr == 0:
        logger.warning("Could not calculate ATR for %s", asset)
        return None

    volume_trend = calc_volume_trend(volumes)
    sr = find_support_resistance(highs, lows, closes)
    signal = calc_signal_direction(
        current_price, rsi, ema20, ema50, volume_trend, sr, weights
    )

    tp_sl = None
    if signal["direction"] != "NEUTRAL":
        tp_sl = calc_tp_sl(asset, signal["direction"], current_price, atr, sr)

    return {
        "asset": asset,
        "current_price": current_price,
        "rsi": rsi,
        "ema20": ema20,
        "ema50": ema50,
        "atr": atr,
        "volume_trend": volume_trend,
        "support": sr.get("support"),
        "resistance": sr.get("resistance"),
        "all_supports": sr.get("all_supports", []),
        "all_resistances": sr.get("all_resistances", []),
        "direction": signal["direction"],
        "bullish_score": signal["bullish"],
        "bearish_score": signal["bearish"],
        "dominance": signal.get("dominance", 0.0),
        "tp_sl": tp_sl,
    }


# ──────────────────────────────────────────────
# MULTI-TIMEFRAME ANALYSIS
# ──────────────────────────────────────────────
def run_mtf_analysis(
    asset: str,
    ohlc_daily: dict,
    ohlc_4h: Optional[dict],
    current_price: float,
    weights: dict,
) -> Optional[dict]:
    """
    Комбинира daily и 4H анализ.

    Логика:
    - Daily дава посоката и TP/SL (primary)
    - 4H потвърждава или предупреждава (filter)

    MTF alignment:
      ALIGNED   — daily и 4H в една посока → confidence +10, публикуваме
      CONFLICT  — daily BUY, 4H SELL (или обратно) → confidence -15,
                  добавяме предупреждение в поста
      NO_4H     — нямаме 4H данни → само daily, без penalty

    Връща обогатен analysis dict с mtf_alignment поле.
    """
    # Primary: daily анализ
    daily = run_analysis(asset, ohlc_daily, current_price, weights)
    if not daily:
        return None

    # Ако нямаме 4H — връщаме daily без penalty
    if not ohlc_4h or len(ohlc_4h.get("closes", [])) < 10:
        daily["mtf_alignment"] = "NO_4H"
        daily["mtf_4h_direction"] = None
        daily["mtf_confidence_adj"] = 0
        return daily

    # 4H анализ — само за direction, не за TP/SL
    closes_4h  = ohlc_4h["closes"]
    highs_4h   = ohlc_4h["highs"]
    lows_4h    = ohlc_4h["lows"]
    volumes_4h = ohlc_4h["volumes"]

    ema20_4h_s = calc_ema(closes_4h, EMA_SHORT)
    ema50_4h_s = calc_ema(closes_4h, EMA_LONG)
    rsi_4h_s   = calc_rsi(closes_4h)

    ema20_4h = next((v for v in reversed(ema20_4h_s) if v is not None), None)
    ema50_4h = next((v for v in reversed(ema50_4h_s) if v is not None), None)
    rsi_4h   = next((v for v in reversed(rsi_4h_s)   if v is not None), None)

    vol_trend_4h = calc_volume_trend(volumes_4h)
    sr_4h = find_support_resistance(highs_4h, lows_4h, closes_4h)

    signal_4h = calc_signal_direction(
        current_price, rsi_4h, ema20_4h, ema50_4h,
        vol_trend_4h, sr_4h, weights,
    )
    direction_4h = signal_4h["direction"]
    direction_daily = daily["direction"]

    # Определяме alignment
    if direction_4h == "NEUTRAL":
        # 4H neutral — не пречи на daily сигнала
        alignment = "NEUTRAL_4H"
        confidence_adj = 0
    elif direction_4h == direction_daily:
        # И двата timeframe в една посока — силен сигнал
        alignment = "ALIGNED"
        confidence_adj = +10
    else:
        # Противоречие — daily казва едно, 4H казва друго
        alignment = "CONFLICT"
        confidence_adj = -15

    daily["mtf_alignment"] = alignment
    daily["mtf_4h_direction"] = direction_4h
    daily["mtf_4h_rsi"] = rsi_4h
    daily["mtf_4h_ema20"] = ema20_4h
    daily["mtf_4h_ema50"] = ema50_4h
    daily["mtf_confidence_adj"] = confidence_adj

    return daily


def mtf_alignment_context(analysis: dict) -> str:
    """
    Превежда MTF alignment в plain-English context за Claude prompt.
    """
    alignment = analysis.get("mtf_alignment", "NO_4H")
    direction = analysis.get("direction", "")
    dir_4h    = analysis.get("mtf_4h_direction", "")

    if alignment == "ALIGNED":
        if direction == "BUY":
            return (
                "Both the daily and shorter-term picture are pointing the same way — "
                "buyers are in control across timeframes. This adds conviction to the setup."
            )
        else:
            return (
                "Both the daily and shorter-term picture are pointing the same way — "
                "sellers are in control across timeframes. This adds conviction to the setup."
            )

    elif alignment == "CONFLICT":
        if direction == "BUY":
            return (
                f"Worth noting: the daily picture favors buyers, but the shorter-term "
                f"chart is telling a different story ({dir_4h}). "
                f"We still like the setup but sizing down makes sense here — "
                f"wait for the shorter timeframe to confirm before going full size."
            )
        else:
            return (
                f"Worth noting: the daily picture favors sellers, but the shorter-term "
                f"chart isn't confirming yet ({dir_4h}). "
                f"We still see the risk, but the move may take longer to develop. "
                f"Patience here."
            )

    elif alignment == "NEUTRAL_4H":
        return "Shorter-term chart is sitting on the fence — daily picture leads."

    return ""
