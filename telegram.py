import aiohttp
import logging
from datetime import datetime

log = logging.getLogger(__name__)

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

    msg = (
        confidence_emoji + " <b>Opportunity Found!</b> " + category_emoji + "\n\n"
        "<b>Market:</b> " + opp["question"][:100] + "\n"
        "<b>Category:</b> " + opp.get("category", "General") + "\n"
        "<b>YES Price:</b> " + str(round(opp["yes_price"] * 100)) + "%\n"
        "<b>Score:</b> " + str(opp["score"]) + "/100\n"
        "<b>Confidence:</b> " + opp.get("confidence_tier", "Medium") + "\n"
        "<b>Why:</b> " + opp["reason"] + "\n"
        "<b>Volume:</b> $" + str(round(opp["volume"])) + "\n"
        "<b>Max Trade Size:</b> $" + str(limits["max_trade_size"]) + "\n"
    )

    if opp.get("age") and opp["age"] < 24:
        msg += "<b>🆕 New Market:</b> Only " + str(round(opp["age"])) + "h old\n"

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

    if research:
        msg += "\n" + research + "\n"

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
        resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
        total_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts")
        total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log")
        profitable_count = sum(1 for a in resolved if a["profitable"])
        win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0

        cat_stats = {}
        for cat in ["Crypto", "Sports", "Politics", "Economics", "Science"]:
            cat_res = [a for a in resolved if a.get("category") == cat
                      or (a.get("reason") and cat + " market" in a["reason"])]
            if cat_res:
                cat_win = round(sum(1 for a in cat_res if a["profitable"]) / len(cat_res) * 100)
                cat_stats[cat] = str(cat_win) + "% (" + str(len(cat_res)) + ")"
            else:
                cat_stats[cat] = "0% (0)"

        high_conf = [a for a in resolved if a.get("confidence_tier") == "HIGH"]
        high_win = round(sum(1 for a in high_conf if a["profitable"]) / len(high_conf) * 100) if high_conf else 0

        fear_res = [a for a in resolved if a.get("fear_greed_regime") in ["Extreme Fear", "Fear"]]
        greed_res = [a for a in resolved if a.get("fear_greed_regime") in ["Greed", "Extreme Greed"]]
        fear_win = round(sum(1 for a in fear_res if a["profitable"]) / len(fear_res) * 100) if fear_res else 0
        greed_win = round(sum(1 for a in greed_res if a["profitable"]) / len(greed_res) * 100) if greed_res else 0

        agreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='agree' AND outcome IS NOT NULL")
        disagreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='disagree' AND outcome IS NOT NULL")
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
            + "<b>Win Rates:</b>\n"
            + "Overall: " + str(win_rate) + "% (" + str(len(resolved)) + " resolved)\n"
        )
        for cat, stat in cat_stats.items():
            msg += cat + ": " + stat + "\n"

        msg += (
            "\n<b>Confidence Tiers:</b>\n"
            + "High confidence: " + str(high_win) + "% (" + str(len(high_conf)) + " resolved)\n\n"
            + "<b>Sentiment Win Rates:</b>\n"
            + "During Fear: " + str(fear_win) + "% (" + str(len(fear_res)) + ")\n"
            + "During Greed: " + str(greed_win) + "% (" + str(len(greed_res)) + ")\n\n"
            + "<b>Your Judgment:</b>\n"
            + "When agreed: " + str(agreed_win) + "% (" + str(len(agreed)) + " rated)\n"
            + "When disagreed: " + str(disagreed_win) + "% (" + str(len(disagreed)) + " rated)\n\n"
            + "<b>Risk Limits:</b>\n"
            + "Max trade size: $" + str(limits["max_trade_size"]) + "\n"
            + "Max daily loss: $" + str(limits["max_daily_loss"]) + "\n"
            + "Win streak: " + str(state["win_streak"]) + "\n"
            + "Loss streak: " + str(state["loss_streak"]) + "\n\n"
            + "<b>Database:</b>\n"
            + "Total alerts: " + str(total_alerts) + "\n"
            + "Opportunities logged: " + str(total_opps) + "\n"
            + "Resolved: " + str(len(resolved)) + "\n"
        )

        if sentiment:
            msg += (
                "\n<b>Current Sentiment:</b>\n"
                + "Fear and Greed: " + str(sentiment["score"])
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
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log")
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
    profitable_count = sum(1 for a in resolved if a["profitable"])
    win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0
    avg_score = sum(a["score"] for a in alerts_today) / len(alerts_today) if alerts_today else 0
    agreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating='agree'") or 0
    disagreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating='disagree'") or 0
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
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Avg score: " + str(round(avg_score)) + "/100\n\n"
        + "By Category:\n"
    )
    for cat, count in cat_counts.items():
        msg += cat + ": " + str(count) + "\n"

    if sentiment_today:
        msg += (
            "\nMarket Sentiment:\n"
            + "Fear and Greed: " + str(sentiment_today["score"])
            + " (" + str(sentiment_today["regime"]) + ")\n"
        )

    msg += (
        "\nYour Ratings:\n"
        + "Agreed: " + str(agreed) + "\n"
        + "Disagreed: " + str(disagreed) + "\n\n"
        + "Resolved Markets:\n"
        + "Total resolved: " + str(len(resolved)) + "\n"
        + "Profitable: " + str(profitable_count) + "\n"
        + "Win rate: " + str(win_rate) + "%\n\n"
        + "Dynamic Risk:\n"
        + "Max trade: $" + str(limits["max_trade_size"]) + "\n"
        + "Win streak: " + str(state["win_streak"]) + "\n"
        + "Loss streak: " + str(state["loss_streak"]) + "\n\n"
        + "Database:\n"
        + "Total alerts: " + str(total_count) + "\n"
        + "Opportunities: " + str(total_opps) + "\n\n"
        + "Type /status for live stats!"
    )
    await send_message(token, chat_id, msg)
    log.info("Daily summary sent")

