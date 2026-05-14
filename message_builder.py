"""
message_builder.py — Raw Brief Bot
Строи Claude prompts и форматира Telegram постовете.
Plain English tone — без forbidden думи в анализа.
Три сесии + flash alerts + weekly recap.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    ASSETS,
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    FORBIDDEN_ANALYSIS_WORDS,
)
from data_fetcher import fetch_all_prices, fetch_fear_greed, format_price, calculate_price_change
from news_fetcher import (
    fetch_all_news,
    filter_crypto_news,
    filter_equity_news,
    news_to_prompt_context,
    get_macro_warning,
    format_upcoming_events,
    detect_upcoming_events,
)
from signal_engine import build_all_signals, is_weekend_locked, signal_to_prompt_context
from self_learning import (
    get_todays_results_summary,
    get_weekly_accuracy_summary,
    save_morning_predictions,
)
from database import get_session_snapshot, save_session_snapshot

logger = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ──────────────────────────────────────────────
# CLAUDE CALL
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """You are the voice of Raw Brief — a Telegram channel for market signals.
You write like an experienced trader talking directly with a friend.
Tone: confident, warm, a story behind every signal. Use "we". 
Acknowledge uncertainty when it exists. Explain WHY, not just what.
Never talk down to the reader. Be consistent with the signal — BUY = optimistic, SELL = cautious.

STRICT RULES:
- Never use these words in your analysis: RSI, EMA, ATR, support, resistance, overbought, oversold, momentum, trendline, crossover, breakout, retest, confluence, divergence, indicators
- Translate everything to plain English: "been running too hot" not "overbought", "the $76K floor" not "support at $76K", "sellers keep showing up" not "resistance"
- Keep each asset section tight — 3-5 sentences max
- Always include Entry, TP, SL, R/R on their own lines
- Use the exact format shown in the template
- Write in English"""


def _call_claude(prompt: str) -> str:
    """Вика Claude API, връща text response."""
    try:
        msg = _claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else ""
        # Проверка за forbidden думи (logging само)
        _check_forbidden_words(text)
        return text
    except Exception as e:
        logger.error("Claude API error: %s", e)
        return ""


def _check_forbidden_words(text: str) -> None:
    """Логва ако Claude е използвал забранена дума."""
    for word in FORBIDDEN_ANALYSIS_WORDS:
        if word.lower() in text.lower():
            logger.warning("Forbidden word '%s' found in Claude output", word)


# ──────────────────────────────────────────────
# PRICE FORMAT HELPERS
# ──────────────────────────────────────────────
def _fmt(asset: str, price: float) -> str:
    return format_price(asset, price)


def _pct(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def _emoji_direction(direction: str) -> str:
    return "🟢" if direction == "BUY" else "🔴"


def _confidence_bar(conf: int) -> str:
    filled = round(conf / 10)
    empty = 10 - filled
    return "█" * filled + "░" * empty + f" {conf}%"


# ──────────────────────────────────────────────
# СУТРЕШЕН ПОСТ (08:00 UTC)
# ──────────────────────────────────────────────
def build_morning_post() -> str:
    """
    Forward-looking сутрешен пост с всички сигнали.
    Записва snapshot + предикции в Supabase.
    """
    weekday = datetime.now(timezone.utc).weekday()
    prices = fetch_all_prices()
    fear_greed = fetch_fear_greed()
    news = fetch_all_news(max_age_hours=8)
    macro_warning = get_macro_warning(news)

    # Snapshot за price movement context
    save_session_snapshot("morning", prices)

    # Сигнали
    signals = build_all_signals()
    # Записваме предикции в Supabase
    save_morning_predictions(signals)

    # Строим prompt
    date_str = datetime.now(timezone.utc).strftime("%A, %B %d")
    news_ctx = news_to_prompt_context(filter_crypto_news(news) + filter_equity_news(news))

    asset_contexts = []
    weekend_locked_assets = []

    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil", "DXY"]:
        if is_weekend_locked(asset, weekday):
            weekend_locked_assets.append(asset)
            continue

        signal = signals.get(asset)
        price = prices.get(asset)

        if signal:
            ctx = signal_to_prompt_context(signal)
            asset_contexts.append(ctx)
        elif price:
            asset_contexts.append(
                f"Asset: {asset}\nDirection: NEUTRAL\nCurrent price: {price}\n"
                f"No clear signal — market is in wait-and-see mode"
            )

    fg_str = ""
    if fear_greed:
        fg_str = f"Market sentiment: Fear & Greed Index at {fear_greed['value']} ({fear_greed['label']})"

    locked_str = ""
    if weekend_locked_assets:
        locked_str = f"Weekend — markets closed: {', '.join(weekend_locked_assets)}"

    prompt = f"""Today is {date_str}. Write the morning market briefing for Raw Brief Telegram channel.

