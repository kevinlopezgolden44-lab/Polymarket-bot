import aiohttp
import logging
from datetime import datetime

log = logging.getLogger(__name__)

from database import BOT_VERSION  # v18 clean data fork — defined in database.py

def now():
    return datetime.utcnow()

async def send_message(token, chat_id, message, reply_markup=None):
    if not token or not chat_id:
        log.warning("Telegram not configured")
        return None
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    log.info("Telegram sent")
                    return data.get("result", {}).get("message_id")
                else:
                    log.error("Telegram error: %d", resp.status)
                    return None
    except Exception as e:
        log.error("Telegram failed: %s", e)
        return None

async def answer_callback(token, callback_query_id):
    url = "https://api.telegram.org/bot" + token + "/answerCallbackQuery"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"callback_query_id": callback_query_id})
    except Exception as e:
        log.warning("answer_callback error: %s", e)

async def get_updates(token, offset=None):
    url = "https://api.telegram.org/bot" + token + "/getUpdates"
    params = {"timeout": 1}
    if offset:
        params["offset"] = offset
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", [])
    except Exception as e:
        log.warning("get_updates error: %s", e)
    return []

async def send_alert(token, chat_id, opp, alert_id, research, limits):
    confidence_emoji = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "💡"}.get(
        opp.get("confidence_tier", "MEDIUM"), "⚡"
    )
    category_emoji = {
        "Crypto": "₿", "Sports": "🏆", "Politics": "🗳",
        "Economics": "📈", "Science": "🔬", "General": "📊"
    }.get(opp.get("category", "General"), "📊")

    entry = opp["yes_price"]
    take_profit_price = round(min(entry * 1.40, 0.99), 2)
    stop_loss_price   = round(max(entry * 0.80, 0.01), 2)  # -20% stop loss (was 0.75 = -25%)

    msg = (
        confidence_emoji + " <b>Opportunity Found!</b> " + category_emoji + "\n\n"
        "<b>Market:</b> " + opp["question"][:100] + "\n"
        "<b>Category:</b> " + opp.get("category", "General") + "\n"
        "<b>YES Price:</b> " + str(round(entry * 100)) + "¢\n"
        "<b>Score:</b> " + str(opp["score"]) + "/100\n"
        "<b>Confidence:</b> " + opp.get("confidence_tier", "MEDIUM") + "\n"
        "<b>Why:</b> " + opp["reason"] + "\n"
        "<b>Volume:</b> $" + str(round(opp["volume"])) + "\n\n"
        "<b>Position Management:</b>\n"
        "  Take Profit: " + str(round(take_profit_price * 100)) + "¢ (+40%)\n"
        "  Stop Loss:   " + str(round(stop_loss_price * 100)) + "¢ (-20%)\n"
        "  Max Trade:   $" + str(limits["max_trade_size"]) + "\n"
    )

    # Direction + edge
    direction = opp.get("direction", "NO_EDGE")
    edge_pct  = opp.get("edge_pct")
    if direction != "NO_EDGE":
        dir_emoji = "🟢" if direction == "BUY_YES" else "🔴"
        edge_str = f" ({edge_pct:.1f}% edge)" if edge_pct else ""
        msg += f"\n{dir_emoji} <b>Signal:</b> {direction}{edge_str}\n"

    if opp.get("age") and opp["age"] < 24:
        msg += "\n🆕 <b>New Market:</b> Only " + str(round(opp["age"])) + "h old\n"

    if opp.get("velocity_alert"):
        msg += "\n⚡ " + opp["velocity_alert"] + "\n"

    if opp.get("lag_detected"):
        msg += "\n🚨 " + opp["lag_detected"] + "\n"

    if opp.get("inconsistencies"):
        for inc in opp["inconsistencies"]:
            msg += "\n⚠️ " + inc + "\n"

    if opp.get("ambiguity_warning"):
        msg += "\n⚠️ " + opp["ambiguity_warning"] + "\n"

    if opp.get("upcoming_events"):
        for event in opp["upcoming_events"]:
            msg += "\n📅 Related event: " + event["name"] + " in " + str(event["days_until"]) + " days\n"

    if opp.get("liquidity_warning"):
        msg += "\n⚠️ " + opp["liquidity_warning"] + "\n"

    if opp.get("signals_fired"):
        sig_display = opp["signals_fired"].replace(",", " · ")
        msg += "\n🔍 <b>Signals:</b> " + sig_display + "\n"

    if research:
        msg += "\n" + research + "\n"

    # Category-specific context
    cat = opp.get("category", "General")
    mtype = opp.get("market_type", "GENERAL")
    dtr = opp.get("days_to_resolution")
    vol = opp.get("volume", 0)

    if cat == "Economics":
        msg += "\n<b>📈 Economics Context:</b>\n"
        msg += "  Watch for: FOMC statements, jobs data, CPI releases\n"
        if dtr is not None:
            msg += f"  Resolves in: {dtr}d\n"
    elif cat == "Politics":
        msg += "\n<b>🗳 Politics Context:</b>\n"
        msg += "  Watch for: polling shifts, official announcements\n"
        if dtr is not None:
            msg += f"  Resolves in: {dtr}d\n"
    elif cat == "Science":
        msg += "\n<b>🔬 Science Context:</b>\n"
        msg += "  Watch for: official announcements, trial results\n"
        if dtr is not None:
            msg += f"  Resolves in: {dtr}d\n"
    elif cat == "Sports" and not research:
        msg += "\n<b>🏆 Sports Context:</b>\n"
        msg += "  No Vegas odds available — trade with caution\n"
    elif cat == "Crypto":
        if mtype == "GENERAL":
            msg += "\n<b>💡 Tip:</b> GENERAL crypto — historically 47% win rate on live trades\n"
        elif mtype == "PRICE_TARGET":
            msg += "\n<b>💡 Tip:</b> PRICE_TARGET — bot edge strongest at 10-30¢ entry\n"

    msg += "\nDo you agree this looks interesting?"

    reply_markup = {
        "inline_keyboard": [[
            {"text": "👍 Agree", "callback_data": "agree_" + str(alert_id)},
            {"text": "👎 Disagree", "callback_data": "disagree_" + str(alert_id)}
        ]]
    }
    return await send_message(token, chat_id, msg, reply_markup)

