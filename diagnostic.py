"""
Polymarket Bot — Comprehensive Performance Diagnostic
Outcomes: TAKE_PROFIT = WIN, STOP_LOSS = LOSS, RESOLVED = use profitable field

Start command: python diagnostic.py
"""

import asyncio
import asyncpg
import os
import json
import argparse
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")

# Outcome mappings
WIN  = "TAKE_PROFIT"
LOSS = "STOP_LOSS"

async def get_conn():
    return await asyncpg.connect(DATABASE_URL)

async def fetch(conn, query, *args):
    return await conn.fetch(query, *args)


# ─────────────────────────────────────────────
# 1. Score gradient
# ─────────────────────────────────────────────

async def score_gradient(conn):
    rows = await fetch(conn, f"""
        SELECT
            CASE
                WHEN score >= 95 THEN '95-100'
                WHEN score >= 90 THEN '90-94'
                WHEN score >= 85 THEN '85-89'
                ELSE '<85'
            END AS bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome = '{LOSS}' THEN 1 ELSE 0 END) AS losses,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}')
        GROUP BY bucket
        ORDER BY bucket DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 2. Win rate by category
# ─────────────────────────────────────────────

async def win_rate_by_category(conn):
    rows = await fetch(conn, f"""
        SELECT
            COALESCE(category, 'None') AS category,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome = '{LOSS}' THEN 1 ELSE 0 END) AS losses,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return,
            ROUND(AVG(score)::numeric, 1) AS avg_score
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}')
        GROUP BY category
        ORDER BY total DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 3. Loss reason breakdown
# ─────────────────────────────────────────────