{fg_str}
{locked_str}

ASSET SIGNALS:
{chr(10).join(asset_contexts)}

NEWS CONTEXT:
{news_ctx}

FORMAT — use exactly this structure:

🌅 Good morning. Here's what we're watching today.

[For each asset with a signal:]
[EMOJI] [ASSET NAME]
[2-3 sentence setup explaining the situation in plain English — what the market has been doing, why this level matters, what we expect]
Direction: [BUY/SELL] {_emoji_direction('BUY')}
Entry: [price]
TP: [price] | SL: [price]
R/R: 1:[ratio] | Confidence: [X]%
[confidence bar using █ and ░]

[For NEUTRAL assets — 1 sentence why we're watching but not trading]

[If macro events detected — add warning]

[End with 1-2 sentences about what to watch today]

Write warm, direct, human. Never use forbidden technical terms."""

    if macro_warning:
        prompt += f"\n\nIMPORTANT: Include this macro warning: {macro_warning}"

    post = _call_claude(prompt)

    if not post:
        post = _fallback_morning_post(prices, signals, fear_greed, weekday)

    return post


def _fallback_morning_post(prices: dict, signals: dict, fear_greed, weekday: int) -> str:
    """Резервен пост без Claude ако API fail."""
    lines = [f"🌅 Morning Brief — {datetime.now(timezone.utc).strftime('%b %d, %Y')}\n"]

    if fear_greed:
        lines.append(f"Sentiment: Fear & Greed {fear_greed['value']} ({fear_greed['label']})\n")

    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        if is_weekend_locked(asset, weekday):
            lines.append(f"{ASSETS[asset]['emoji']} {ASSETS[asset]['name']}: 🔒 Markets closed")
            continue

        price = prices.get(asset)
        signal = signals.get(asset)
        emoji = ASSETS[asset]["emoji"]
        name = ASSETS[asset]["name"]

        if signal and signal["direction"] != "NEUTRAL":
            direction = signal["direction"]
            dir_emoji = _emoji_direction(direction)
            lines.append(
                f"{emoji} {name}\n"
                f"Direction: {direction} {dir_emoji}\n"
                f"Entry: {_fmt(asset, signal['entry'])} | "
                f"TP: {_fmt(asset, signal['take_profit'])} | "
                f"SL: {_fmt(asset, signal['stop_loss'])}\n"
                f"R/R: 1:{signal['rr_ratio']} | Confidence: {signal['confidence']}%"
            )
        elif price:
            lines.append(f"{emoji} {name}: {_fmt(asset, price)} — watching")

    return "\n\n".join(lines)


# ──────────────────────────────────────────────
# ОБЕДЕН ПОСТ (13:00 UTC)
# ──────────────────────────────────────────────
def build_midday_post() -> str:
    """
    Обеден пост с price movement context от сутринта.
    """
    weekday = datetime.now(timezone.utc).weekday()
    prices = fetch_all_prices()
    morning_snapshot = get_session_snapshot("morning")
    news = fetch_all_news(max_age_hours=6)

    # Snapshot за вечерния пост
    save_session_snapshot("midday", prices)

    # Price changes от сутринта
    price_changes = {}
    for asset, current in prices.items():
        ref = morning_snapshot.get(asset)
        if ref:
            price_changes[asset] = calculate_price_change(current, ref)

    # Нови сигнали (midday може да има различна посока)
    signals = build_all_signals()
    fear_greed = fetch_fear_greed()

    asset_contexts = []
    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        if is_weekend_locked(asset, weekday):
            continue

        price = prices.get(asset)
        change = price_changes.get(asset, {})
        signal = signals.get(asset)

        ctx_parts = []
        if price:
            change_str = ""
            if change:
                pct = change.get("pct", 0)
                direction = change.get("direction", "flat")
                change_str = f" ({_pct(pct)} since morning, moving {direction})"
            ctx_parts.append(f"{asset}: {_fmt(asset, price)}{change_str}")

        if signal and signal["direction"] != "NEUTRAL":
            ctx_parts.append(signal_to_prompt_context(signal))

        if ctx_parts:
            asset_contexts.append("\n".join(ctx_parts))

    fg_str = f"Fear & Greed: {fear_greed['value']} ({fear_greed['label']})" if fear_greed else ""
    news_ctx = news_to_prompt_context(news, max_articles=5)

    prompt = f"""Write the midday market update for Raw Brief. Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}.