async def send_status(token, chat_id, conn):
    from database import get_risk_state, get_dynamic_limits, get_daily_stats
    try:
        resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
        total_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
        total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log WHERE COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
        open_positions = await conn.fetchval(
            "SELECT COUNT(*) FROM trade_positions WHERE is_open = TRUE"
        )
        profitable_count = sum(1 for a in resolved if a["profitable"])
        win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0

        # Only include live-tracked trades (not backfilled estimates) for return stats
        live_resolved = [a for a in resolved if not a.get("is_backfilled")]
        returns = [a["exit_return_pct"] for a in live_resolved if a.get("exit_return_pct") is not None]
        avg_return = round(sum(returns) / len(returns), 1) if returns else None
        peaks = [a["peak_return_pct"] for a in live_resolved if a.get("peak_return_pct") is not None]
        avg_peak = round(sum(peaks) / len(peaks), 1) if peaks else None

        avg_return_str = (("+" if avg_return >= 0 else "") + str(avg_return) + "%") if avg_return is not None else "tracking soon"
        avg_peak_str = ("+" + str(avg_peak) + "%") if avg_peak is not None else "tracking soon"

        # Outcome type breakdown
        outcome_counts = {}
        for a in resolved:
            ot = a.get("outcome_type") or "UNKNOWN"
            outcome_counts[ot] = outcome_counts.get(ot, 0) + 1

        outcome_lines = ""
        for ot, count in sorted(outcome_counts.items(), key=lambda x: -x[1]):
            outcome_lines += "  " + ot + ": " + str(count) + "\n"

        cat_stats = {}
        for cat in ["Crypto", "Sports", "Politics", "Economics", "Science"]:
            cat_res = [a for a in resolved if a.get("category") == cat
                      or (a.get("reason") and cat + " market" in a["reason"])]
            if cat_res:
                cat_win = round(sum(1 for a in cat_res if a["profitable"]) / len(cat_res) * 100)
                cat_stats[cat] = str(cat_win) + "% (" + str(len(cat_res)) + ")"
            else:
                cat_stats[cat] = "0% (0)"

        high_conf   = [a for a in resolved if a.get("confidence_tier") == "HIGH"]
        medium_conf = [a for a in resolved if a.get("confidence_tier") == "MEDIUM"]
        low_conf    = [a for a in resolved if a.get("confidence_tier") == "LOW"]
        def tier_win(group):
            return round(sum(1 for a in group if a["profitable"]) / len(group) * 100) if group else 0
        high_win   = tier_win(high_conf)
        medium_win = tier_win(medium_conf)
        low_win    = tier_win(low_conf)

        fear_res = [a for a in resolved if a.get("fear_greed_regime") in ["Extreme Fear", "Fear"]]
        greed_res = [a for a in resolved if a.get("fear_greed_regime") in ["Greed", "Extreme Greed"]]
        fear_win = round(sum(1 for a in fear_res if a["profitable"]) / len(fear_res) * 100) if fear_res else 0
        greed_win = round(sum(1 for a in greed_res if a["profitable"]) / len(greed_res) * 100) if greed_res else 0

        agreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='agree' AND outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
        disagreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='disagree' AND outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
        agreed_win = round(sum(1 for a in agreed if a["profitable"]) / len(agreed) * 100) if agreed else 0
        disagreed_win = round(sum(1 for a in disagreed if a["profitable"]) / len(disagreed) * 100) if disagreed else 0

        state = await get_risk_state(conn)
        limits = get_dynamic_limits(state)

        sentiment = await conn.fetchrow("""
            SELECT score, regime, trend FROM sentiment_history
            ORDER BY recorded_at DESC LIMIT 1
        """)

        msg = (
            "<b>Bot Status Report</b>\n"
            + now().strftime("%B %d, %Y %H:%M UTC") + "\n\n"
            + "<b>Performance:</b>\n"
            + "Win Rate: " + str(win_rate) + "% (" + str(len(resolved)) + " resolved)\n"
            + "Avg Exit Return: " + avg_return_str + "\n"
            + "Avg Peak Return: " + avg_peak_str + "\n"
            + "Open Positions: " + str(open_positions) + "\n"
            + "Live Tracked: " + str(len(live_resolved)) + " still open / " + str(len(resolved)) + " resolved\n\n"
            + "<b>Outcome Breakdown:</b>\n"
            + outcome_lines
            + "\n<b>Win Rate by Category:</b>\n"
        )
        for cat, stat in cat_stats.items():
            msg += "  " + cat + ": " + stat + "\n"

        msg += (
            "\n<b>Confidence Tiers:</b>\n"
            + "  🔥 HIGH:   " + str(high_win)   + "% (" + str(len(high_conf))   + " resolved)\n"
            + "  ⚡ MEDIUM: " + str(medium_win) + "% (" + str(len(medium_conf)) + " resolved)\n"
            + "  💡 LOW:    " + str(low_win)    + "% (" + str(len(low_conf))    + " resolved)\n\n"
            + "<b>Sentiment Win Rates:</b>\n"
            + "  During Fear: " + str(fear_win) + "% (" + str(len(fear_res)) + ")\n"
            + "  During Greed: " + str(greed_win) + "% (" + str(len(greed_res)) + ")\n\n"
            + (
                "<b>Your Judgment:</b>\n"
                + "  When agreed: " + str(agreed_win) + "% (" + str(len(agreed)) + " rated)\n"
                + "  When disagreed: " + str(disagreed_win) + "% (" + str(len(disagreed)) + " rated)\n\n"
                if (agreed or disagreed) else ""
            )
            + "<b>Risk Limits:</b>\n"
            + "  Max trade: $" + str(limits["max_trade_size"]) + "\n"
            + "  Max daily loss: $" + str(limits["max_daily_loss"]) + "\n"
            + "  Win streak: " + str(state["win_streak"]) + "\n"
            + "  Loss streak: " + str(state["loss_streak"]) + "\n\n"
            + "<b>Database:</b>\n"
            + "  Total alerts: " + str(total_alerts) + "\n"
            + "  Opportunities logged: " + str(total_opps) + "\n"
            + "  Resolved: " + str(len(resolved)) + "\n"
        )

        if sentiment:
            msg += (
                "\n<b>Current Sentiment:</b>\n"
                + "  Fear and Greed: " + str(sentiment["score"])
                + " (" + str(sentiment["regime"]) + ") "
                + str(sentiment["trend"]) + "\n"
            )

        await send_message(token, chat_id, msg)
        log.info("Status report sent")
    except Exception as e:
        log.error("Status error: %s", e)
        await send_message(token, chat_id, "Error generating status: " + str(e)[:200])