async def loss_reasons(conn):
    rows = await fetch(conn, f"""
        SELECT
            COALESCE(loss_reason, 'N/A') AS loss_reason,
            COUNT(*) AS count,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome = '{LOSS}'
        GROUP BY loss_reason
        ORDER BY count DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 4. Loss pattern breakdown
# ─────────────────────────────────────────────

async def loss_patterns(conn):
    rows = await fetch(conn, f"""
        SELECT
            COALESCE(loss_pattern, 'N/A') AS loss_pattern,
            COUNT(*) AS count,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome = '{LOSS}'
        GROUP BY loss_pattern
        ORDER BY count DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 5. Fear & Greed segmentation (crypto only)
# ─────────────────────────────────────────────

async def fear_greed_buckets(conn):
    rows = await fetch(conn, f"""
        SELECT
            CASE
                WHEN fear_greed_score < 25 THEN 'Extreme Fear (<25)'
                WHEN fear_greed_score < 46 THEN 'Fear (25-45)'
                WHEN fear_greed_score < 55 THEN 'Neutral (46-54)'
                WHEN fear_greed_score < 76 THEN 'Greed (55-75)'
                ELSE                            'Extreme Greed (>75)'
            END AS sentiment,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND category = 'Crypto'
        GROUP BY sentiment
        ORDER BY sentiment
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 6. Fear & Greed regime (all categories)
# ─────────────────────────────────────────────

async def fear_greed_regime(conn):
    rows = await fetch(conn, f"""
        SELECT
            fear_greed_regime,
            COALESCE(category, 'None') AS category,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND fear_greed_regime IS NOT NULL
        GROUP BY fear_greed_regime, category
        ORDER BY fear_greed_regime, category
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 7. Per-signal win rates
# ─────────────────────────────────────────────

async def signal_win_rates(conn):
    rows = await fetch(conn, f"""
        SELECT
            trim(signal) AS signal,
            COUNT(*) AS total_fired,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts,
             unnest(string_to_array(signals_fired, ',')) AS signal
        WHERE outcome IN ('{WIN}', '{LOSS}') AND signals_fired IS NOT NULL
        GROUP BY trim(signal)
        ORDER BY total_fired DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 8. Direction accuracy
# ─────────────────────────────────────────────

async def direction_accuracy(conn):
    rows = await fetch(conn, f"""
        SELECT
            COALESCE(category, 'None') AS category,
            direction,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND direction IS NOT NULL
        GROUP BY category, direction
        ORDER BY category, direction
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 9. Time-to-resolution
# ─────────────────────────────────────────────

async def resolution_time_analysis(conn):
    rows = await fetch(conn, f"""
        SELECT
            CASE
                WHEN days_to_resolution < 0.25 THEN '<6h'
                WHEN days_to_resolution < 1    THEN '6-24h'
                WHEN days_to_resolution < 7    THEN '1-7d'
                ELSE '>7d'
            END AS time_bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND days_to_resolution IS NOT NULL
        GROUP BY time_bucket
        ORDER BY time_bucket
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 10. Vegas gap analysis
# ─────────────────────────────────────────────

async def vegas_gap_analysis(conn):
    rows = await fetch(conn, f"""
        SELECT
            CASE
                WHEN vegas_gap < -0.10 THEN 'Large negative (<-10%)'
                WHEN vegas_gap < -0.05 THEN 'Mod negative (-10 to -5%)'
                WHEN vegas_gap < 0     THEN 'Small negative (-5 to 0%)'
                WHEN vegas_gap < 0.05  THEN 'Small positive (0 to 5%)'
                WHEN vegas_gap < 0.10  THEN 'Mod positive (5-10%)'
                ELSE                        'Large positive (>10%)'
            END AS gap_bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND vegas_gap IS NOT NULL
        GROUP BY gap_bucket
        ORDER BY gap_bucket
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 11. Edge pct analysis
# ─────────────────────────────────────────────

async def edge_analysis(conn):
    rows = await fetch(conn, f"""
        SELECT
            CASE
                WHEN edge_pct < 0.02  THEN '<2%'
                WHEN edge_pct < 0.05  THEN '2-5%'
                WHEN edge_pct < 0.10  THEN '5-10%'
                WHEN edge_pct < 0.20  THEN '10-20%'
                ELSE                       '>20%'
            END AS edge_bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND edge_pct IS NOT NULL
        GROUP BY edge_bucket
        ORDER BY edge_bucket
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 12. Confidence tier performance
# ─────────────────────────────────────────────

async def confidence_tier_analysis(conn):
    rows = await fetch(conn, f"""
        SELECT
            confidence_tier,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return,
            ROUND(AVG(score)::numeric, 1) AS avg_score
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND confidence_tier IS NOT NULL
        GROUP BY confidence_tier
        ORDER BY avg_score DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 13. Hour of day
# ─────────────────────────────────────────────

async def hour_of_day(conn):
    rows = await fetch(conn, f"""
        SELECT
            hour_of_day_utc,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND hour_of_day_utc IS NOT NULL
        GROUP BY hour_of_day_utc
        ORDER BY hour_of_day_utc
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 14. Recent vs all-time
# ─────────────────────────────────────────────

async def recent_vs_alltime(conn):
    rows = await fetch(conn, f"""
        SELECT
            CASE
                WHEN alerted_at >= NOW() - INTERVAL '30 days' THEN 'Last 30d'
                ELSE 'Older'
            END AS period,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return,
            ROUND(AVG(score)::numeric, 1) AS avg_score
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}')
        GROUP BY period
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 15. Entry price buckets
# ─────────────────────────────────────────────

async def entry_price_analysis(conn):
    rows = await fetch(conn, f"""
        SELECT
            CASE
                WHEN entry_price < 0.10 THEN '<10%'
                WHEN entry_price < 0.25 THEN '10-25%'
                WHEN entry_price < 0.50 THEN '25-50%'
                WHEN entry_price < 0.75 THEN '50-75%'
                WHEN entry_price < 0.90 THEN '75-90%'
                ELSE '>90%'
            END AS price_bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}') AND entry_price IS NOT NULL
        GROUP BY price_bucket
        ORDER BY price_bucket
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 16. NO_EDGE deep dive
# ─────────────────────────────────────────────

async def no_edge_analysis(conn):
    rows = await fetch(conn, f"""
        SELECT
            COALESCE(category, 'None') AS category,
            direction,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = '{WIN}' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_return,
            ROUND(AVG(score)::numeric, 1) AS avg_score
        FROM alerts
        WHERE outcome IN ('{WIN}', '{LOSS}')
        GROUP BY category, direction
        ORDER BY total DESC
    """)
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# Renderer helpers
# ─────────────────────────────────────────────

def pct(wins, total):
    if not total:
        return "N/A"
    return f"{round(wins / total * 100, 1)}%"

def render_table(rows, columns):
    if not rows:
        print("  (no data)")
        return
    header = " | ".join(str(c).ljust(22) for c in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print(" | ".join(str(row.get(c, "")).ljust(22) for c in columns))

def section(title):
    print(f"\n{'═'*65}")
    print(f"  {title}")
    print(f"{'═'*65}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

async def run_diagnostic(output_json=False):
    conn = await get_conn()
    results = {}

    print("\n🔍 Polymarket Bot — Performance Diagnostic")
    print(f"   Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   WIN = {WIN} | LOSS = {LOSS}\n")

    section("1. Score Gradient")
    data = await score_gradient(conn)
    results["score_gradient"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["bucket", "total", "wins", "losses", "win_rate", "avg_return"])

    section("2. Win Rate by Category")
    data = await win_rate_by_category(conn)
    results["by_category"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["category", "total", "wins", "losses", "win_rate", "avg_return", "avg_score"])

    section("3. Loss Reason Breakdown")
    data = await loss_reasons(conn)
    results["loss_reasons"] = data
    render_table(data, ["loss_reason", "count", "avg_return"])

    section("4. Loss Pattern Breakdown")
    data = await loss_patterns(conn)
    results["loss_patterns"] = data
    render_table(data, ["loss_pattern", "count", "avg_return"])

    section("5. Crypto Win Rate by Fear & Greed Bucket")
    data = await fear_greed_buckets(conn)
    results["fear_greed_buckets"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["sentiment", "total", "wins", "win_rate", "avg_return"])

    section("6. Win Rate by Fear & Greed Regime")
    data = await fear_greed_regime(conn)
    results["fear_greed_regime"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["fear_greed_regime", "category", "total", "wins", "win_rate", "avg_return"])

    section("7. Per-Signal Win Rates")
    data = await signal_win_rates(conn)
    results["signals"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total_fired"])
    render_table(data, ["signal", "total_fired", "wins", "win_rate", "avg_return"])

    section("8. Direction Accuracy by Category")
    data = await direction_accuracy(conn)
    results["direction"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["category", "direction", "total", "wins", "win_rate", "avg_return"])

    section("9. NO_EDGE Deep Dive")
    data = await no_edge_analysis(conn)
    results["no_edge"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["category", "direction", "total", "wins", "win_rate", "avg_return", "avg_score"])

    section("10. Win Rate by Time-to-Resolution")
    data = await resolution_time_analysis(conn)
    results["resolution_time"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["time_bucket", "total", "wins", "win_rate", "avg_return"])

    section("11. Vegas Gap Analysis")
    data = await vegas_gap_analysis(conn)
    results["vegas_gap"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["gap_bucket", "total", "wins", "win_rate", "avg_return"])

    section("12. Edge Pct Analysis")
    data = await edge_analysis(conn)
    results["edge"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["edge_bucket", "total", "wins", "win_rate", "avg_return"])

    section("13. Confidence Tier Performance")
    data = await confidence_tier_analysis(conn)
    results["confidence_tier"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["confidence_tier", "total", "wins", "win_rate", "avg_return", "avg_score"])

    section("14. Hour of Day Performance (UTC)")
    data = await hour_of_day(conn)
    results["hour_of_day"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["hour_of_day_utc", "total", "wins", "win_rate", "avg_return"])

    section("15. Recent Trend — Last 30d vs Older")
    data = await recent_vs_alltime(conn)
    results["trend"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["period", "total", "wins", "win_rate", "avg_return", "avg_score"])

    section("16. Entry Price (Probability) Buckets")
    data = await entry_price_analysis(conn)
    results["entry_price"] = data
    for row in data:
        row["win_rate"] = pct(row["wins"], row["total"])
    render_table(data, ["price_bucket", "total", "wins", "win_rate", "avg_return"])

    await conn.close()

    if output_json:
        print("\n\n--- JSON OUTPUT ---")
        print(json.dumps(results, indent=2, default=str))

    print(f"\n{'═'*65}")
    print("  ✅ Diagnostic complete")
    print(f"{'═'*65}")
    print("""
Key things to act on:
  • Score gradient flat?          → weights not discriminating, rebuild
  • Signal win rate <20%?         → zero out that signal weight
  • Extreme Fear win rate low?    → add hard macro gate in scoring.py
  • NO_EDGE dominating?           → suppress alerts with no direction
  • Vegas gap sweet spot?         → tighten entry to winning range only
  • Edge <2% always loses?        → raise minimum edge threshold
  • Entry price >75% losing?      → avoid near-certain markets
  • Specific hours consistently bad? → add time-of-day filter
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not DATABASE_URL:
        print("❌ DATABASE_URL not set")
        exit(1)

    asyncio.run(run_diagnostic(output_json=args.json))