{fg_str}

PRICE ACTION SINCE MORNING:
{chr(10).join(asset_contexts)}

LATEST NEWS:
{news_ctx}

FORMAT:
☀️ Midday check-in.

[For each asset — 1-2 sentences on what has moved and why it matters]
[Highlight the biggest mover and give context]
[Update any signals that have changed]
[If signals are holding — confirm or add context]

[2-3 sentences on the broader picture — is the morning thesis playing out?]

Keep it tight. This is a check-in, not a full briefing. Warm, direct, human."""

    post = _call_claude(prompt)

    if not post:
        post = _fallback_midday_post(prices, price_changes, signals, weekday)

    return post


def _fallback_midday_post(prices, changes, signals, weekday) -> str:
    lines = [f"☀️ Midday Update — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"]
    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        if is_weekend_locked(asset, weekday):
            continue
        price = prices.get(asset)
        change = changes.get(asset, {})
        if price:
            pct = change.get("pct", 0)
            lines.append(f"{ASSETS[asset]['emoji']} {asset}: {_fmt(asset, price)} ({_pct(pct)} since morning)")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# ВЕЧЕРЕН ПОСТ (20:00 UTC)
# ──────────────────────────────────────────────
def build_evening_post() -> str:
    """
    Вечерен пост с TODAY'S RESULTS и economic events за утре.
    """
    weekday = datetime.now(timezone.utc).weekday()
    prices = fetch_all_prices()
    midday_snapshot = get_session_snapshot("midday")
    morning_snapshot = get_session_snapshot("morning")
    news = fetch_all_news(max_age_hours=12)

    # Price changes от обед
    price_changes_midday = {}
    for asset, current in prices.items():
        ref = midday_snapshot.get(asset)
        if ref:
            price_changes_midday[asset] = calculate_price_change(current, ref)

    # Price changes от сутринта (day total)
    price_changes_morning = {}
    for asset, current in prices.items():
        ref = morning_snapshot.get(asset)
        if ref:
            price_changes_morning[asset] = calculate_price_change(current, ref)

    # Today's results
    results = get_todays_results_summary()

    # Upcoming events
    upcoming = detect_upcoming_events(news)
    events_str = format_upcoming_events(upcoming)

    fear_greed = fetch_fear_greed()
    fg_str = f"Fear & Greed: {fear_greed['value']} ({fear_greed['label']})" if fear_greed else ""

    # Резултати context
    results_ctx = _format_results_context(results)

    # Price context
    price_ctx = []
    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        if is_weekend_locked(asset, weekday):
            continue
        price = prices.get(asset)
        change_day = price_changes_morning.get(asset, {})
        if price:
            pct = change_day.get("pct", 0)
            price_ctx.append(f"{asset}: {_fmt(asset, price)} ({_pct(pct)} on the day)")

    prompt = f"""Write the evening wrap-up for Raw Brief. Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}.