async def send_daily_summary(token, chat_id, conn):
    from database import get_risk_state, get_dynamic_limits, get_daily_stats
    alerts_today = await get_daily_stats(conn)
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log WHERE COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    open_positions = await conn.fetchval(
        "SELECT COUNT(*) FROM trade_positions WHERE is_open = TRUE"
    )
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    profitable_count = sum(1 for a in resolved if a["profitable"])
    win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0

    live_resolved = [a for a in resolved if not a.get("is_backfilled")]
    returns = [a["exit_return_pct"] for a in live_resolved if a.get("exit_return_pct") is not None]
    avg_return = round(sum(returns) / len(returns), 1) if returns else None
    avg_return_str = (("+" if avg_return >= 0 else "") + str(avg_return) + "%") if avg_return is not None else "tracking soon"

    # Positions closed today
    closed_today = await conn.fetch("""
        SELECT * FROM trade_positions
        WHERE is_open = FALSE
        AND closed_at > NOW() - INTERVAL '24 hours'
    """)
    closed_today_wins = sum(1 for p in closed_today if p.get("outcome_type") in ("FULL_WIN", "PARTIAL_WIN"))
    closed_today_returns = [p["return_pct"] for p in closed_today if p.get("return_pct") is not None]
    avg_today_return = round(sum(closed_today_returns) / len(closed_today_returns), 1) if closed_today_returns else 0

    avg_score = sum(a["score"] for a in alerts_today) / len(alerts_today) if alerts_today else 0
    agreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating='agree' AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'") or 0
    disagreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating='disagree' AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'") or 0
    state = await get_risk_state(conn)
    limits = get_dynamic_limits(state)

    cat_counts = {}
    for cat in ["Crypto", "Sports", "Politics", "Economics", "Science"]:
        count = sum(1 for a in alerts_today
                   if a.get("category") == cat or
                   (a.get("reason") and cat + " market" in a["reason"]))
        if count > 0:
            cat_counts[cat] = count

    sentiment_today = await conn.fetchrow("""
        SELECT score, regime, trend FROM sentiment_history
        ORDER BY recorded_at DESC LIMIT 1
    """)

    msg = (
        "<b>Daily Summary Report</b>\n"
        + now().strftime("%B %d, %Y") + "\n\n"
        + "<b>Today's Activity:</b>\n"
        + "  Alerts sent: " + str(len(alerts_today)) + "\n"
        + "  Avg score: " + str(round(avg_score)) + "/100\n"
        + "  Open positions: " + str(open_positions) + "\n"
    )

    if closed_today:
        msg += (
            "\n<b>Positions Closed Today:</b>\n"
            + "  Closed: " + str(len(closed_today)) + "\n"
            + "  Wins: " + str(closed_today_wins) + " / " + str(len(closed_today)) + "\n"
            + "  Avg return: " + ("+" if avg_today_return >= 0 else "") + str(avg_today_return) + "%\n"
        )

    msg += "\n<b>By Category:</b>\n"
    for cat, count in cat_counts.items():
        msg += "  " + cat + ": " + str(count) + "\n"

    if sentiment_today:
        msg += (
            "\n<b>Market Sentiment:</b>\n"
            + "  Fear and Greed: " + str(sentiment_today["score"])
            + " (" + str(sentiment_today["regime"]) + ")\n"
        )

    msg += (
        "\n<b>Your Ratings:</b>\n"
        + "  Agreed: " + str(agreed) + "\n"
        + "  Disagreed: " + str(disagreed) + "\n\n"
        + "<b>All-Time Performance:</b>\n"
        + "  Total resolved: " + str(len(resolved)) + "\n"
        + "  Win rate: " + str(win_rate) + "%\n"
        + "  Avg exit return: " + avg_return_str + "\n\n"
        + "<b>Dynamic Risk:</b>\n"
        + "  Max trade: $" + str(limits["max_trade_size"]) + "\n"
        + "  Win streak: " + str(state["win_streak"]) + "\n"
        + "  Loss streak: " + str(state["loss_streak"]) + "\n\n"
        + "<b>Database:</b>\n"
        + "  Total alerts: " + str(total_count) + "\n"
        + "  Opportunities: " + str(total_opps) + "\n\n"
        + "Type /status for live stats!"
    )
    await send_message(token, chat_id, msg)

    log.info("Daily summary sent")

