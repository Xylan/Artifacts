#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MonsterStore: Local SQLite cache with update expiration timer for Artifacts MMO monsters database.
"""

import json
import time
from typing import Dict, List, Optional, Any
from .base_store import BaseStore


class MonsterStore(BaseStore):

    def __init__(
        self, 
        db_path: str = "artifacts_game.db", 
        api=None, 
        ttl_seconds: int = 86400
    ):
        super().__init__(db_path=db_path, api=api, ttl_seconds=ttl_seconds, table_name="monsters")
        self.update_key = "monsters_last_updated"
        self._init_db()

    def _init_db(self) -> None:
        """Creates the monsters and metadata tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS monsters (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    level INTEGER DEFAULT 1,
                    hp INTEGER DEFAULT 0,
                    attack_fire INTEGER DEFAULT 0,
                    attack_earth INTEGER DEFAULT 0,
                    attack_water INTEGER DEFAULT 0,
                    attack_air INTEGER DEFAULT 0,
                    res_fire INTEGER DEFAULT 0,
                    res_earth INTEGER DEFAULT 0,
                    res_water INTEGER DEFAULT 0,
                    res_air INTEGER DEFAULT 0,
                    min_gold INTEGER DEFAULT 0,
                    max_gold INTEGER DEFAULT 0,
                    raw_data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_monsters_level ON monsters(level);")
            conn.commit()

    async def sync_from_api(self, force: bool = False) -> int:
        """
        Fetches monsters from the API and caches them into SQLite.
        Only runs if forced, if the DB is empty, or if the update timer expired.
        """
        if not force and not self.is_cache_expired(self.update_key):
            last_up = self.get_last_updated(self.update_key)
            time_left_hrs = round((self.ttl_seconds - (time.time() - last_up)) / 3600, 1)
            print(f"[MonsterStore] Local cache valid ({self.count()} monsters). Next sync in ~{time_left_hrs}h.")
            return self.count()

        if not self.api:
            raise ValueError("API instance required to sync MonsterStore!")

        print("[MonsterStore] Cache expired or force flag set. Syncing complete monster catalog from API...")
        page = 1
        page_size = 100
        total_monsters = 0

        while True:
            res = await self.api.get_monsters(page=page, size=page_size)
            
            monsters_data = res.get("data", []) if isinstance(res, dict) else res
            if not monsters_data:
                break

            with self._get_connection() as conn:
                for monster in monsters_data:
                    code = monster["code"]
                    name = monster["name"]
                    level = monster.get("level", 1)
                    hp = monster.get("hp", 0)

                    attack_fire = monster.get("attack_fire", 0)
                    attack_earth = monster.get("attack_earth", 0)
                    attack_water = monster.get("attack_water", 0)
                    attack_air = monster.get("attack_air", 0)

                    res_fire = monster.get("res_fire", 0)
                    res_earth = monster.get("res_earth", 0)
                    res_water = monster.get("res_water", 0)
                    res_air = monster.get("res_air", 0)

                    min_gold = monster.get("min_gold", 0)
                    max_gold = monster.get("max_gold", 0)

                    raw_json = json.dumps(monster)

                    conn.execute("""
                        INSERT OR REPLACE INTO monsters 
                        (code, name, level, hp, attack_fire, attack_earth, attack_water, attack_air,
                         res_fire, res_earth, res_water, res_air, min_gold, max_gold, raw_data)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        code, name, level, hp, attack_fire, attack_earth, attack_water, attack_air,
                        res_fire, res_earth, res_water, res_air, min_gold, max_gold, raw_json
                    ))

                conn.commit()

            total_monsters += len(monsters_data)
            
            total_pages = res.get("pages", page) if isinstance(res, dict) else page
            if page >= total_pages or len(monsters_data) < page_size:
                break
            page += 1

        self.set_last_updated(self.update_key, time.time())
        print(f"[MonsterStore] Sync complete. Cached {total_monsters} monsters at {time.strftime('%Y-%m-%d %H:%M:%S')}.")
        return total_monsters

    def get_monster(self, code: str) -> Optional[Dict[str, Any]]:
        """Retrieves raw JSON payload for a single monster by code (e.g. 'chicken')."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT raw_data FROM monsters WHERE code = ?", (code,))
            row = cursor.fetchone()
            return json.loads(row["raw_data"]) if row else None

    def get_by_level_range(self, min_level: int = 1, max_level: int = 100) -> List[Dict[str, Any]]:
        """Returns all monsters within a specified combat level range."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT raw_data FROM monsters 
                WHERE level >= ? AND level <= ?
                ORDER BY level ASC
            """, (min_level, max_level))
            return [json.loads(row["raw_data"]) for row in cursor.fetchall()]

    def get_monsters_dropping_item(self, item_code: str) -> List[Dict[str, Any]]:
        """Finds all monsters that drop a specific item code in their drop table."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT raw_data FROM monsters")
            matching_monsters = []
            for row in cursor.fetchall():
                data = json.loads(row["raw_data"])
                drops = data.get("drops", [])
                if any(drop.get("code") == item_code for drop in drops):
                    matching_monsters.append(data)
            return matching_monsters