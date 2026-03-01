import asyncio
import aiohttp
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

async def main():
    url = "https://clob.polymarket.com/markets?active=true&closed=false&limit=5"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": "https://polymarket.com",
        "Referer": "https://polymarket.com/"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            markets = data.get("data", [])
            log.info("Got %d markets", len(markets))
            if markets:
                m = markets[0]
                log.info("First market question: %s", m.get("question", "none"))
                log.info("Tokens field: %s", m.get("tokens", "MISSING"))
                log.info("Volume field: %s", m.get("volume", "MISSING"))
                log.info("Active field: %s", m.get("active", "MISSING"))
                log.info("Closed field: %s", m.get("closed", "MISSING"))
                log.info("Full keys: %s", list(m.keys()))

asyncio.run(main())