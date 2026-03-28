import aiosqlite
import logging
import os
from contextlib import asynccontextmanager

from .config import DB_PATH

logger = logging.getLogger(__name__)

_db_path = os.path.abspath(DB_PATH)


@asynccontextmanager
async def get_db():
    """Async context manager: `async with get_db() as db:`."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db


async def init_db():
    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    logger.info("Initializing database at %s", _db_path)
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_serial TEXT NOT NULL UNIQUE,
                reserved_by   TEXT NOT NULL,
                reserved_at   TEXT NOT NULL,
                released_at   TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reservation_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_serial TEXT NOT NULL,
                reserved_by   TEXT NOT NULL,
                reserved_at   TEXT NOT NULL,
                released_at   TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_serial
            ON reservation_history(device_serial)
        """)

        await db.commit()
    logger.info("Database ready")
