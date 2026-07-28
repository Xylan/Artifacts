#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ResourceStore: Local SQLite cache with update expiration timer for Artifacts MMO
gatherable resources database (ore/tree/fishing spot nodes, as opposed to monsters).
"""

import json
import time
from typing import Dict, List, Optional, Any
from .base_store import BaseStore
from models import Resource


class ResourceStore(BaseStore):

    def __init__(
        self,
        db_path: str = "artifacts_game.db",
        api=None,
        ttl_seconds: int = 86400
    ):
        super().__init__(db_path=db_path, api=api, ttl_seconds=ttl_seconds, table_name="resources")
        self.update_key = "resources_last_updated"
        self._init_db()

    def _init_db(self) -> None:
        """Creates the resources and metadata tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS resources (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    skill TEXT,
                    level INTEGER DEFAULT 1,
                    raw_data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_resources_skill ON resources(skill, level);")
            conn.commit()

    async def sync_from_api(self, force: bool = False) -> int:
        """
        Fetches gatherable resources from the API and caches them into SQLite.
        Only runs if forced, if the DB is empty, or if the update timer expired.
        """
        if not force and not self.is_cache_expired(self.update_key):
            last_up = self.get_last_updated(self.update_key)
            time_left_hrs = round((self.ttl_seconds - (time.time() - last_up)) / 3600, 1)
            print(f"[ResourceStore] Local cache valid ({self.count()} resources). Next sync in ~{time_left_hrs}h.")
            return self.count()

        if not self.api:
            raise ValueError("API instance required to sync ResourceStore!")

        print("[ResourceStore] Cache expired or force flag set. Syncing complete resource catalog from API...")
        page = 1
        page_size = 100
        total_resources = 0

        while True:
            res = await self.api.get_resources(page=page, size=page_size)

            resources_data = res.get("data", []) if isinstance(res, dict) else res
            if not resources_data:
                break

            with self._get_connection() as conn:
                for resource in resources_data:
                    code = resource["code"]
                    name = resource["name"]
                    skill = resource.get("skill")
                    level = resource.get("level", 1)

                    raw_json = json.dumps(resource)

                    conn.execute("""
                        INSERT OR REPLACE INTO resources
                        (code, name, skill, level, raw_data)
                        VALUES (?, ?, ?, ?, ?)
                    """, (code, name, skill, level, raw_json))

                conn.commit()

            total_resources += len(resources_data)

            total_pages = res.get("pages", page) if isinstance(res, dict) else page
            if page >= total_pages or len(resources_data) < page_size:
                break
            page += 1

        self.set_last_updated(self.update_key, time.time())
        print(f"[ResourceStore] Sync complete. Cached {total_resources} resources at {time.strftime('%Y-%m-%d %H:%M:%S')}.")
        return total_resources

    def get_resource(self, code: str) -> Optional[Dict[str, Any]]:
        """Retrieves raw JSON payload for a single resource node by code (e.g. 'copper_rocks')."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT raw_data FROM resources WHERE code = ?", (code,))
            row = cursor.fetchone()
            return json.loads(row["raw_data"]) if row else None

    def get_resource_obj(self, code: str) -> Optional[Resource]:
        """Same as get_resource(), but returns the typed Resource dataclass
        (models.py) instead of a raw dict."""
        raw = self.get_resource(code)
        return Resource.from_dict(raw) if raw else None

    def get_by_skill(self, skill: str, max_level: int = 100) -> List[Dict[str, Any]]:
        """Returns all resource nodes gatherable with a given skill, up to a max level, ordered by level."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT raw_data FROM resources
                WHERE skill = ? AND level <= ?
                ORDER BY level ASC
            """, (skill, max_level))
            return [json.loads(row["raw_data"]) for row in cursor.fetchall()]

    def get_resources_dropping_item(self, item_code: str) -> List[Dict[str, Any]]:
        """Finds all resource nodes that drop a specific item code in their drop table."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT raw_data FROM resources")
            matching_resources = []
            for row in cursor.fetchall():
                data = json.loads(row["raw_data"])
                drops = data.get("drops", [])
                if any(drop.get("code") == item_code for drop in drops):
                    matching_resources.append(data)
            return matching_resources

    def find_best_for_item(self, item_code: str) -> Optional[Dict[str, Any]]:
        """Picks the lowest-level resource node that drops item_code -- used
        by planning.GearList.resolve() to turn a needed raw material into a
        gather task."""
        candidates = self.get_resources_dropping_item(item_code)
        if not candidates:
            return None
        return min(candidates, key=lambda r: r.get("level", 1))