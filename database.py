"""
database.py — Raw Brief Bot
Supabase/PostgreSQL: connection pool, schema creation, всички CRUD операции.
"""

import json
import logging
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL, PREDICTION_HISTORY_DAYS

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5, dsn=DATABASE_URL, connect_timeout=10,
        )
        logger.info("DB pool created")
    return _pool


@contextmanager
def get_conn():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS predictions (
    id              SERIAL PRIMARY KEY,
    asset           TEXT NOT NULL,
    direction       TEXT NOT NULL,
    entry_price     DOUBLE PRECISION NOT NULL,
    take_profit     DOUBLE PRECISION NOT NULL,
    stop_loss       DOUBLE PRECISION NOT NULL,
    confidence      INTEGER NOT NULL,
    rr_ratio        INTEGER NOT NULL,
    session         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at     TIMESTAMPTZ,
    hit_tp          BOOLEAN,
    hit_sl          BOOLEAN,
    in_profit       BOOLEAN,
    exit_price      DOUBLE PRECISION,
    archived        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS learning_weights (
    id              SERIAL PRIMARY KEY,
    asset           TEXT NOT NULL UNIQUE,
    rsi_weight      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    fear_greed_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    funding_weight  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    sr_weight       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    ema_weight      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    volume_weight   DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS price_cache (
    asset           TEXT PRIMARY KEY,
    price           DOUBLE PRECISION NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS session_snapshots (
    session         TEXT PRIMARY KEY,
    data            TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_accuracy (
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    asset           TEXT NOT NULL,
    total           INTEGER NOT NULL DEFAULT 0,
    correct         INTEGER NOT NULL DEFAULT 0,
    accuracy        DOUBLE PRECISION,
    UNIQUE(date, asset)
);

CREATE TABLE IF NOT EXISTS flash_alert_log (
    id              SERIAL PRIMARY KEY,
    asset           TEXT NOT NULL,
    direction       TEXT NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    pct_change      DOUBLE PRECISION NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_predictions_asset_created
    ON predictions(asset, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_archived
    ON predictions(archived, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_daily_accuracy_date
    ON daily_accuracy(date DESC);
"""


def init_schema() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    logger.info("Schema initialized")


def save_prediction(asset, direction, entry_price, take_profit, stop_loss, confidence, rr_ratio, session) -> int:
    sql = """
        INSERT INTO predictions
            (asset, direction, entry_price, take_profit, stop_loss, confidence, rr_ratio, session)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (asset, direction, entry_price, take_profit, stop_loss, confidence, rr_ratio, session))
            return cur.fetchone()[0]


def get_unverified_predictions(older_than_hours: int = 8) -> list:
    sql = """
        SELECT id, asset, direction, entry_price, take_profit, stop_loss,
               confidence, rr_ratio, session, created_at
        FROM predictions
        WHERE verified_at IS NULL AND archived = FALSE
          AND created_at < NOW() - INTERVAL '%s hours'
        ORDER BY created_at ASC
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (older_than_hours,))
            return [dict(r) for r in cur.fetchall()]


def verify_prediction(prediction_id, hit_tp, hit_sl, in_profit, exit_price) -> None:
    sql = """
        UPDATE predictions
        SET verified_at = NOW(), hit_tp = %s, hit_sl = %s, in_profit = %s, exit_price = %s
        WHERE id = %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (hit_tp, hit_sl, in_profit, exit_price, prediction_id))


def get_todays_verified_predictions() -> list:
    sql = """
        SELECT asset, direction, entry_price, take_profit, stop_loss,
               hit_tp, hit_sl, in_profit, exit_price, confidence, rr_ratio
        FROM predictions
        WHERE verified_at::date = CURRENT_DATE AND archived = FALSE
        ORDER BY asset, verified_at
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def archive_old_predictions() -> int:
    sql = """
        UPDATE predictions SET archived = TRUE
        WHERE created_at < NOW() - INTERVAL '%s days' AND archived = FALSE
        RETURNING id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (PREDICTION_HISTORY_DAYS,))
            return len(cur.fetchall())


def get_predictions_for_period(days: int = 7) -> list:
    sql = """
        SELECT asset, direction, hit_tp, hit_sl, in_profit,
               entry_price, exit_price, confidence, created_at
        FROM predictions
        WHERE created_at >= NOW() - INTERVAL '%s days'
          AND verified_at IS NOT NULL AND archived = FALSE
        ORDER BY asset, created_at
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (days,))
            return [dict(r) for r in cur.fetchall()]


def get_weights(asset: str) -> dict:
    sql = """
        SELECT rsi_weight, fear_greed_weight, funding_weight,
               sr_weight, ema_weight, volume_weight
        FROM learning_weights WHERE asset = %s
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (asset,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return {
                "rsi_weight": 1.0, "fear_greed_weight": 1.0,
                "funding_weight": 1.0, "sr_weight": 1.0,
                "ema_weight": 1.0, "volume_weight": 1.0,
            }


def upsert_weights(asset: str, weights: dict) -> None:
    sql = """
        INSERT INTO learning_weights
            (asset, rsi_weight, fear_greed_weight, funding_weight,
             sr_weight, ema_weight, volume_weight, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (asset) DO UPDATE SET
            rsi_weight = EXCLUDED.rsi_weight,
            fear_greed_weight = EXCLUDED.fear_greed_weight,
            funding_weight = EXCLUDED.funding_weight,
            sr_weight = EXCLUDED.sr_weight,
            ema_weight = EXCLUDED.ema_weight,
            volume_weight = EXCLUDED.volume_weight,
            updated_at = NOW()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                asset,
                weights.get("rsi_weight", 1.0), weights.get("fear_greed_weight", 1.0),
                weights.get("funding_weight", 1.0), weights.get("sr_weight", 1.0),
                weights.get("ema_weight", 1.0), weights.get("volume_weight", 1.0),
            ))


def cache_price(asset: str, price: float) -> None:
    sql = """
        INSERT INTO price_cache (asset, price, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (asset) DO UPDATE SET price = EXCLUDED.price, updated_at = NOW()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (asset, price))


def get_cached_price(asset: str) -> Optional[float]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT price FROM price_cache WHERE asset = %s", (asset,))
            row = cur.fetchone()
            return row[0] if row else None


def get_all_cached_prices() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT asset, price FROM price_cache")
            return {row[0]: row[1] for row in cur.fetchall()}


def save_session_snapshot(session: str, prices: dict) -> None:
    """Записва snapshot на цените — TEXT колона, без type conflict."""
    snapshot_key = f"_snapshot_{session}"
    sql = """
        INSERT INTO session_snapshots (session, data, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (session) DO UPDATE SET
            data = EXCLUDED.data, updated_at = NOW()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (snapshot_key, json.dumps(prices)))


def get_session_snapshot(session: str) -> dict:
    """Връща snapshot на цените за дадена сесия."""
    snapshot_key = f"_snapshot_{session}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM session_snapshots WHERE session = %s", (snapshot_key,))
            row = cur.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    return {}
            return {}


def upsert_daily_accuracy(date_str: str, asset: str, total: int, correct: int) -> None:
    accuracy = correct / total if total > 0 else None
    sql = """
        INSERT INTO daily_accuracy (date, asset, total, correct, accuracy)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (date, asset) DO UPDATE SET
            total = EXCLUDED.total, correct = EXCLUDED.correct, accuracy = EXCLUDED.accuracy
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (date_str, asset, total, correct, accuracy))


def get_weekly_accuracy() -> list:
    sql = """
        SELECT asset, SUM(total) AS total, SUM(correct) AS correct, AVG(accuracy) AS avg_accuracy
        FROM daily_accuracy
        WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY asset ORDER BY asset
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def log_flash_alert(asset: str, direction: str, price: float, pct_change: float) -> None:
    sql = "INSERT INTO flash_alert_log (asset, direction, price, pct_change) VALUES (%s, %s, %s, %s)"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (asset, direction, price, pct_change))


def get_recent_flash_alerts(asset: str, within_minutes: int = 60) -> list:
    sql = """
        SELECT id, direction, price, pct_change, sent_at
        FROM flash_alert_log
        WHERE asset = %s AND sent_at > NOW() - INTERVAL '%s minutes'
        ORDER BY sent_at DESC
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (asset, within_minutes))
            return [dict(r) for r in cur.fetchall()]
