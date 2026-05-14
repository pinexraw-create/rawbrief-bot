"""
main.py — Raw Brief Bot
Entry point. APScheduler jobs: morning/midday/evening posts,
flash alerts, weekly recap, self-learning pipeline.
Railway worker process.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import telegram
import telegram.error

from config import (
    ASSETS,
    EVENING_HOUR,
    FEAR_GREED_ALERT_THRESHOLD,
    FLASH_THRESHOLDS,
    MIDDAY_HOUR,
    MORNING_HOUR,
    TELEGRAM_CHANNEL_ID,
    TELEGRAM_TOKEN,
    WEEKLY_RECAP_DAY,
    WEEKLY_RECAP_HOUR,
)
from database import get_recent_flash_alerts, init_schema
from data_fetcher import fetch_all_prices, fetch_fear_greed
from message_builder import (
    build_evening_post,
    build_fear_greed_alert,
    build_flash_alert,
    build_midday_post,
    build_morning_post,
    build_weekly_recap,
)
from news_fetcher import reset_daily_hashes
from self_learning import (
    archive_daily_predictions,
    update_learning_from_results,
    verify_predictions,
)

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("rawbrief")

# ──────────────────────────────────────────────
# TELEGRAM BOT
# ──────────────────────────────────────────────
_bot = telegram.Bot(token=TELEGRAM_TOKEN)

MAX_MESSAGE_LENGTH = 4096  # Telegram limit


def send_message(text: str, channel_id: str = TELEGRAM_CHANNEL_ID) -> bool:
    """
    Праща съобщение в Telegram channel.
    PTB 21.x е async — използваме asyncio.run() за sync context.
    """
    if not text or not text.strip():
        logger.warning("Attempted to send empty message")
        return False

    parts = _split_message(text)
    success = True

    for i, part in enumerate(parts):
        try:
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                _bot.send_message(
                    chat_id=channel_id,
                    text=part,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            )
            if len(parts) > 1:
                logger.info("Sent part %d/%d (%d chars)", i + 1, len(parts), len(part))
            time.sleep(0.5)
        except Exception as e:
            logger.warning("HTML parse error, retrying plain: %s", e)
            try:
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    _bot.send_message(chat_id=channel_id, text=part)
                )
            except Exception as e2:
                logger.error("Failed to send message part %d: %s", i + 1, e2)
                success = False

    return success


def _split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list:
    """Разбива дълго съобщение на части по \n\n."""
    if len(text) <= limit:
        return [text]

    parts = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= limit:
            current += ("" if not current else "\n\n") + paragraph
        else:
            if current:
                parts.append(current)
            current = paragraph
    if current:
        parts.append(current)

    return parts if parts else [text[:limit]]


# ──────────────────────────────────────────────
# JOB: MORNING POST
# ──────────────────────────────────────────────
def job_morning_post() -> None:
    logger.info("=== MORNING POST JOB START ===")
    reset_daily_hashes()
    try:
        post = build_morning_post()
        if post:
            success = send_message(post)
            logger.info("Morning post sent: %s (%d chars)", success, len(post))
        else:
            logger.error("Morning post build returned empty")
    except Exception as e:
        logger.error("Morning post job failed: %s", e, exc_info=True)


# ──────────────────────────────────────────────
# JOB: MIDDAY POST
# ──────────────────────────────────────────────
def job_midday_post() -> None:
    logger.info("=== MIDDAY POST JOB START ===")
    try:
        post = build_midday_post()
        if post:
            success = send_message(post)
            logger.info("Midday post sent: %s (%d chars)", success, len(post))
        else:
            logger.error("Midday post build returned empty")
    except Exception as e:
        logger.error("Midday post job failed: %s", e, exc_info=True)


# ──────────────────────────────────────────────
# JOB: EVENING POST + SELF-LEARNING
# ──────────────────────────────────────────────
def job_evening_post() -> None:
    logger.info("=== EVENING POST JOB START ===")
    try:
        # 1. Верифицираме предикциите
        verify_predictions()

        # 2. Update learning weights
        update_learning_from_results()

        # 3. Archive old predictions
        archive_daily_predictions()

        # 4. Build & send post
        post = build_evening_post()
        if post:
            success = send_message(post)
            logger.info("Evening post sent: %s (%d chars)", success, len(post))
        else:
            logger.error("Evening post build returned empty")
    except Exception as e:
        logger.error("Evening post job failed: %s", e, exc_info=True)


# ──────────────────────────────────────────────
# JOB: FLASH ALERTS (every 5 minutes)
# ──────────────────────────────────────────────
# Rolling price history: {asset: [(timestamp, price), ...]}
_price_history: dict = {}


def _record_price(asset: str, price: float) -> None:
    """Записва цената с timestamp. Пазим само последния час."""
    now = time.time()
    if asset not in _price_history:
        _price_history[asset] = []
    _price_history[asset].append((now, price))
    cutoff = now - 3700
    _price_history[asset] = [(t, p) for t, p in _price_history[asset] if t > cutoff]


def _get_hourly_change(asset: str, current: float) -> Optional[float]:
    """% промяна спрямо цената преди ~1 час. None ако историята е недостатъчна."""
    history = _price_history.get(asset, [])
    if len(history) < 3:
        return None
    target_ts = time.time() - 3600
    closest = min(history, key=lambda x: abs(x[0] - target_ts))
    ref = closest[1]
    if not ref:
        return None
    return ((current - ref) / ref) * 100


def _is_volume_confirmed(asset: str) -> bool:
    """
    Проверява дали последният 4H bar има повишен volume (≥20% над предните 4).
    Ако нямаме 4H данни — пропускаме volume filter (не блокираме).
    """
    try:
        from data_fetcher import fetch_ohlc_4h
        ohlc_4h = fetch_ohlc_4h(asset)
        if not ohlc_4h:
            return True
        volumes = ohlc_4h.get("volumes", [])
        if len(volumes) < 5 or all(v == 0 for v in volumes):
            return True   # Crypto от CoinGecko няма volume — не блокираме
        last_vol = volumes[-1]
        prev_avg = sum(volumes[-5:-1]) / 4
        if prev_avg == 0:
            return True
        return (last_vol / prev_avg) >= 1.2
    except Exception:
        return True   # При грешка — не блокираме сигнала


def job_flash_alerts() -> None:
    """
    Умен flash alert с rolling hourly change + volume confirmation.
    Праща само ако:
      1. % движение за последния час ≥ threshold
      2. Volume на последния 4H bar е ≥ 20% над средното (ако имаме данни)
      3. Няма пратен alert за актива в последните 55 минути
    """
    try:
        current_prices = fetch_all_prices()
        fear_greed = fetch_fear_greed()

        # Fear & Greed alert
        if fear_greed and fear_greed.get("value", 100) < FEAR_GREED_ALERT_THRESHOLD:
            recent_fg = get_recent_flash_alerts("FEAR_GREED", within_minutes=360)
            if not recent_fg:
                post = build_fear_greed_alert(fear_greed["value"], fear_greed["label"])
                if post:
                    send_message(post)
                    logger.info("Fear & Greed alert sent: %d", fear_greed["value"])

        # Price flash alerts
        for asset, threshold in FLASH_THRESHOLDS.items():
            current = current_prices.get(asset)
            if not current:
                continue

            _record_price(asset, current)

            pct_change = _get_hourly_change(asset, current)
            if pct_change is None or abs(pct_change) < threshold:
                continue

            # Дублиран alert?
            recent = get_recent_flash_alerts(asset, within_minutes=55)
            if recent:
                logger.debug("Flash alert for %s already sent recently", asset)
                continue

            # Volume confirmation
            if not _is_volume_confirmed(asset):
                logger.info(
                    "Flash alert %s skipped — %.2f%% move not volume-confirmed",
                    asset, pct_change,
                )
                continue

            direction = "up" if pct_change > 0 else "down"
            logger.info(
                "FLASH ALERT: %s %.2f%% to %.4f (volume confirmed)",
                asset, pct_change, current,
            )
            post = build_flash_alert(asset, current, pct_change, direction)
            if post:
                send_message(post)

    except Exception as e:
        logger.error("Flash alert job failed: %s", e)


# ──────────────────────────────────────────────
# JOB: WEEKLY RECAP (Sunday 22:00 UTC)
# ──────────────────────────────────────────────
def job_weekly_recap() -> None:
    logger.info("=== WEEKLY RECAP JOB START ===")
    try:
        post = build_weekly_recap()
        if post:
            success = send_message(post)
            logger.info("Weekly recap sent: %s (%d chars)", success, len(post))
    except Exception as e:
        logger.error("Weekly recap job failed: %s", e, exc_info=True)


# ──────────────────────────────────────────────
# STARTUP CHECK
# ──────────────────────────────────────────────
def startup_checks() -> None:
    """Верифицира ENV vars, DB connection, Telegram connection."""
    errors = []

    # ENV vars
    required_env = ["TELEGRAM_TOKEN", "TELEGRAM_CHANNEL_ID", "ANTHROPIC_API_KEY", "DATABASE_URL"]
    for var in required_env:
        if not os.environ.get(var):
            errors.append(f"Missing ENV: {var}")

    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)

    # DB schema
    try:
        init_schema()
        logger.info("✅ Database connected and schema ready")
    except Exception as e:
        logger.error("❌ Database connection failed: %s", e)
        sys.exit(1)

    # Telegram
    try:
        import asyncio
        me = asyncio.get_event_loop().run_until_complete(_bot.get_me())
        logger.info("✅ Telegram connected: @%s", me.username)
    except Exception as e:
        logger.error("❌ Telegram connection failed: %s", e)
        sys.exit(1)

    # Fetch initial prices
    try:
        prices = fetch_all_prices()
        global _last_prices
        _last_prices = dict(prices)
        logger.info("✅ Initial prices loaded: %s", list(prices.keys()))
    except Exception as e:
        logger.warning("Could not load initial prices: %s", e)

    logger.info("✅ Raw Brief Bot startup complete")


# ──────────────────────────────────────────────
# SCHEDULER SETUP
# ──────────────────────────────────────────────
def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="UTC")

    # Morning post — 08:00 UTC
    scheduler.add_job(
        job_morning_post,
        CronTrigger(hour=MORNING_HOUR, minute=0, timezone="UTC"),
        id="morning_post",
        name="Morning Post",
        misfire_grace_time=300,
    )

    # Midday post — 13:00 UTC
    scheduler.add_job(
        job_midday_post,
        CronTrigger(hour=MIDDAY_HOUR, minute=0, timezone="UTC"),
        id="midday_post",
        name="Midday Post",
        misfire_grace_time=300,
    )

    # Evening post — 20:00 UTC
    scheduler.add_job(
        job_evening_post,
        CronTrigger(hour=EVENING_HOUR, minute=0, timezone="UTC"),
        id="evening_post",
        name="Evening Post",
        misfire_grace_time=300,
    )

    # Flash alerts — every 5 minutes
    scheduler.add_job(
        job_flash_alerts,
        "interval",
        minutes=5,
        id="flash_alerts",
        name="Flash Alerts",
        misfire_grace_time=60,
    )

    # Weekly recap — Sunday 22:00 UTC
    scheduler.add_job(
        job_weekly_recap,
        CronTrigger(
            day_of_week=WEEKLY_RECAP_DAY,
            hour=WEEKLY_RECAP_HOUR,
            minute=0,
            timezone="UTC",
        ),
        id="weekly_recap",
        name="Weekly Recap",
        misfire_grace_time=600,
    )

    return scheduler


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
def main() -> None:
    logger.info("=" * 50)
    logger.info("Raw Brief Bot starting...")
    logger.info("=" * 50)

    startup_checks()

    scheduler = build_scheduler()

    # Log scheduled jobs
    logger.info("Scheduled jobs:")
    for job in scheduler.get_jobs():
        logger.info("  • %s — %s", job.name, job.trigger)

    # Optional: send startup notification
    startup_env = os.environ.get("SEND_STARTUP_MESSAGE", "false").lower()
    if startup_env == "true":
        try:
            send_message(
                "🟢 Raw Brief Bot is online.\n"
                f"Morning post at {MORNING_HOUR:02d}:00 UTC | "
                f"Midday at {MIDDAY_HOUR:02d}:00 UTC | "
                f"Evening at {EVENING_HOUR:02d}:00 UTC"
            )
        except Exception:
            pass

    try:
        logger.info("Scheduler starting — bot is live")
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error("Scheduler crashed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
