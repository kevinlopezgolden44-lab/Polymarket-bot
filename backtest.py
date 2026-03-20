"""
Polymarket Bot — Historical Backtesting Engine
Reads from historical_markets table populated by fetch_gamma.py.

Start command: python backtest.py

Outputs to Railway logs:
  - Score gradient (win rate by score bucket)
  - Win rate by category and market type
  - Win rate by entry price bucket
  - Optimal score threshold finder
  - Signal effectiveness analysis
  - Primary vs validation stability comparison
"""

import asyncio
import asyncpg
import os
import sys
import re
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

# ─────────────────────────────────────────────
# Import bot's own detection functions
# ─────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from scoring import detect_category, detect_market_type
    log.info("Loaded scoring.py functions")
except ImportError:
    log.warning("Could not import scoring.py — using fallback detection")
    def detect_category(q):
        q = q.lower()
        if any(w in q for w in ["bitcoin","btc","ethereum","crypto","solana","xrp"]):
            return "Crypto"
        if re.search(r'\b(eth|sol|bnb)\b', q):
            return "Crypto"
        if " vs " in q or " vs. " in q:
            return "Sports"
        if re.search(r'\b(nba|nfl|mlb|nhl|ufc)\b', q):
            return "Sports"
        if any(w in q for w in ["election","president","senate","vote"]):
            return "Politics"
        if any(w in q for w in ["fed","inflation","gdp","cpi","interest rate"]):
            return "Economics"
        return "General"

    def detect_market_type(q):
        q = q.lower()
        if "dominance" in q:
            return "DOMINANCE"
        if any(w in q for w in ["reach","hit","exceed","above","below","dip"]) and ("$" in q or any(c.isdigit() for c in q)):
            return "PRICE_TARGET"
        if any(w in q for w in ["gain","rise","increase"]) and "%" in q:
            return "PERCENTAGE_MOVE"
        if any(w in q for w in ["etf","approve","halving","ban","sec"]):
            return "EVENT"
        if any(w in q for w in ["stay above","remain above","stay below","range"]):
            return "RANGE"
        return "GENERAL"


# ─────────────────────────────────────────────
# Scoring (signals available from historical data)
# ─────────────────────────────────────────────

def compute_score(row):
    """
    Score a historical market using only data available at creation time.

    Available signals from Gamma API historical data:
      ✅ yes_price / spread / liquidity
      ✅ days to resolution
      ✅ resolution ambiguity
      ✅ category / market type

    Not available (need live data at alert time):
      ❌ Fear & Greed
      ❌ Funding rates
      ❌ CLOB depth
      ❌ Price momentum / velocity (need price history series)
      ❌ Vegas odds
    """
    score = 50
    flags = []
    confirming = 0
    contradicting = 0

    question    = str(row["question"])
    yes_price   = row["initial_price"] or 0.5
    category    = detect_category(question)
    market_type = detect_market_type(question)

    # Longshot filter
    if yes_price < 0.10:
        return {"score": 0, "filtered": True, "filter_reason": "LONGSHOT",
                "category": category, "market_type": market_type, "flags": ""}

    # Spread
    bid = row["initial_bid"] or 0
    ask = row["initial_ask"] or 0
    if bid and ask:
        spread = ask - bid
        if spread > 0.06:
            score -= 15
            contradicting += 1
            flags.append("WIDE_SPREAD")
        elif spread <= 0.02:
            score += 5
            confirming += 1

    # Liquidity
    vol_24h = row["volume_24h_usd"] or 0
    if vol_24h < 500:
        score -= 20
        contradicting += 1
        flags.append("LOW_LIQUIDITY")
    else:
        score += 5
        confirming += 1

    # Days to resolution
    duration_hours = row["time_to_resolution_hours"]
    if duration_hours is not None:
        days = duration_hours / 24
        if days <= 3:
            if yes_price >= 0.20:
                score += 10
                confirming += 1
                flags.append("RESOLVES_SOON")
            else:
                score -= 5
                contradicting += 1
                flags.append("DECAYING_SHORT")
        elif days <= 14:
            score += 5
        elif days > 180:
            score -= 10
            contradicting += 1
        elif days > 60:
            score -= 5

    # Ambiguity
    q_lower = question.lower()
    vague   = ["approximately","around","about","roughly","sometime",
               "expected to","likely to","probably","may ","might ",
               "significant","major","notable"]
    precise = ["$","%","by end of","before","after","2024","2025","2026"]
    if sum(1 for p in vague if p in q_lower) >= 2 and not any(p in q_lower for p in precise):
        score -= 10
        contradicting += 1
        flags.append("AMBIGUOUS")

    score = max(0, min(100, score))

    return {
        "score":        score,
        "filtered":     False,
        "filter_reason": None,
        "category":     category,
        "market_type":  market_type,
        "confirming":   confirming,
        "contradicting": contradicting,
        "flags":        "|".join(flags),
    }


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def pct(wins, total):
    return f"{round(wins / total * 100, 1)}%" if total > 0 else "N/A"

