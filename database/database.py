import asyncio
from typing import Dict, Any, Tuple
from config import DB_PATH
from .map_store import MapStore
from .item_store import ItemStore
from .monster_store import MonsterStore
from .resource_store import ResourceStore

class GameDatabase():
    """Unified manager sharing a single SQLite file across domain stores."""

    def __init__(
        self, 
        db_path: str = DB_PATH, 
        api=None, 
        ttl_seconds: int = 86400
    ):
        self.db_path = db_path
        self.api = api

        # Both stores share the exact same db file and api reference
        self.maps = MapStore(db_path=self.db_path, api=self.api, max_age_seconds=ttl_seconds)
        self.items = ItemStore(db_path=self.db_path, api=self.api, ttl_seconds=ttl_seconds)
        self.monsters = MonsterStore(db_path=self.db_path, api=self.api, ttl_seconds=ttl_seconds)
        self.resources = ResourceStore(db_path=self.db_path, api=self.api, ttl_seconds=ttl_seconds)

    async def sync_all(self, force: bool = False, concurrent: bool = False) -> Tuple[int, int, int]:
        """Delegates cache refresh sequentially with a shared connection context to avoid locks."""
        # Use a single shared connection for the entire sync routine to prevent locking
        # Temporarily share connection or execute sequentially
        map_count = await self.maps.sync_from_api(force=force)
        item_count = await self.items.sync_from_api(force=force)
        monster_count = await self.monsters.sync_from_api(force=force)
        resource_count = await self.resources.sync_from_api(force=force)
        return map_count, item_count, monster_count, resource_count