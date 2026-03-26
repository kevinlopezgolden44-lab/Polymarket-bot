import asyncio
import asyncpg
import os

async def run():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await conn.fetch("""
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_name = 'alerts' 
        ORDER BY ordinal_position
    """)
    print("\n--- alerts table columns ---")
    for r in rows:
        print(f"{r['column_name']:30} {r['data_type']}")
    await conn.close()

asyncio.run(run())