def section(title):
    print(f"\n{'═'*65}")
    print(f"  {title}")
    print(f"{'═'*65}")


# ─────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────

def analyze_score_gradient(rows, label):
    section(f"Score Gradient — {label}")
    buckets = [
        ("<60",   lambda s: s < 60),
        ("60-70", lambda s: 60 <= s < 70),
        ("70-80", lambda s: 70 <= s < 80),
        ("80-85", lambda s: 80 <= s < 85),
        ("85-90", lambda s: 85 <= s < 90),
        ("90-95", lambda s: 90 <= s < 95),
        ("95-100",lambda s: s >= 95),
    ]
    print(f"\n  {'Bucket':<10} {'Total':>8} {'Wins':>8} {'Win Rate':>10} {'Avg Price':>10}")
    print(f"  {'-'*50}")
    for name, fn in buckets:
        subset = [r for r in rows if fn(r["score"])]
        total  = len(subset)
        wins   = sum(r["resolved_yes"] for r in subset)
        avg_p  = sum(r["initial_price"] for r in subset) / total if total else 0
        print(f"  {name:<10} {total:>8,} {wins:>8} {pct(wins,total):>10} {avg_p:>9.1%}")


def analyze_by_category(rows, label):
    section(f"Win Rate by Category — {label}")
    cats = {}
    for r in rows:
        c = r["category"]
        cats.setdefault(c, {"wins": 0, "total": 0, "score_sum": 0})
        cats[c]["total"] += 1
        cats[c]["wins"]  += r["resolved_yes"]
        cats[c]["score_sum"] += r["score"]
    print(f"\n  {'Category':<15} {'Total':>8} {'Wins':>8} {'Win Rate':>10} {'Avg Score':>10}")
    print(f"  {'-'*55}")
    for cat, d in sorted(cats.items(), key=lambda x: -x[1]["total"]):
        avg_s = d["score_sum"] / d["total"] if d["total"] else 0
        print(f"  {cat:<15} {d['total']:>8,} {d['wins']:>8} {pct(d['wins'],d['total']):>10} {avg_s:>9.1f}")
    return cats


def analyze_by_market_type(rows, label):
    section(f"Win Rate by Market Type — {label}")
    types = {}
    for r in rows:
        t = r["market_type"]
        types.setdefault(t, {"wins": 0, "total": 0})
        types[t]["total"] += 1
        types[t]["wins"]  += r["resolved_yes"]
    print(f"\n  {'Market Type':<18} {'Total':>8} {'Wins':>8} {'Win Rate':>10}")
    print(f"  {'-'*48}")
    for t, d in sorted(types.items(), key=lambda x: -x[1]["total"]):
        print(f"  {t:<18} {d['total']:>8,} {d['wins']:>8} {pct(d['wins'],d['total']):>10}")
    return types


def analyze_entry_price(rows, label):
    section(f"Win Rate by Entry Price — {label}")
    bins = [(0.10,0.20),(0.20,0.30),(0.30,0.40),(0.40,0.50),
            (0.50,0.60),(0.60,0.70),(0.70,0.80),(0.80,0.90),(0.90,0.96)]
    print(f"\n  {'Price Bucket':<14} {'Total':>8} {'Wins':>8} {'Win Rate':>10}")
    print(f"  {'-'*44}")
    for lo, hi in bins:
        subset = [r for r in rows if lo <= r["initial_price"] < hi]
        wins   = sum(r["resolved_yes"] for r in subset)
        label2 = f"{int(lo*100)}-{int(hi*100)}%"
        print(f"  {label2:<14} {len(subset):>8,} {wins:>8} {pct(wins,len(subset)):>10}")


