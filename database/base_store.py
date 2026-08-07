import sqlite3
import time
from typing import Optional


class BaseStore:
    """Shared base class providing database connections, pickling safety, generic counts, and TTL cache management."""

    def __init__(self, db_path: str = "artifacts_game.db", api=None, ttl_seconds: int = 86400, table_name: str = ""):
        self.db_path = db_path
        self.api = api
        self.ttl_seconds = ttl_seconds
        self.table_name = table_name
        self._configure_database()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _configure_database(self) -> None:
        """Enables Write-Ahead Logging (WAL) mode for better concurrent read/write handling."""
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.commit()

    def get_metadata(self, key: str) -> float:
        """Generic helper to retrieve a timestamp or float value from metadata."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return float(row["value"]) if row else 0.0

    def set_metadata(self, key: str, value: str, conn: Optional[sqlite3.Connection] = None) -> None:
        """Generic helper to save a key-value pair into the metadata table, supporting connection sharing."""
        if conn is not None:
            conn.execute("""
                INSERT OR REPLACE INTO metadata (key, value)
                VALUES (?, ?)
            """, (key, str(value)))
        else:
            with self._get_connection() as c:
                c.execute("""
                    INSERT OR REPLACE INTO metadata (key, value)
                    VALUES (?, ?)
                """, (key, str(value)))
                c.commit()

    def count(self) -> int:
        """Returns the total number of cached rows in the store's target table."""
        if not self.table_name:
            return 0
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(f"SELECT COUNT(*) FROM {self.table_name}")
                return cursor.fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def get_last_updated(self, key: str) -> float:
        """Retrieves the unix timestamp for a given update key."""
        return self.get_metadata(key)

    def set_last_updated(self, key: str, timestamp: float, conn: Optional[sqlite3.Connection] = None) -> None:
        """Updates the unix timestamp for a given update key, passing down active connections."""
        self.set_metadata(key, timestamp, conn=conn)

    def is_cache_expired(self, last_updated_key: str) -> bool:
        """True if the local cache is empty or has exceeded the TTL duration.
        Called once per process, at startup, via GameDatabase.sync_all()
        (main.py). Deliberately time-based rather than event-driven -- see
        ARCHITECTURE.md ("Static vs. live cache distinction") for why.
        """
        if self.count() == 0:
            return True
        last_updated = self.get_last_updated(last_updated_key)
        return (time.time() - last_updated) > self.ttl_seconds

    def __getstate__(self):
        """Prevents Spyder/pickle from crashing on non-picklable connections or API clients."""
        state = self.__dict__.copy()
        if "api" in state:
            state["api"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)