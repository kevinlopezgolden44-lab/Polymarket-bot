"""
Exit threshold analysis — should we adjust take_profit_pct and stop_loss_pct?
Current settings: take_profit=+40%, stop_loss=-25%

Start command: python exit_analysis.py
"""

import asyncio
import asyncpg
import os
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")

async def run():
    conn = await asyncpg.connect(DATABASE_URL)

    print("\n📊 Exit Threshold Analysis")
    print(f"   Current: take_profit=+40%  stop_loss=-25%")
    print(f"   Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # ── 1. How did wins exit? ──────────────────────────────────────────────────
    print("═" * 60)
    print("  1. How did TAKE_PROFIT trades exit?")
    print("═" * 60)
    rows = await conn.fetch("""
        SELECT
            exit_return_pct,
            peak_return_pct,
            hold_duration_hours,
            actual_hold_days,
            exit_reason,
            entry_price,
            exit_price,
            peak_price
        FROM alerts
        WHERE outcome = 'TAKE_PROFIT'
        ORDER BY exit_return_pct DESC
    """)
    for r in rows:
        print(f"  exit={r['exit_return_pct']}%  peak={r['peak_return_pct']}%  "
              f"hold={r['hold_duration_hours']}h  "
              f"entry={round((r['entry_price'] or 0)*100)}¢ → exit={round((r['exit_price'] or 0)*100)}¢")

    # ── 2. How did losses exit? ────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  2. How did STOP_LOSS trades exit?")
    print("═" * 60)
    rows = await conn.fetch("""
        SELECT
            exit_return_pct,
            peak_return_pct,
            hold_duration_hours,
            actual_hold_days,
            exit_reason,
            entry_price,
            exit_price,
            peak_price,
            loss_reason,
            loss_pattern
        FROM alerts
        WHERE outcome = 'STOP_LOSS'
        ORDER BY exit_return_pct ASC
        LIMIT 20
    """)
    for r in rows:
        print(f"  exit={r['exit_return_pct']}%  peak={r['peak_return_pct']}%  "
              f"hold={r['hold_duration_hours']}h  "
              f"loss_reason={r['loss_reason']}  pattern={r['loss_pattern']}")

    # ── 3. Peak vs exit — how much profit left on table? ──────────────────────
    print("\n" + "═" * 60)
    print("  3. Peak vs Exit — profit left on table (TAKE_PROFIT only)")
    print("═" * 60)
    rows = await conn.fetch("""
        SELECT
            ROUND(AVG(peak_return_pct)::numeric, 2) AS avg_peak,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_exit,
            ROUND(AVG(peak_return_pct - exit_return_pct)::numeric, 2) AS avg_left_on_table,
            ROUND(MAX(peak_return_pct)::numeric, 2) AS max_peak,
            ROUND(MIN(exit_return_pct)::numeric, 2) AS min_exit
        FROM alerts
        WHERE outcome = 'TAKE_PROFIT'
          AND peak_return_pct IS NOT NULL
          AND exit_return_pct IS NOT NULL
    """)
    for r in rows:
        print(f"  avg peak: {r['avg_peak']}%")
        print(f"  avg exit: {r['avg_exit']}%")
        print(f"  avg left on table: {r['avg_left_on_table']}%")
        print(f"  max peak seen: {r['max_peak']}%")
        print(f"  min exit (worst win): {r['min_exit']}%")

    # ── 4. Stop loss — how deep before stopping out? ──────────────────────────
    print("\n" + "═" * 60)
    print("  4. Stop Loss depth analysis")
    print("═" * 60)
    rows = await conn.fetch("""
        SELECT
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_exit,
            ROUND(MIN(exit_return_pct)::numeric, 2) AS worst_exit,
            ROUND(MAX(exit_return_pct)::numeric, 2) AS best_stop_exit,
            ROUND(AVG(peak_return_pct)::numeric, 2) AS avg_peak_before_stop,
            COUNT(*) AS total_stops
        FROM alerts
        WHERE outcome = 'STOP_LOSS'
    """)
    for r in rows:
        print(f"  total stop losses: {r['total_stops']}")
        print(f"  avg exit at stop: {r['avg_exit']}%")
        print(f"  worst exit: {r['worst_exit']}%")
        print(f"  best stop exit: {r['best_stop_exit']}%")
        print(f"  avg peak before stopping out: {r['avg_peak_before_stop']}%")

    # ── 5. Did any losses go positive before stopping out? ────────────────────
    print("\n" + "═" * 60)
    print("  5. Losses that went positive first (peak > 0) before stopping out")
    print("     — these would have been saved by a tighter trailing stop")
    print("═" * 60)
    rows = await conn.fetch("""
        SELECT
            COUNT(*) AS count,
            ROUND(AVG(peak_return_pct)::numeric, 2) AS avg_peak,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_exit
        FROM alerts
        WHERE outcome = 'STOP_LOSS'
          AND peak_return_pct > 5
    """)
    for r in rows:
        print(f"  count: {r['count']}")
        print(f"  avg peak reached: {r['avg_peak']}%")
        print(f"  avg exit (stopped out after): {r['avg_exit']}%")

    # ── 6. Hold duration distribution ─────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  6. Hold duration by outcome")
    print("═" * 60)
    rows = await conn.fetch("""
        SELECT
            outcome,
            ROUND(AVG(hold_duration_hours)::numeric, 1) AS avg_hold_hours,
            ROUND(MIN(hold_duration_hours)::numeric, 1) AS min_hold_hours,
            ROUND(MAX(hold_duration_hours)::numeric, 1) AS max_hold_hours,
            COUNT(*) AS total
        FROM alerts
        WHERE outcome IN ('TAKE_PROFIT', 'STOP_LOSS')
          AND hold_duration_hours IS NOT NULL
        GROUP BY outcome
    """)
    for r in rows:
        print(f"  {r['outcome']:15} avg={r['avg_hold_hours']}h  "
              f"min={r['min_hold_hours']}h  max={r['max_hold_hours']}h  n={r['total']}")

    # ── 7. What % of losses were within 10% of stop loss threshold? ───────────
    print("\n" + "═" * 60)
    print("  7. Stop loss proximity — were losses close to -25% or much worse?")
    print("═" * 60)
    rows = await conn.fetch("""
        SELECT
            CASE
                WHEN exit_return_pct >= -10  THEN 'Tight loss (>-10%)'
                WHEN exit_return_pct >= -25  THEN 'Hit stop loss (-10 to -25%)'
                WHEN exit_return_pct >= -50  THEN 'Blew through stop (-25 to -50%)'
                ELSE 'Severe loss (< -50%)'
            END AS loss_bucket,
            COUNT(*) AS count,
            ROUND(AVG(exit_return_pct)::numeric, 2) AS avg_exit
        FROM alerts
        WHERE outcome = 'STOP_LOSS'
        GROUP BY loss_bucket
        ORDER BY avg_exit DESC
    """)
    for r in rows:
        print(f"  {r['loss_bucket']:35} n={r['count']}  avg={r['avg_exit']}%")

    await conn.close()
    print("\n✅ Exit analysis complete\n")

asyncio.run(run())