def find_optimal_threshold(rows, label):
    section(f"Optimal Score Threshold — {label}")
    breakeven = 20 / (40 + 20)  # stop_loss / (take_profit + stop_loss)
    print(f"\n  Break-even win rate: {breakeven*100:.1f}%  (TP=+40%, SL=-20%)")
    print(f"\n  {'Threshold':>10} {'Total':>8} {'Win Rate':>10}")
    print(f"  {'-'*32}")

    best_wr   = 0
    best_thresh = 85
    for thresh in range(60, 100):
        subset = [r for r in rows if r["score"] >= thresh]
        if len(subset) < 20:
            continue
        wr = sum(r["resolved_yes"] for r in subset) / len(subset)
        marker  = " ← CURRENT" if thresh == 85 else ""
        if wr > best_wr and len(subset) >= 50:
            best_wr     = wr
            best_thresh = thresh
        print(f"  {thresh:>10} {len(subset):>8,} {wr*100:>9.1f}%{marker}")

    # Mark optimal
    print(f"\n  Optimal threshold: {best_thresh} → {best_wr*100:.1f}% win rate")
    current_subset = [r for r in rows if r["score"] >= 85]
    if current_subset:
        current_wr = sum(r["resolved_yes"] for r in current_subset) / len(current_subset)
        print(f"  Current (85):      {current_wr*100:.1f}% win rate ({len(current_subset):,} markets)")
    return best_thresh, best_wr


def analyze_signals(rows, label):
    section(f"Signal Effectiveness — {label}")
    signals = {
        "RESOLVES_SOON": "Should help (short-dated, liquid)",
        "LOW_LIQUIDITY": "Should hurt (avoid thin markets)",
        "WIDE_SPREAD":   "Should hurt (high entry cost)",
        "AMBIGUOUS":     "Should hurt (vague resolution)",
        "DECAYING_SHORT":"Should hurt (low price, near expiry)",
    }
    print(f"\n  {'Signal':<18} {'With':>8} {'WR With':>9} {'Without':>9} {'WR Without':>11} {'Impact':>8} {'Expected':>10}")
    print(f"  {'-'*80}")
    for sig, note in signals.items():
        with_sig    = [r for r in rows if sig in (r["flags"] or "")]
        without_sig = [r for r in rows if sig not in (r["flags"] or "")]
        if len(with_sig) < 5:
            continue
        wr_with    = sum(r["resolved_yes"] for r in with_sig) / len(with_sig) * 100
        wr_without = sum(r["resolved_yes"] for r in without_sig) / len(without_sig) * 100 if without_sig else 0
        impact = wr_with - wr_without
        # Check if impact is in expected direction
        expected_positive = sig == "RESOLVES_SOON"
        ok = "✅" if (impact > 0) == expected_positive else "⚠️ "
        print(f"  {sig:<18} {len(with_sig):>8,} {wr_with:>8.1f}% {wr_without:>8.1f}%  {impact:>+8.1f}pp {ok:>8}")