async def send_heartbeat(token, chat_id, conn):
    from database import get_risk_state, get_dynamic_limits, get_daily_stats
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    alerts_today = await get_daily_stats(conn)
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    profitable_count = sum(1 for a in resolved if a["profitable"])
    win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0
    state = await get_risk_state(conn)
    limits = get_dynamic_limits(state)
    msg = (
        "<b>Bot Heartbeat</b>\n"
        + now().strftime("%B %d, %Y %H:%M UTC") + "\n\n"
        + "Status: Running normally\n"
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Total in database: " + str(total_count) + "\n"
        + "Win rate: " + str(win_rate) + "%\n"
        + "Max trade: $" + str(limits["max_trade_size"]) + "\n\n"
        + "Type /status for full stats!"
    )
    await send_message(token, chat_id, msg)
    log.info("Heartbeat sent")

async def send_weekly_analysis(token, chat_id, conn):
    """
    Sends a lightweight weekly operational summary.
    The deep signal/category backtest is handled separately
    by run_weekly_backtest() in database.py and sent right after this.
    """
    total_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log WHERE COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    profitable = sum(1 for a in resolved if a["profitable"])
    win_rate = round(profitable / len(resolved) * 100) if resolved else 0

    open_positions = await conn.fetchval(
        "SELECT COUNT(*) FROM trade_positions WHERE is_open = TRUE"
    )
    live_resolved = [a for a in resolved if not a.get("is_backfilled")]
    returns = [a["exit_return_pct"] for a in live_resolved if a.get("exit_return_pct") is not None]
    avg_return = round(sum(returns) / len(returns), 1) if returns else None
    peaks = [a["peak_return_pct"] for a in live_resolved if a.get("peak_return_pct") is not None]
    avg_peak = round(sum(peaks) / len(peaks), 1) if peaks else None
    avg_return_str = (("+" if avg_return >= 0 else "") + str(avg_return) + "%") if avg_return is not None else "tracking soon"
    avg_peak_str = ("+" + str(avg_peak) + "%") if avg_peak is not None else "tracking soon"

    agreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='agree' AND outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    disagreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='disagree' AND outcome IS NOT NULL AND COALESCE(bot_version,'v17') = '" + BOT_VERSION + "'")
    agreed_win = round(sum(1 for a in agreed if a["profitable"]) / len(agreed) * 100) if agreed else 0
    disagreed_win = round(sum(1 for a in disagreed if a["profitable"]) / len(disagreed) * 100) if disagreed else 0

    msg = (
        "<b>Weekly Summary</b>\n"
        + now().strftime("%B %d, %Y") + "\n\n"
        + "<b>All-Time Stats:</b>\n"
        + "  Total alerts: " + str(total_alerts) + "\n"
        + "  Opportunities logged: " + str(total_opps) + "\n"
        + "  Resolved: " + str(len(resolved)) + "\n"
        + "  Win rate: " + str(win_rate) + "%\n"
        + "  Avg exit return: " + avg_return_str + "\n"
        + "  Avg peak return: " + avg_peak_str + "\n"
        + "  Open positions: " + str(open_positions) + "\n\n"
        + "<b>Your Judgment Accuracy:</b>\n"
        + "  When agreed: " + str(agreed_win) + "% (" + str(len(agreed)) + " rated)\n"
        + "  When disagreed: " + str(disagreed_win) + "% (" + str(len(disagreed)) + " rated)\n\n"
        + "Full signal breakdown follows below ↓\n"
        + "Type /status anytime for live stats!"
    )
    await send_message(token, chat_id, msg)
    log.info("Weekly analysis sent")