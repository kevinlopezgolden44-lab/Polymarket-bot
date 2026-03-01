import asyncio
import aiohttp
import logging
import os
import json
from datetime import datetime, timezone

CONFIG = {
    "check_interval_seconds": 30,
    "min_score_for_alert": 70,
    "max_daily_loss": 20,
    "max_trade_size": 5,
    "max_open_positions": 10,
    "markets_per_page": 100,
    "max_pages": 20,
    "summary_hour_utc": 13,
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

DB_FILE = "alerts_database.json"

def load_database():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"alerts": [], "price_history": {}, "daily_stats": []}

def save_database(db):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log.error("Failed to save database: %s", e)

def log_alert(db, opportunity):
    alert = {
        "id": opportunity["id"],
        "question": opportunity["question"],
        "yes_price_at_alert": opportunity["yes_price"],
        "score": opportunity["score"],
        "reason": opportunity["reason"],
        "volume_at_alert": opportunity["volume"],
        "alerted_at": datetime.now(timezone.utc).isoformat(),
        "outcome": None,
        "profitable": None,
        "price_history": []
    }
    db["alerts"].append(alert)
    save_database(db)
    log.info("Alert logged to database (total: %d)", len(db["alerts"]))

def update_price_history(db, market_id, yes_price):
    if market_id not in