{fg_str}

DAY'S PRICE ACTION:
{chr(10).join(price_ctx)}

TODAY'S SIGNAL RESULTS:
{results_ctx}

UPCOMING EVENTS:
{events_str}

FORMAT:
🌙 Evening wrap.

[2-3 sentences on how the day played out — biggest movers, themes]

📊 Today's Results:
[For each verified signal — did it work? Be honest and direct]
[Overall accuracy line: "X out of Y signals in profit"]

[If accuracy was low — acknowledge it, explain what the market did differently]
[If accuracy was high — celebrate it but stay grounded]

{events_str}

[Close with 1-2 sentences setting up tomorrow]

Honest, warm, never defensive about wrong calls. A trader who respects his readers."""

    post = _call_claude(prompt)

    if not post:
        post = _fallback_evening_post(prices, price_changes_morning, results, events_str, weekday)

    return post


def _format_results_context(results: dict) -> str:
    if results.get("total", 0) == 0:
        return "No signals to verify today."

    lines = [
        f"Total signals: {results['total']}",
        f"Hit TP: {results['hit_tp']}",
        f"Hit SL: {results['hit_sl']}",
        f"In profit: {results['in_profit']}",
        f"Accuracy: {results['accuracy']:.0%}",
    ]
    for asset, preds in results.get("by_asset", {}).items():
        for p in preds:
            outcome = "✅ TP hit" if p.get("hit_tp") else ("❌ SL hit" if p.get("hit_sl") else ("📈 In profit" if p.get("in_profit") else "📉 In loss"))
            entry = p.get("entry_price", 0)
            exit_p = p.get("exit_price", 0)
            lines.append(f"{asset} {p['direction']}: entry {entry:.2f} → exit {exit_p:.2f} — {outcome}")

    return "\n".join(lines)


def _fallback_evening_post(prices, changes, results, events_str, weekday) -> str:
    lines = [f"🌙 Evening Wrap — {datetime.now(timezone.utc).strftime('%b %d')}\n"]

    for asset in ["BTC", "ETH", "SPX", "Gold", "Silver", "Oil"]:
        if is_weekend_locked(asset, weekday):
            continue
        price = prices.get(asset)
        change = changes.get(asset, {})
        if price:
            pct = change.get("pct", 0)
            lines.append(f"{ASSETS[asset]['emoji']} {asset}: {_fmt(asset, price)} ({_pct(pct)} today)")

    if results.get("total", 0) > 0:
        lines.append(f"\n📊 Today's Results: {results['in_profit']}/{results['total']} in profit ({results['accuracy']:.0%})")

    lines.append(f"\n{events_str}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# FLASH ALERT
# ──────────────────────────────────────────────
def build_flash_alert(asset: str, current_price: float, pct_change: float, direction: str) -> str:
    """
    Flash alert при голямо движение.
    Включва исторически контекст и key level.
    """
    from database import log_flash_alert
    log_flash_alert(asset, direction, current_price, pct_change)

    signal = None
    try:
        from signal_engine import build_signal
        signal = build_signal(asset)
    except Exception:
        pass

    sign = "🚀" if direction == "up" else "🔻"
    move_desc = f"+{pct_change:.1f}%" if direction == "up" else f"{pct_change:.1f}%"

    prompt = f"""Write a flash alert for Raw Brief Telegram channel.

Asset: {asset} ({ASSETS[asset]['name']})
Current price: {_fmt(asset, current_price)}
Move: {move_desc} in the last hour
Direction: {direction}

{f"Signal context: {signal_to_prompt_context(signal)}" if signal else ""}

