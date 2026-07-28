import json
import time
from typing import Dict, List, Optional, Any
from .base_store import BaseStore
from models import Item

class ItemStore(BaseStore):

    def __init__(
        self, 
        db_path: str = "artifacts_game.db", 
        api=None, 
        ttl_seconds: int = 86400
    ):
        super().__init__(db_path=db_path, api=api, ttl_seconds=ttl_seconds, table_name="items")
        self.update_key = "items_last_updated"
        self._init_db()

    def _init_db(self) -> None:
        """Creates the items and metadata tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    subtype TEXT,
                    level INTEGER DEFAULT 1,
                    tradeable INTEGER DEFAULT 1,
                    craft_skill TEXT,
                    craft_level INTEGER,
                    raw_data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_craft ON items(craft_skill, craft_level);")
            conn.commit()

    async def sync_from_api(self, force: bool = False) -> int:
        if not force and not self.is_cache_expired(self.update_key):
            last_up = self.get_last_updated(self.update_key)
            time_left_hrs = round((self.ttl_seconds - (time.time() - last_up)) / 3600, 1)
            print(f"[ItemStore] Local cache valid ({self.count()} items). Next sync in ~{time_left_hrs}h.")
            return self.count()

        if not self.api:
            raise ValueError("API instance required to sync ItemStore!")

        print("[ItemStore] Cache expired or force flag set. Syncing complete item catalog from API...")
        page = 1
        page_size = 100
        total_items = 0

        while True:
            res = await self.api.get_items(page=page, size=page_size)
            
            items_data = res.get("data", []) if isinstance(res, dict) else res
            if not items_data:
                break

            with self._get_connection() as conn:
                for item in items_data:
                    code = item["code"]
                    name = item["name"]
                    item_type = item.get("type", "")
                    subtype = item.get("subtype", "")
                    level = item.get("level", 1)
                    tradeable = 1 if item.get("tradeable", True) else 0

                    craft_info = item.get("craft") or {}
                    craft_skill = craft_info.get("skill")
                    craft_level = craft_info.get("level")

                    raw_json = json.dumps(item)

                    conn.execute("""
                        INSERT OR REPLACE INTO items 
                        (code, name, type, subtype, level, tradeable, craft_skill, craft_level, raw_data)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (code, name, item_type, subtype, level, tradeable, craft_skill, craft_level, raw_json))

                conn.commit()

            total_items += len(items_data)
            
            total_pages = res.get("pages", page) if isinstance(res, dict) else page
            if page >= total_pages or len(items_data) < page_size:
                break
            page += 1

        with self._get_connection() as conn:
            self.set_last_updated(self.update_key, time.time(), conn=conn)
            conn.commit()
        print(f"[ItemStore] Item sync complete. Cached {total_items} items.")
        return total_items

    def get_item(self, code: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT raw_data FROM items WHERE code = ?", (code,))
            row = cursor.fetchone()
            return json.loads(row["raw_data"]) if row else None

    def get_by_type(self, item_type: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT raw_data FROM items WHERE type = ?", (item_type,))
            return [json.loads(row["raw_data"]) for row in cursor.fetchall()]

    def get_craftable_by_skill(self, skill: str, max_level: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT raw_data FROM items 
                WHERE craft_skill = ? AND craft_level <= ?
                ORDER BY craft_level ASC
            """, (skill, max_level))
            return [json.loads(row["raw_data"]) for row in cursor.fetchall()]

    def get_recipe(self, code: str) -> Optional[Dict[str, Any]]:
        item = self.get_item(code)
        if item and "craft" in item and item["craft"]:
            return item["craft"]
        return None

    # ------------------------------------------------------------------
    # Typed Item accessors + character-aware queries
    # ------------------------------------------------------------------

    def get_item_obj(self, code: str) -> Optional[Item]:
        """Same as get_item(), but returns the typed Item dataclass (models.py)
        instead of a raw dict."""
        raw = self.get_item(code)
        return Item.from_dict(raw) if raw else None

    def get_all_items_obj(self) -> List[Item]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT raw_data FROM items")
            return [Item.from_dict(json.loads(row["raw_data"])) for row in cursor.fetchall()]

    def _conditions_met(self, character, conditions) -> bool:
        """Evaluates a list of ItemCondition against character attributes.
        Mirrors MapStore.check_conditions -- same idea, scoped to items
        instead of map tiles."""
        ops = {
            "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
            "gt": lambda a, b: a > b, "lt": lambda a, b: a < b,
            "gte": lambda a, b: a >= b, "lte": lambda a, b: a <= b,
        }
        for cond in conditions:
            actual = getattr(character, cond.code, None)
            if actual is None:
                for nested in (character.stats, character.skills, character.equipment):
                    if hasattr(nested, cond.code):
                        actual = getattr(nested, cond.code)
                        break
            compare = ops.get(cond.operator)
            if actual is None or compare is None or not compare(actual, cond.value):
                return False
        return True

    def meets_conditions(self, character, conditions) -> bool:
        """Public wrapper around _conditions_met, for callers outside this
        module (e.g. planning.GearList.for_upgrades) that need to check an
        item's conditions against a character."""
        return self._conditions_met(character, conditions)

    def get_craftable_for_character(self, character) -> List[Item]:
        """Items whose craft-skill requirement your character currently meets."""
        out = []
        for item in self.get_all_items_obj():
            if not item.craft:
                continue
            skill_level = getattr(character.skills, f"{item.craft.skill}_level", None)
            if skill_level is not None and skill_level >= item.craft.level:
                out.append(item)
        return out

    def get_equipable_for_character(self, character) -> List[Item]:
        """Equipment-slot items your character's level/conditions currently allow."""
        out = []
        for item in self.get_all_items_obj():
            if not item.is_equipable or item.level > character.level:
                continue
            if self._conditions_met(character, item.conditions):
                out.append(item)
        return out