def compare_datasets(p_cats, v_cats, label):
    section(f"Primary vs Validation Stability — {label}")
    print(f"\n  {'Bucket':<18} {'Primary WR':>12} {'Validation WR':>14} {'Stable?':>8}")
    print(f"  {'-'*56}")
    all_keys = set(p_cats) | set(v_cats)
    for key in sorted(all_keys):
        p = p_cats.get(key, {})
        v = v_cats.get(key, {})
        p_wr = p["wins"]/p["total"]*100 if p.get("total", 0) >= 10 else None
        v_wr = v["wins"]/v["total"]*100 if v.get("total", 0) >= 10 else None
        if p_wr is None or v_wr is None:
            stable = "?"
        else:
            stable = "✅" if abs(p_wr - v_wr) <= 10 else "⚠️ "
        p_str = f"{p_wr:.1f}%" if p_wr is not None else "N/A"
        v_str = f"{v_wr:.1f}%" if v_wr is not None else "N/A"
        print(f"  {str(key):<18} {p_str:>12} {v_str:>14} {stable:>8}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

async def main():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        return

    conn = await asyncpg.connect(DATABASE_URL)

    # Check data exists
    counts = await conn.fetch("""
        SELECT dataset, COUNT(*) as n
        FROM historical_markets
        GROUP BY dataset
    """)
    if not counts:
        log.error("No data in historical_markets table — run fetch_gamma.py first")
        await conn.close()
        return

    for row in counts:
        log.info("Dataset '%s': %d markets", row["dataset"], row["n"])

    # Load all markets
    raw = await conn.fetch("""
        SELECT market_id, question, raw_category, dataset,
               initial_bid, initial_ask, initial_price,
               resolution_price, resolved_yes,
               total_volume_usd, volume_24h_usd,
               time_to_resolution_hours
        FROM historical_markets
        WHERE resolved_yes IS NOT NULL
          AND initial_price IS NOT NULL
          AND initial_price BETWEEN 0.05 AND 0.95
          AND total_volume_usd >= 1000
        ORDER BY dataset, initial_price
    """)
    await conn.close()

    log.info("Loaded %d markets from DB", len(raw))

    # Score all markets
    print("\n⚙️  Scoring markets...")
    scored = []
    filtered = 0
    for r in raw:
        result = compute_score(dict(r))
        if result["filtered"]:
            filtered += 1
            continue
        row = dict(r)
        row.update(result)
        scored.append(row)

    print(f"  Filtered (longshot): {filtered:,}")
    print(f"  Remaining:           {len(scored):,}")

    # Split datasets
    primary    = [r for r in scored if r["dataset"] == "primary"]
    validation = [r for r in scored if r["dataset"] == "validation"]

    print(f"  Primary:    {len(primary):,}")
    print(f"  Validation: {len(validation):,}")

    # ── Primary analysis ───────────────────────────────────────
    if primary:
        print(f"\n{'═'*65}")
        print(f"  PRIMARY DATASET — Apr 2025 to Mar 2026")
        print(f"  {len(primary):,} markets | "
              f"YES rate: {sum(r['resolved_yes'] for r in primary)/len(primary)*100:.1f}%")
        print(f"{'═'*65}")

        analyze_score_gradient(primary, "Primary")
        p_cats  = analyze_by_category(primary, "Primary")
        p_types = analyze_by_market_type(primary, "Primary")
        analyze_entry_price(primary, "Primary")
        best_thresh, best_wr = find_optimal_threshold(primary, "Primary")
        analyze_signals(primary, "Primary")

    # ── Validation analysis ────────────────────────────────────
    v_cats = {}
    if validation:
        print(f"\n{'═'*65}")
        print(f"  VALIDATION DATASET — Jan 2024 to Mar 2025")
        print(f"  {len(validation):,} markets | "
              f"YES rate: {sum(r['resolved_yes'] for r in validation)/len(validation)*100:.1f}%")
        print(f"{'═'*65}")

        analyze_score_gradient(validation, "Validation")
        v_cats  = analyze_by_category(validation, "Validation")
        analyze_by_market_type(validation, "Validation")
        analyze_entry_price(validation, "Validation")
        find_optimal_threshold(validation, "Validation")
        analyze_signals(validation, "Validation")

    # ── Cross-dataset stability ────────────────────────────────
    if primary and validation:
        compare_datasets(p_cats, v_cats, "Category")

    # ── Final recommendations ──────────────────────────────────
    section("RECOMMENDATIONS")
    if primary:
        current = [r for r in primary if r["score"] >= 85]
        current_wr = sum(r["resolved_yes"] for r in current) / len(current) * 100 if current else 0
        print(f"""
  Current threshold (85): {current_wr:.1f}% win rate ({len(current):,} markets)
  Optimal threshold ({best_thresh}):   {best_wr*100:.1f}% win rate
  Break-even:             33.3% (TP=+40%, SL=-20%)

  Interpretation guide:
    • Score gradient flat    → weights not discriminating, rebuild
    • Signal 'HURTS' marker  → that signal's weight should be reduced
    • Primary/Validation >10pp apart → finding is regime-specific, don't use
    • Primary/Validation <10pp apart → finding is robust, act on it
    • Optimal threshold > 85 → raise your alert threshold in bot.py
    • Optimal threshold < 85 → current threshold may be too conservative

  After reviewing, update signal weights in scoring.py and rerun
  this backtest to confirm improvement before deploying.
""")


if __name__ == "__main__":
    asyncio.run(main())