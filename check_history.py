import asyncio
import asyncpg
import os

async def run():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    print("\n--- historical_markets summary ---")
    for dataset in ["primary", "validation"]:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
        )
        yes = await conn.fetchval(
            "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1 AND resolved_yes=1", dataset
        )
        oldest = await conn.fetchval(
            "SELECT MIN(end_date_str) FROM historical_markets WHERE dataset=$1", dataset
        )
        newest = await conn.fetchval(
            "SELECT MAX(end_date_str) FROM historical_markets WHERE dataset=$1", dataset
        )
        rate = round((yes or 0) / total * 100, 1) if total else 0
        print(f"dataset={dataset} total={total} yes_rate={rate}% dates={oldest} to {newest}")
    total = await conn.fetchval("SELECT COUNT(*) FROM historical_markets")
    print(f"\nTotal: {total}")
    await conn.close()

asyncio.run(run())