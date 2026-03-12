"""
Polymarket Bot — Comprehensive Performance Diagnostic
Run from your Railway environment or locally with DATABASE_URL set.

Usage:
    python diagnostic.py                  # full report
    python diagnostic.py --json           # output raw JSON for further processing
"""

import asyncio
import asyncpg
import os
import json
import argparse
from collections import defaultdict
from datetime import datetime, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL")


# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

async def get_conn():
    return await asyncpg.connect(DATABASE_URL)


async def fetch(conn, query, *args):
    return await conn.fetch(query, *args)


# ─────────────────────────────────────────────
# 1. Score distribution & gradient
# ─────────────────────────────────────────────

async def score_gradient(conn):
    """Do higher scores win more? Is there a meaningful gradient?"""
    rows = await fetch(conn, """
        SELECT
            CASE
                WHEN score >= 95 THEN '95-100'
                WHEN score >= 90 THEN '90-94'
                WHEN score >= 85 THEN '85-89'
                ELSE '<85'
            END AS bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE resolved = true
        GROUP BY bucket
        ORDER BY bucket DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 2. Win rate by category
# ─────────────────────────────────────────────

async def win_rate_by_category(conn):
    rows = await fetch(conn, """
        SELECT
            category,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return,
            ROUND(AVG(score)::numeric, 1) AS avg_score
        FROM alerts
        WHERE resolved = true
        GROUP BY category
        ORDER BY total DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 3. Loss reason breakdown
# ─────────────────────────────────────────────

async def loss_reasons(conn):
    rows = await fetch(conn, """
        SELECT
            loss_reason,
            COUNT(*) AS count,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE resolved = true AND outcome = 'LOSS'
        GROUP BY loss_reason
        ORDER BY count DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 4. Fear & Greed segmentation
# ─────────────────────────────────────────────

async def fear_greed_buckets(conn):
    """
    Requires fear_greed_at_alert column. If missing, returns a warning.
    Buckets: Extreme Fear <25, Fear 25-45, Neutral 46-54, Greed 55-75, Extreme Greed >75
    """
    # Check if column exists
    col_check = await conn.fetchval("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'fear_greed_at_alert'
    """)
    if not col_check:
        return {"warning": "fear_greed_at_alert column not found — add this to your alerts table for macro sentiment gating"}

    rows = await fetch(conn, """
        SELECT
            CASE
                WHEN fear_greed_at_alert < 25  THEN 'Extreme Fear (<25)'
                WHEN fear_greed_at_alert < 46  THEN 'Fear (25-45)'
                WHEN fear_greed_at_alert < 55  THEN 'Neutral (46-54)'
                WHEN fear_greed_at_alert < 76  THEN 'Greed (55-75)'
                ELSE                                'Extreme Greed (>75)'
            END AS sentiment,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return,
            category
        FROM alerts
        WHERE resolved = true AND category = 'Crypto'
        GROUP BY sentiment, category
        ORDER BY sentiment
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 5. Signal firing analysis
# ─────────────────────────────────────────────

async def signal_win_rates(conn):
    """
    Requires signals_fired as JSONB or text array.
    Checks which signals correlate with wins vs losses.
    Adapt the JSON extraction to your actual schema.
    """
    # Check signals_fired column type
    col_info = await conn.fetchrow("""
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'signals_fired'
    """)
    if not col_info:
        return {"warning": "signals_fired column not found"}

    dtype = col_info["data_type"]

    # JSONB array of signal name strings
    if dtype == "jsonb":
        rows = await fetch(conn, """
            SELECT
                signal,
                COUNT(*) AS total_fired,
                SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
                ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
            FROM alerts,
                 jsonb_array_elements_text(signals_fired) AS signal
            WHERE resolved = true
            GROUP BY signal
            ORDER BY total_fired DESC
        """)
    else:
        # Fallback: treat as comma-separated text
        rows = await fetch(conn, """
            SELECT
                trim(signal) AS signal,
                COUNT(*) AS total_fired,
                SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
                ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
            FROM alerts,
                 unnest(string_to_array(signals_fired::text, ',')) AS signal
            WHERE resolved = true
            GROUP BY trim(signal)
            ORDER BY total_fired DESC
        """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 6. Direction accuracy
# ─────────────────────────────────────────────

async def direction_accuracy(conn):
    """WRONG_DIRECTION rate by category — key for signal tuning."""
    rows = await fetch(conn, """
        SELECT
            category,
            COUNT(*) AS total_losses,
            SUM(CASE WHEN loss_reason = 'WRONG_DIRECTION' THEN 1 ELSE 0 END) AS wrong_direction,
            SUM(CASE WHEN loss_reason = 'NO_MOVEMENT'     THEN 1 ELSE 0 END) AS no_movement,
            SUM(CASE WHEN loss_reason = 'REVERSAL'        THEN 1 ELSE 0 END) AS reversal
        FROM alerts
        WHERE resolved = true AND outcome = 'LOSS'
        GROUP BY category
        ORDER BY total_losses DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 7. Time-to-resolution analysis
# ─────────────────────────────────────────────

async def resolution_time_analysis(conn):
    """Do short or long-duration markets perform better?"""
    col_check = await conn.fetchval("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'resolution_time'
    """)
    if not col_check:
        return {"warning": "resolution_time column not found — tracking market duration would improve analysis"}

    rows = await fetch(conn, """
        SELECT
            CASE
                WHEN EXTRACT(EPOCH FROM (resolution_time - created_at))/3600 < 6    THEN '<6h'
                WHEN EXTRACT(EPOCH FROM (resolution_time - created_at))/3600 < 24   THEN '6-24h'
                WHEN EXTRACT(EPOCH FROM (resolution_time - created_at))/3600 < 168  THEN '1-7d'
                ELSE '>7d'
            END AS time_bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE resolved = true
        GROUP BY time_bucket
        ORDER BY time_bucket
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 8. Recent trend (last 30 vs all-time)
# ─────────────────────────────────────────────

async def recent_vs_alltime(conn):
    rows = await fetch(conn, """
        SELECT
            CASE
                WHEN created_at >= NOW() - INTERVAL '30 days' THEN 'Last 30d'
                ELSE 'Older'
            END AS period,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return,
            ROUND(AVG(score)::numeric, 1) AS avg_score
        FROM alerts
        WHERE resolved = true
        GROUP BY period
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 9. Initial probability edge
# ─────────────────────────────────────────────

async def probability_edge(conn):
    """
    Are you taking positions where the market already reflects your view?
    Ideal: alerts on markets where initial_probability is mid-range (20-80%),
    not on near-certain outcomes.
    """
    col_check = await conn.fetchval("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'initial_probability'
    """)
    if not col_check:
        return {"warning": "initial_probability column not found"}

    rows = await fetch(conn, """
        SELECT
            CASE
                WHEN initial_probability < 0.10 THEN '<10%'
                WHEN initial_probability < 0.25 THEN '10-25%'
                WHEN initial_probability < 0.50 THEN '25-50%'
                WHEN initial_probability < 0.75 THEN '50-75%'
                WHEN initial_probability < 0.90 THEN '75-90%'
                ELSE '>90%'
            END AS prob_bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE resolved = true
        GROUP BY prob_bucket
        ORDER BY prob_bucket
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# Renderer
# ─────────────────────────────────────────────

def pct(wins, total):
    if not total:
        return "N/A"
    return f"{round(wins / total * 100, 1)}%"

def render_table(rows, columns):
    if not rows or isinstance(rows, dict):
        return
    header = " | ".join(str(c).ljust(20) for c in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print(" | ".join(str(row.get(c, "")).ljust(20) for c in columns))

def section(title):
    print(f"\n{'═'*60}")
    print(f"  {title}")
    print(f"{'═'*60}")


async def run_diagnostic(output_json=False):
    conn = await get_conn()
    results = {}

    print("\n🔍 Polymarket Bot — Performance Diagnostic")
    print(f"   Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # 1. Score gradient
    section("1. Score Gradient — does a higher score = higher win rate?")
    data = await score_gradient(conn)
    results["score_gradient"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["bucket", "total", "wins", "win_rate", "avg_return"])

    # 2. Category breakdown
    section("2. Win Rate by Category")
    data = await win_rate_by_category(conn)
    results["by_category"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["category", "total", "wins", "win_rate", "avg_return", "avg_score"])

    # 3. Loss reasons
    section("3. Loss Reason Breakdown")
    data = await loss_reasons(conn)
    results["loss_reasons"] = data
    render_table(data, ["loss_reason", "count", "avg_return"])

    # 4. Fear & Greed
    section("4. Crypto Win Rate by Fear & Greed Bucket")
    data = await fear_greed_buckets(conn)
    results["fear_greed"] = data
    if isinstance(data, dict) and "warning" in data:
        print(f"  ⚠️  {data['warning']}")
    else:
        for row in data:
            row["win_rate"] = pct(row["wins"], row["total"])
        render_table(data, ["sentiment", "total", "wins", "win_rate", "avg_return"])

    # 5. Signal win rates
    section("5. Per-Signal Win Rates")
    data = await signal_win_rates(conn)
    results["signals"] = data
    if isinstance(data, dict) and "warning" in data:
        print(f"  ⚠️  {data['warning']}")
    else:
        for row in data:
            row["win_rate"] = pct(row["wins"], row["total_fired"])
        render_table(data, ["signal", "total_fired", "wins", "win_rate", "avg_return"])

    # 6. Direction accuracy
    section("6. Direction Accuracy by Category")
    data = await direction_accuracy(conn)
    results["direction"] = data
    for row in data:
        row["wrong_dir_pct"] = pct(row["wrong_direction"], row["total_losses"])
    render_table(data, ["category", "total_losses", "wrong_direction", "wrong_dir_pct", "no_movement", "reversal"])

    # 7. Resolution time
    section("7. Win Rate by Time-to-Resolution")
    data = await resolution_time_analysis(conn)
    results["resolution_time"] = data
    if isinstance(data, dict) and "warning" in data:
        print(f"  ⚠️  {data['warning']}")
    else:
        for row in data:
            row["win_rate"] = pct(row["wins"], row["total"])
        render_table(data, ["time_bucket", "total", "wins", "win_rate", "avg_return"])

    # 8. Recent vs all-time
    section("8. Recent Trend (Last 30d vs Older)")
    data = await recent_vs_alltime(conn)
    results["trend"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["period", "total", "wins", "win_rate", "avg_return", "avg_score"])

    # 9. Probability edge
    section("9. Win Rate by Initial Market Probability")
    data = await probability_edge(conn)
    results["probability_edge"] = data
    if isinstance(data, dict) and "warning" in data:
        print(f"  ⚠️  {data['warning']}")
    else:
        for row in data:
            row["win_rate"] = pct(row["wins"], row["total"])
        render_table(data, ["prob_bucket", "total", "wins", "win_rate", "avg_return"])

    await conn.close()

    if output_json:
        print("\n\n--- JSON OUTPUT ---")
        print(json.dumps(results, indent=2, default=str))

    print("\n✅ Diagnostic complete.\n")
    print("Next steps:")
    print("  • Any section with <20% win rate is a suppression candidate")
    print("  • If signals section shows one signal always loses → zero its weight")
    print("  • If fear_greed <25 shows 0% win rate → add macro gate to scoring.py")
    print("  • If wrong_dir_pct > 50% in a category → direction logic needs work")
    print("  • Columns marked ⚠️  missing → add them to your alerts table for richer analysis")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Also output raw JSON")
    args = parser.parse_args()

    if not DATABASE_URL:
        print("❌ DATABASE_URL not set")
        exit(1)

    asyncio.run(run_diagnostic(output_json=args.json))