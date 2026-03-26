import asyncio
import asyncpg
import os

async def run():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    
    print("\n--- outcome values ---")
    rows = await conn.fetch("SELECT outcome, COUNT(*) as count FROM alerts GROUP BY outcome ORDER BY count DESC")
    for r in rows:
        print(f"{str(r['outcome']):30} {r['count']}")

    print("\n--- direction values ---")
    rows = await conn.fetch("SELECT direction, COUNT(*) as count FROM alerts GROUP BY direction ORDER BY count DESC")
    for r in rows:
        print(f"{str(r['direction']):30} {r['count']}")

    print("\n--- profitable values ---")
    rows = await conn.fetch("SELECT profitable, COUNT(*) as count FROM alerts GROUP BY profitable ORDER BY count DESC")
    for r in rows:
        print(f"{str(r['profitable']):30} {r['count']}")

    print("\n--- sample of recent resolved rows ---")
    rows = await conn.fetch("""
        SELECT id, outcome, profitable, exit_return_pct, direction 
        FROM alerts 
        WHERE exit_return_pct IS NOT NULL 
        ORDER BY alerted_at DESC 
        LIMIT 10
    """)
    for r in rows:
        print(dict(r))

    await conn.close()

asyncio.run(run())