async def send_heartbeat(token, chat_id, conn):
    from database import get_risk_state, get_dynamic_limits, get_daily_stats
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    alerts_today = await get_daily_stats(conn)
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
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
    total_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log")
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
    profitable = sum(1 for a in resolved if a["profitable"])
    win_rate = round(profitable / len(resolved) * 100) if resolved else 0

    cat_lines = []
    for cat in ["Crypto", "Sports", "Politics", "Economics", "Science"]:
        cat_res = [a for a in resolved if
                  a.get("category") == cat or
                  (a.get("reason") and cat + " market" in a["reason"])]
        if cat_res:
            cat_win = round(sum(1 for a in cat_res if a["profitable"]) / len(cat_res) * 100)
            cat_lines.append(cat + ": " + str(cat_win) + "% (" + str(len(cat_res)) + " resolved)")

    fear_res = [a for a in resolved if a.get("fear_greed_regime") in ["Extreme Fear", "Fear"]]
    greed_res = [a for a in resolved if a.get("fear_greed_regime") in ["Greed", "Extreme Greed"]]
    fear_win = round(sum(1 for a in fear_res if a["profitable"]) / len(fear_res) * 100) if fear_res else 0
    greed_win = round(sum(1 for a in greed_res if a["profitable"]) / len(greed_res) * 100) if greed_res else 0

    agreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='agree' AND outcome IS NOT NULL")
    disagreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating='disagree' AND outcome IS NOT NULL")
    agreed_win = round(sum(1 for a in agreed if a["profitable"]) / len(agreed) * 100) if agreed else 0
    disagreed_win = round(sum(1 for a in disagreed if a["profitable"]) / len(disagreed) * 100) if disagreed else 0

    msg = (
        "<b>Weekly Self-Analysis Report</b>\n"
        + now().strftime("%B %d, %Y") + "\n\n"
        + "Total alerts: " + str(total_alerts) + "\n"
        + "Opportunities logged: " + str(total_opps) + "\n"
        + "Resolved: " + str(len(resolved)) + "\n"
        + "Win rate: " + str(win_rate) + "%\n\n"
        + "By Category:\n"
        + "\n".join(cat_lines if cat_lines else ["No resolved data yet"]) + "\n\n"
        + "By Sentiment:\n"
        + "During Fear: " + str(fear_win) + "% (" + str(len(fear_res)) + ")\n"
        + "During Greed: " + str(greed_win) + "% (" + str(len(greed_res)) + ")\n\n"
        + "Your Judgment:\n"
        + "When agreed: " + str(agreed_win) + "%\n"
        + "When disagreed: " + str(disagreed_win) + "%\n\n"
        + "Type /status anytime for live stats!"
    )
    await send_message(token, chat_id, msg)
    log.info("Weekly analysis sent")