FORMAT:
{sign} FLASH: {ASSETS[asset]['name']} {move_desc}

[1-2 sentences on what just happened and why it's significant]
[Historical context: "Last time we saw a move like this was..."]
[Key level to watch now]
[If we have a signal — include Entry/TP/SL]

Short, punchy, actionable. Warm but urgent."""

    post = _call_claude(prompt)

    if not post:
        post = (
            f"{sign} FLASH: {ASSETS[asset]['name']} {move_desc}\n\n"
            f"{ASSETS[asset]['emoji']} {asset} just moved {move_desc} to {_fmt(asset, current_price)}.\n"
            f"Stay alert — big moves need big context."
        )

    return post


# ──────────────────────────────────────────────
# FEAR & GREED ALERT
# ──────────────────────────────────────────────
def build_fear_greed_alert(fg_value: int, fg_label: str) -> str:
    prompt = f"""Write a Fear & Greed alert for Raw Brief.

Fear & Greed Index: {fg_value} ({fg_label})
This is below 15 — extreme fear territory.

[2-3 sentences on what extreme fear historically means for crypto markets]
[What we're watching for a potential reversal]
[Measured, not hype — this is information, not a guaranteed buy signal]

Warm, experienced trader voice. Short and direct."""

    post = _call_claude(prompt)

    if not post:
        post = (
            f"😨 Fear & Greed at {fg_value} — {fg_label}\n\n"
            f"Markets are deeply fearful right now. Historically, extreme fear has preceded recoveries — "
            f"but timing the bottom is never easy. We're watching closely."
        )

    return post


# ──────────────────────────────────────────────
# WEEKLY RECAP (Sunday 22:00 UTC)
# ──────────────────────────────────────────────
def build_weekly_recap() -> str:
    """Weekly recap с accuracy по актив и overall."""
    summary = get_weekly_accuracy_summary()
    prices = fetch_all_prices()
    fear_greed = fetch_fear_greed()

    overall_acc = summary.get("overall_accuracy", 0)
    overall_total = summary.get("overall_total", 0)
    overall_correct = summary.get("overall_correct", 0)
    by_asset = summary.get("by_asset", {})

    asset_accuracy_lines = []
    for asset, stats in sorted(by_asset.items()):
        total = stats.get("total", 0)
        correct = stats.get("correct", 0)
        acc = stats.get("accuracy", 0)
        emoji = ASSETS.get(asset, {}).get("emoji", "•")
        asset_accuracy_lines.append(
            f"{emoji} {asset}: {correct}/{total} ({acc:.0%})"
        )

    fg_str = f"Fear & Greed: {fear_greed['value']} ({fear_greed['label']})" if fear_greed else ""

    prompt = f"""Write the weekly recap for Raw Brief Telegram channel.

Week ending: {datetime.now(timezone.utc).strftime('%B %d, %Y')}
{fg_str}

WEEKLY ACCURACY:
Overall: {overall_correct}/{overall_total} signals correct ({overall_acc:.0%})
By asset:
{chr(10).join(asset_accuracy_lines)}

FORMAT:
📅 Weekly Recap

[2-3 sentences on the week's biggest themes and market moves]

📊 Signal Accuracy This Week:
[List each asset accuracy — be honest about what worked and what didn't]
Overall: [X/Y signals] — [honest assessment]

[If good week — celebrate but stay humble]
[If rough week — acknowledge it directly, explain what conditions fooled the system]

[1-2 sentences looking ahead to next week]

Honest, warm, treats readers as equals. Never defensive."""

    post = _call_claude(prompt)

    if not post:
        lines = [
            f"📅 Weekly Recap — {datetime.now(timezone.utc).strftime('%B %d')}\n",
            f"📊 This Week's Accuracy:",
        ]
        for line in asset_accuracy_lines:
            lines.append(f"  {line}")
        lines.append(f"\nOverall: {overall_correct}/{overall_total} ({overall_acc:.0%})")
        post = "\n".join(lines)

    return post
