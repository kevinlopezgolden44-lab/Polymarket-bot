import asyncio
import asyncpg
import os

async def run():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])

    rows = await conn.fetch("""
        SELECT dataset, COUNT(*) as total,
               SUM(resolved_yes) as yes_count,
               ROUND(AVG(resolved_yes::float) * 100, 1) as yes_rate,
               MIN(end_date_str) as oldest,
               MAX(end_date_str) as newest
        FROM historical_markets
        GROUP BY dataset
        ORDER BY dataset
    """)

    print("\n--- historical_markets summary ---")
    for r in rows:
        print(f"dataset={r['dataset']} total={r['total']} yes_rate={r['yes_rate']}% dates={r['oldest']}→{r['newest']}")

    total = await conn.fetchval("SELECT COUNT(*) FROM historical_markets")
    print(f"\nTotal records: {total}")

    await conn.close()

asyncio.run(run())