"""
self_learning.py — Raw Brief Bot
Записва предикции, верифицира вечерта, коригира indicator weights.
Accuracy под 55% → вдига RSI и Fear&Greed тегло до max 2.0.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from config import (
    ACCURACY_LOW_THRESHOLD,
    ASSETS,
    WEIGHT_DEFAULT,
    WEIGHT_MAX,
    WEIGHT_MIN,
)
from database import (
    archive_old_predictions,
    get_predictions_for_period,
    get_todays_verified_predictions,
    get_unverified_predictions,
    get_weights,
    save_prediction,
    upsert_daily_accuracy,
    upsert_weights,
    verify_prediction,
)
from data_fetcher import fetch_all_prices

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# SAVE MORNING PREDICTIONS
# ──────────────────────────────────────────────
def save_morning_predictions(signals: dict) -> None:
    """
    Записва сигналите от сутрешния пост в Supabase.
    signals = {asset: signal_dict или None}
    """
    saved = 0
    for asset, signal in signals.items():
        if not signal or signal.get("direction") == "NEUTRAL":
            continue
        try:
            pred_id = save_prediction(
                asset=asset,
                direction=signal["direction"],
                entry_price=signal["entry"],
                take_profit=signal["take_profit"],
                stop_loss=signal["stop_loss"],
                confidence=signal["confidence"],
                rr_ratio=signal["rr_ratio"],
                session="morning",
            )
            logger.info("Saved prediction #%d for %s %s", pred_id, asset, signal["direction"])
            saved += 1
        except Exception as e:
            logger.error("Failed to save prediction for %s: %s", asset, e)

    logger.info("Saved %d morning predictions", saved)


# ──────────────────────────────────────────────
# VERIFY PREDICTIONS (вечерта)
# ──────────────────────────────────────────────
def verify_predictions() -> dict:
    """
    Верифицира unverified predictions от сутринта.
    Проверява дали current price е hit TP, hit SL, или in_profit/in_loss.
    Връща summary dict за вечерния пост.
    """
    unverified = get_unverified_predictions(older_than_hours=6)
    if not unverified:
        logger.info("No unverified predictions to verify")
        return {}

    current_prices = fetch_all_prices()
    results = {}

    for pred in unverified:
        asset = pred["asset"]
        direction = pred["direction"]
        entry = pred["entry_price"]
        tp = pred["take_profit"]
        sl = pred["stop_loss"]

        current = current_prices.get(asset)
        if not current:
            logger.warning("No current price for %s — skipping verification", asset)
            continue

        if direction == "BUY":
            hit_tp = current >= tp
            hit_sl = current <= sl
            in_profit = current > entry and not hit_sl
            in_loss = current < entry or hit_sl
        else:  # SELL
            hit_tp = current <= tp
            hit_sl = current >= sl
            in_profit = current < entry and not hit_sl
            in_loss = current > entry or hit_sl

        in_profit_final = in_profit and not hit_sl
        in_loss_final = in_loss or hit_sl

        try:
            verify_prediction(
                prediction_id=pred["id"],
                hit_tp=hit_tp,
                hit_sl=hit_sl,
                in_profit=in_profit_final,
                exit_price=current,
            )
        except Exception as e:
            logger.error("Verify failed for prediction #%d: %s", pred["id"], e)
            continue

        results[asset] = {
            "direction": direction,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "exit_price": current,
            "hit_tp": hit_tp,
            "hit_sl": hit_sl,
            "in_profit": in_profit_final,
        }
        logger.info(
            "Verified %s %s: hit_tp=%s hit_sl=%s in_profit=%s",
            asset, direction, hit_tp, hit_sl, in_profit_final
        )

    return results


# ──────────────────────────────────────────────
# ARCHIVE DAILY PREDICTIONS
# ──────────────────────────────────────────────
def archive_daily_predictions() -> None:
    """Архивира стари предикции (> 14 дни). Вика се вечерта."""
    count = archive_old_predictions()
    if count > 0:
        logger.info("Archived %d old predictions", count)


# ──────────────────────────────────────────────
# UPDATE LEARNING WEIGHTS
# ──────────────────────────────────────────────
def update_learning_from_results() -> None:
    """
    Анализира верифицираните предикции за деня и коригира indicator_weights.
    Правило: accuracy < 55% → вдига RSI и F&G тегло с 0.1 (до max 2.0)
             accuracy > 70% → намалява RSI и F&G тегло с 0.05 (до min 0.5)
    """
    verified = get_todays_verified_predictions()
    if not verified:
        logger.info("No verified predictions for weight update")
        return

    # Групираме по asset
    by_asset: dict = {}
    for pred in verified:
        asset = pred["asset"]
        if asset not in by_asset:
            by_asset[asset] = {"total": 0, "correct": 0}
        by_asset[asset]["total"] += 1
        if pred.get("hit_tp") or pred.get("in_profit"):
            by_asset[asset]["correct"] += 1

    # Обновяваме тегла и accuracy per asset
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for asset, stats in by_asset.items():
        total = stats["total"]
        correct = stats["correct"]
        accuracy = correct / total if total > 0 else 0.0

        # Запис в daily_accuracy
        try:
            upsert_daily_accuracy(today, asset, total, correct)
        except Exception as e:
            logger.error("Failed to upsert daily accuracy for %s: %s", asset, e)

        # Корекция на тегла
        try:
            weights = get_weights(asset)
            weights = _adjust_weights(asset, weights, accuracy)
            upsert_weights(asset, weights)
            logger.info(
                "%s accuracy %.0f%% — weights updated: RSI=%.2f F&G=%.2f",
                asset, accuracy * 100,
                weights["rsi_weight"],
                weights["fear_greed_weight"],
            )
        except Exception as e:
            logger.error("Failed to update weights for %s: %s", asset, e)


def _adjust_weights(asset: str, weights: dict, accuracy: float) -> dict:
    """
    Коригира RSI и Fear&Greed тегла спрямо accuracy.
    Другите тегла се коригират с по-малка стъпка.
    """
    w = dict(weights)

    if accuracy < ACCURACY_LOW_THRESHOLD:
        # Нисък accuracy → вдигаме RSI и F&G (те дават по-надеждни сигнали)
        w["rsi_weight"] = min(WEIGHT_MAX, w.get("rsi_weight", WEIGHT_DEFAULT) + 0.10)
        w["fear_greed_weight"] = min(WEIGHT_MAX, w.get("fear_greed_weight", WEIGHT_DEFAULT) + 0.10)
        # Намаляваме SR и EMA леко (те са дали грешни сигнали)
        w["sr_weight"] = max(WEIGHT_MIN, w.get("sr_weight", WEIGHT_DEFAULT) - 0.05)
        w["ema_weight"] = max(WEIGHT_MIN, w.get("ema_weight", WEIGHT_DEFAULT) - 0.05)

    elif accuracy > 0.70:
        # Висок accuracy → системата работи добре, малко rebalance към default
        for key in ["rsi_weight", "fear_greed_weight", "sr_weight", "ema_weight", "volume_weight"]:
            current = w.get(key, WEIGHT_DEFAULT)
            # Плавно връщане към 1.0
            if current > WEIGHT_DEFAULT:
                w[key] = max(WEIGHT_DEFAULT, current - 0.05)
            elif current < WEIGHT_DEFAULT:
                w[key] = min(WEIGHT_DEFAULT, current + 0.05)

    # Non-crypto: funding weight не се използва — оставяме на default
    if ASSETS.get(asset, {}).get("type") != "crypto":
        w["funding_weight"] = WEIGHT_DEFAULT

    # Clamp всички
    for key in w:
        if isinstance(w[key], float):
            w[key] = round(max(WEIGHT_MIN, min(WEIGHT_MAX, w[key])), 3)

    return w


# ──────────────────────────────────────────────
# TODAY'S RESULTS SUMMARY
# ──────────────────────────────────────────────
def get_todays_results_summary() -> dict:
    """
    Сглобява summary за вечерния TODAY'S RESULTS раздел.
    Връща структуриран dict.
    """
    verified = get_todays_verified_predictions()

    total = len(verified)
    if total == 0:
        return {
            "total": 0,
            "hit_tp": 0,
            "hit_sl": 0,
            "in_profit": 0,
            "accuracy": 0.0,
            "by_asset": {},
            "predictions": [],
        }

    hit_tp_count = sum(1 for p in verified if p.get("hit_tp"))
    hit_sl_count = sum(1 for p in verified if p.get("hit_sl"))
    in_profit_count = sum(1 for p in verified if p.get("in_profit"))

    correct = hit_tp_count + in_profit_count
    accuracy = correct / total if total > 0 else 0.0

    by_asset = {}
    for pred in verified:
        asset = pred["asset"]
        if asset not in by_asset:
            by_asset[asset] = []
        by_asset[asset].append(pred)

    return {
        "total": total,
        "hit_tp": hit_tp_count,
        "hit_sl": hit_sl_count,
        "in_profit": in_profit_count,
        "accuracy": round(accuracy, 3),
        "by_asset": by_asset,
        "predictions": verified,
    }


# ──────────────────────────────────────────────
# WEEKLY ACCURACY SUMMARY
# ──────────────────────────────────────────────
def get_weekly_accuracy_summary() -> dict:
    """За weekly recap неделя 22:00 UTC."""
    from database import get_weekly_accuracy
    rows = get_weekly_accuracy()

    overall_total = sum(r.get("total", 0) for r in rows)
    overall_correct = sum(r.get("correct", 0) for r in rows)
    overall_accuracy = overall_correct / overall_total if overall_total > 0 else 0.0

    by_asset = {}
    for row in rows:
        asset = row.get("asset", "")
        total = row.get("total", 0)
        correct = row.get("correct", 0)
        acc = row.get("avg_accuracy")
        if acc is None and total > 0:
            acc = correct / total
        by_asset[asset] = {
            "total": total,
            "correct": correct,
            "accuracy": round(float(acc), 3) if acc is not None else 0.0,
        }

    return {
        "overall_accuracy": round(overall_accuracy, 3),
        "overall_total": overall_total,
        "overall_correct": overall_correct,
        "by_asset": by_asset,
    }
