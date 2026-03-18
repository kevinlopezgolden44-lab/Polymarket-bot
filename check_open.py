import asyncio
import asyncpg
import os
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")

async def run():
    conn = await asyncpg.connect(DATABASE_URL)

    print("\n--- oldest open positions ---")
    rows = await conn.fetch("""
        SELECT question, yes_price, entry_price, alerted_at,
               ROUND(EXTRACT(EPOCH FROM (NOW() - alerted_at))/3600, 1) AS hours_open
        FROM alerts
        WHERE outcome IS NULL
        ORDER BY alerted_at ASC
        LIMIT 20
    """)
    for r in rows:
        print(f"{r['hours_open']}h open | entry={round((r['entry_price'] or r['yes_price'])*100)}¢ | {r['question'][:70]}")

    print("\n--- summary ---")
    row = await conn.fetchrow("""
        SELECT
            COUNT(*) AS total_open,
            ROUND(AVG(EXTRACT(EPOCH FROM (NOW() - alerted_at))/3600)::numeric, 1) AS avg_hours_open,
            ROUND(MAX(EXTRACT(EPOCH FROM (NOW() - alerted_at))/3600)::numeric, 1) AS max_hours_open
        FROM alerts
        WHERE outcome IS NULL
    """)
    print(f"total open: {row['total_open']}")
    print(f"avg hours open: {row['avg_hours_open']}")
    print(f"oldest: {row['max_hours_open']} hours")

    await conn.close()

asyncio.run(run())