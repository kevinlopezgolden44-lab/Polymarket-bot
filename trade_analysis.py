"""
Live Trade Weight Calibration
Pulls your bot's actual resolved trades and analyzes
which entry prices, signals, and conditions correlate with wins.

Start command: python trade_analysis.py
"""

import asyncio
import asyncpg
import os
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")

async def run():
    conn = await asyncpg.connect(DATABASE_URL)
    print(f"\n📊 Live Trade Weight Calibration")
    print(f"   Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # ── 1. Entry price vs win rate (your bot's actual trades) ─────────────────
    print("═"*60)
    print("  1. Entry Price vs Win Rate — YOUR BOT'S ACTUAL TRADES")
    print("     (not historical markets — these are trades your bot entered)")
    print("═"*60)
    rows = await conn.fetch("""
        SELECT
            CASE
                WHEN entry_price < 0.10 THEN '01: <10c'
                WHEN entry_price < 0.20 THEN '02: 10-20c'
                WHEN entry_price < 0.30 THEN '03: 20-30c'
                WHEN entry_price < 0.40 THEN '04: 30-40c'
                WHEN entry_price < 0.50 THEN '05: 40-50c'
                WHEN entry_price < 0.60 THEN '06: 50-60c'
                WHEN entry_price < 0.70 THEN '07: 60-70c'
                WHEN entry_price < 0.80 THEN '08: 70-80c'
                WHEN entry_price < 0.90 THEN '09: 80-90c'
                ELSE                        '10: 90c+'
            END AS bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'TAKE_PROFIT' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome = 'STOP_LOSS' THEN 1 ELSE 0 END) AS losses,
            ROUND(AVG(entry_price)::numeric, 2) AS avg_entry,
            ROUND(AVG(exit_return_pct)::numeric, 1) AS avg_return
        FROM alerts
        WHERE outcome IN ('TAKE_PROFIT', 'STOP_LOSS')
        AND entry_price IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
    """)
    print(f"\n  {'Price':<12} {'Total':>6} {'Wins':>6} {'Losses':>7} {'Win%':>7} {'Avg Ret':>8}")
    print(f"  {'-'*52}")
    for r in rows:
        total = r['total']
        wins  = r['wins'] or 0
        wr    = round(wins/total*100, 1) if total else 0
        flag  = " ← LOSING" if wr < 33.3 else " ✅" if wr > 50 else ""
        print(f"  {r['bucket']:<12} {total:>6} {wins:>6} {r['losses']:>7} {wr:>6.1f}% {r['avg_return']:>7.1f}%{flag}")

    # ── 2. Direction vs win rate ───────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  2. Direction vs Win Rate")
    print(f"{'═'*60}")
    rows = await conn.fetch("""
        SELECT
            direction,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'TAKE_PROFIT' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(entry_price)::numeric, 2) AS avg_entry,
            ROUND(AVG(exit_return_pct)::numeric, 1) AS avg_return
        FROM alerts
        WHERE outcome IN ('TAKE_PROFIT', 'STOP_LOSS')
        GROUP BY direction
        ORDER BY total DESC
    """)
    print(f"\n  {'Direction':<15} {'Total':>6} {'Wins':>6} {'Win%':>7} {'Avg Entry':>10} {'Avg Ret':>8}")
    print(f"  {'-'*56}")
    for r in rows:
        wr = round((r['wins'] or 0)/r['total']*100, 1) if r['total'] else 0
        print(f"  {str(r['direction']):<15} {r['total']:>6} {r['wins']:>6} {wr:>6.1f}% {r['avg_entry']:>9.2f} {r['avg_return']:>7.1f}%")

    # ── 3. Score vs win rate ───────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  3. Score vs Win Rate — does higher score = higher win rate?")
    print(f"{'═'*60}")
    rows = await conn.fetch("""
        SELECT
            CASE
                WHEN score >= 95 THEN '95-100'
                WHEN score >= 90 THEN '90-94'
                WHEN score >= 85 THEN '85-89'
                WHEN score >= 80 THEN '80-84'
                ELSE '<80'
            END AS bucket,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'TAKE_PROFIT' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(exit_return_pct)::numeric, 1) AS avg_return
        FROM alerts
        WHERE outcome IN ('TAKE_PROFIT', 'STOP_LOSS')
        GROUP BY bucket
        ORDER BY bucket DESC
    """)
    print(f"\n  {'Score':<10} {'Total':>6} {'Wins':>6} {'Win%':>7} {'Avg Ret':>8}")
    print(f"  {'-'*42}")
    for r in rows:
        wr = round((r['wins'] or 0)/r['total']*100, 1) if r['total'] else 0
        print(f"  {r['bucket']:<10} {r['total']:>6} {r['wins']:>6} {wr:>6.1f}% {r['avg_return']:>7.1f}%")

    # ── 4. Market type vs win rate ─────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  4. Market Type vs Win Rate")
    print(f"{'═'*60}")
    rows = await conn.fetch("""
        SELECT
            market_type,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'TAKE_PROFIT' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(entry_price)::numeric, 2) AS avg_entry,
            ROUND(AVG(exit_return_pct)::numeric, 1) AS avg_return
        FROM alerts
        WHERE outcome IN ('TAKE_PROFIT', 'STOP_LOSS')
        AND market_type IS NOT NULL
        GROUP BY market_type
        ORDER BY total DESC
    """)
    print(f"\n  {'Market Type':<18} {'Total':>6} {'Wins':>6} {'Win%':>7} {'Avg Entry':>10} {'Avg Ret':>8}")
    print(f"  {'-'*60}")
    for r in rows:
        wr = round((r['wins'] or 0)/r['total']*100, 1) if r['total'] else 0
        print(f"  {str(r['market_type']):<18} {r['total']:>6} {r['wins']:>6} {wr:>6.1f}% {r['avg_entry']:>9.2f} {r['avg_return']:>7.1f}%")

    # ── 5. Signals fired vs win rate ───────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  5. Signal Win Rates — which signals actually predict wins?")
    print(f"{'═'*60}")
    all_trades = await conn.fetch("""
        SELECT signals_fired, outcome, exit_return_pct
        FROM alerts
        WHERE outcome IN ('TAKE_PROFIT', 'STOP_LOSS')
        AND signals_fired IS NOT NULL
    """)
    signal_stats = {}
    for row in all_trades:
        for sig in (row['signals_fired'] or '').split(','):
            sig = sig.strip()
            if not sig: continue
            signal_stats.setdefault(sig, {'wins': 0, 'total': 0, 'ret': []})
            signal_stats[sig]['total'] += 1
            if row['outcome'] == 'TAKE_PROFIT':
                signal_stats[sig]['wins'] += 1
            if row['exit_return_pct'] is not None:
                signal_stats[sig]['ret'].append(row['exit_return_pct'])

    print(f"\n  {'Signal':<25} {'Total':>6} {'Wins':>6} {'Win%':>7} {'Avg Ret':>8}")
    print(f"  {'-'*56}")
    for sig, d in sorted(signal_stats.items(), key=lambda x: -x[1]['total']):
        if d['total'] < 3: continue
        wr  = round(d['wins']/d['total']*100, 1)
        avg = round(sum(d['ret'])/len(d['ret']), 1) if d['ret'] else 0
        flag = " ⚠️  HURTS" if wr < 20 else " ✅" if wr > 40 else ""
        print(f"  {sig:<25} {d['total']:>6} {d['wins']:>6} {wr:>6.1f}% {avg:>7.1f}%{flag}")

    # ── 6. Fear & Greed regime vs win rate ────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  6. Fear & Greed Regime vs Win Rate")
    print(f"{'═'*60}")
    rows = await conn.fetch("""
        SELECT
            fear_greed_regime,
            COUNT(*) AS total,
            SUM(CASE WHEN outcome = 'TAKE_PROFIT' THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(entry_price)::numeric, 2) AS avg_entry,
            ROUND(AVG(exit_return_pct)::numeric, 1) AS avg_return
        FROM alerts
        WHERE outcome IN ('TAKE_PROFIT', 'STOP_LOSS')
        AND fear_greed_regime IS NOT NULL
        GROUP BY fear_greed_regime
        ORDER BY total DESC
    """)
    print(f"\n  {'Regime':<18} {'Total':>6} {'Wins':>6} {'Win%':>7} {'Avg Entry':>10} {'Avg Ret':>8}")
    print(f"  {'-'*60}")
    for r in rows:
        wr = round((r['wins'] or 0)/r['total']*100, 1) if r['total'] else 0
        print(f"  {str(r['fear_greed_regime']):<18} {r['total']:>6} {r['wins']:>6} {wr:>6.1f}% {r['avg_entry']:>9.2f} {r['avg_return']:>7.1f}%")

    # ── 7. Raw trade list for manual review ───────────────────────────────────
    print(f"\n{'═'*60}")
    print("  7. All Winning Trades — what did they look like?")
    print(f"{'═'*60}")
    rows = await conn.fetch("""
        SELECT
            question,
            entry_price,
            exit_return_pct,
            direction,
            market_type,
            signals_fired,
            fear_greed_regime,
            score
        FROM alerts
        WHERE outcome = 'TAKE_PROFIT'
        ORDER BY exit_return_pct DESC
    """)
    print(f"\n  {'Entry':>6} {'Return':>7} {'Dir':<10} {'Type':<14} {'Score':>5}  Question")
    print(f"  {'-'*80}")
    for r in rows:
        print(f"  {round((r['entry_price'] or 0)*100):>5}¢ {r['exit_return_pct']:>6.1f}%"
              f" {str(r['direction']):<10} {str(r['market_type']):<14} {r['score']:>5}"
              f"  {r['question'][:45]}")

    await conn.close()
    print("\n✅ Analysis complete\n")

asyncio.run(run())