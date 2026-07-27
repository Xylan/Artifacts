import heapq
import json
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from models import Position, Location
from .base_store import BaseStore


class MapStore(BaseStore):

    def __init__(self, db_path: str = "artifacts_game.db", api=None, max_age_seconds: int = 86400):
        super().__init__(db_path=db_path, api=api, ttl_seconds=max_age_seconds, table_name="maps")
        self.update_key = "maps_last_updated"
        self._init_db()

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS maps (
                    map_id INTEGER PRIMARY KEY,
                    layer TEXT NOT NULL,
                    x INTEGER NOT NULL,
                    y INTEGER NOT NULL,
                    name TEXT,
                    skin TEXT,
                    access_type TEXT,
                    access_conditions TEXT,
                    content_type TEXT,
                    content_code TEXT,
                    target_layer TEXT,
                    target_x INTEGER,
                    target_y INTEGER,
                    transition_conditions TEXT,
                    UNIQUE(layer, x, y)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
            self._ensure_condition_columns(conn)

    def _ensure_condition_columns(self, conn) -> None:
        """Adds access_conditions/transition_conditions columns to pre-existing DBs that predate this change."""
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(maps)").fetchall()}
        if "access_conditions" not in existing_cols:
            conn.execute("ALTER TABLE maps ADD COLUMN access_conditions TEXT")
        if "transition_conditions" not in existing_cols:
            conn.execute("ALTER TABLE maps ADD COLUMN transition_conditions TEXT")
        conn.commit()

    # --- Sync & Cache Integration ---
    
    async def sync_from_api(self, force: bool = False) -> int:
        """Fetches all map tiles from the API and saves them to SQLite if cache expired."""
        if not force and not self.is_cache_expired(self.update_key):
            last_up = self.get_last_updated(self.update_key)
            time_left_hrs = round((self.ttl_seconds - (time.time() - last_up)) / 3600, 1)
            print(f"[MapStore] Local cache valid ({self.count()} map tiles). Next sync in ~{time_left_hrs}h.")
            return self.count()

        if not self.api:
            raise ValueError("API instance required to sync MapStore!")

        print("[MapStore] Cache expired or force flag set. Syncing maps from API...")
        page = 1
        page_size = 100
        total_tiles = 0

        while True:
            res = await self.api.get_maps(page=page, size=page_size)
            maps_data = res.get("data", []) if isinstance(res, dict) else res
            if not maps_data:
                break

            self.save_maps(maps_data)
            total_tiles += len(maps_data)

            total_pages = res.get("pages", page) if isinstance(res, dict) else page
            if page >= total_pages or len(maps_data) < page_size:
                break
            page += 1

        print(f"[MapStore] Map sync complete. Cached {total_tiles} map tiles.")
        return total_tiles

    def _normalize_location(
        self, 
        loc_input: Union[Tuple[int, int], Tuple[int, int, str], Position, Location, object], 
        layer: Optional[str] = None
    ) -> Tuple[int, int, str]:
        if hasattr(loc_input, "location"):
            loc_input = loc_input.location

        if hasattr(loc_input, "position") and hasattr(loc_input, "layer"):
            return (loc_input.position.x, loc_input.position.y, loc_input.layer or layer or "overworld")

        if hasattr(loc_input, "x") and hasattr(loc_input, "y"):
            selected_layer = layer or "overworld"
            return (loc_input.x, loc_input.y, selected_layer)

        if isinstance(loc_input, (tuple, list)):
            x = loc_input[0]
            y = loc_input[1]
            selected_layer = loc_input[2] if len(loc_input) > 2 else (layer or "overworld")
            return (x, y, selected_layer)

        raise ValueError(f"Unrecognized location input type: {type(loc_input)}")


    def save_maps(self, maps_data: List[dict]):
        with self._get_connection() as conn:
            for tile in maps_data:
                interactions = tile.get("interactions") or {}

                # NOTE: "content" (monster/resource/workshop) and "transition" are SIBLING
                # keys under "interactions" per the /maps schema - transition is NOT nested
                # inside content. Pulling it from content (as before) always returned nothing.
                content = interactions.get("content") or tile.get("content") or {}
                transition = interactions.get("transition") or tile.get("transition") or {}

                access_data = tile.get("access") or {}
                access_type = (
                    access_data.get("type") if isinstance(access_data, dict) else tile.get("access_type")
                ) or "standard"
                access_conditions = access_data.get("conditions") if isinstance(access_data, dict) else None
                transition_conditions = transition.get("conditions")

                conn.execute("""
                    INSERT INTO maps (
                        map_id, layer, x, y, name, skin,
                        access_type, access_conditions,
                        content_type, content_code,
                        target_layer, target_x, target_y, transition_conditions
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(layer, x, y) DO UPDATE SET
                        map_id=excluded.map_id,
                        name=excluded.name,
                        access_type=excluded.access_type,
                        access_conditions=excluded.access_conditions,
                        content_type=excluded.content_type,
                        content_code=excluded.content_code,
                        target_layer=excluded.target_layer,
                        target_x=excluded.target_x,
                        target_y=excluded.target_y,
                        transition_conditions=excluded.transition_conditions
                """, (
                    tile.get("map_id"),
                    tile.get("layer", "overworld"),
                    tile["x"],
                    tile["y"],
                    tile.get("name"),
                    tile.get("skin"),
                    access_type,
                    json.dumps(access_conditions) if access_conditions else None,
                    content.get("type"),
                    content.get("code"),
                    transition.get("layer"),
                    transition.get("x"),
                    transition.get("y"),
                    json.dumps(transition_conditions) if transition_conditions else None,
                ))

            self.set_last_updated(self.update_key, time.time(), conn=conn)
            conn.commit()

    # --- Query & Pathfinding Extensions ---

    def get_walkable_tiles(
        self, 
        layer: Optional[Union[str, Tuple[int, int], Position, Location, object]] = None
    ) -> Set[Tuple[int, int, str]]:
        with self._get_connection() as conn:
            if layer:
                _, _, target_layer = self._normalize_location((0, 0), layer=str(layer) if isinstance(layer, str) else None)
                cursor = conn.execute(
                    "SELECT x, y, layer FROM maps WHERE (access_type IS NULL OR access_type != 'blocked') AND layer = ?",
                    (target_layer,),
                )
            else:
                cursor = conn.execute("SELECT x, y, layer FROM maps WHERE access_type IS NULL OR access_type != 'blocked'")
            return {(row[0], row[1], row[2]) for row in cursor.fetchall()}

    def get_transitions(self) -> Dict[Tuple[int, int, str], Tuple[int, int, str]]:
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT x, y, layer, target_x, target_y, target_layer 
                FROM maps 
                WHERE target_layer IS NOT NULL AND target_x IS NOT NULL AND target_y IS NOT NULL
            """)
            return {
                (r[0], r[1], r[2]): (r[3], r[4], r[5]) 
                for r in cursor.fetchall()
            }

    def get_tile_conditions(
        self, 
        location: Union[Tuple[int, int], Tuple[int, int, str], Position, Location, object],
        layer: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Returns the raw access/transition condition lists for a tile, e.g.
        {"access": [{"code": "level", "operator": "gte", "value": 10}], "transition": [...]}.
        Empty lists mean no conditions are attached (tile is unconditionally usable)."""
        x, y, target_layer = self._normalize_location(location, layer)
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT access_conditions, transition_conditions FROM maps WHERE x = ? AND y = ? AND layer = ?",
                (x, y, target_layer),
            )
            row = cursor.fetchone()
            if not row:
                return {"access": [], "transition": []}

            access_raw, transition_raw = row[0], row[1]
            return {
                "access": json.loads(access_raw) if access_raw else [],
                "transition": json.loads(transition_raw) if transition_raw else [],
            }

    def check_conditions(self, character, conditions: List[Dict[str, Any]]) -> bool:
        """Evaluates a list of {code, operator, value} conditions against character attributes.
        Checks Character, then Stats/Skills/Equipment as update_from_dict does. Unrecognized
        codes/operators fail closed (return False) rather than silently passing."""
        if not conditions:
            return True

        operators = {
            "eq": lambda a, b: a == b,
            "ne": lambda a, b: a != b,
            "gt": lambda a, b: a > b,
            "gte": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "lte": lambda a, b: a <= b,
        }

        for cond in conditions:
            code = cond.get("code")
            op = cond.get("operator")
            expected = cond.get("value")

            actual = None
            if hasattr(character, code):
                actual = getattr(character, code)
            else:
                for nested_obj in [getattr(character, "stats", None), getattr(character, "skills", None), getattr(character, "equipment", None)]:
                    if nested_obj is not None and hasattr(nested_obj, code):
                        actual = getattr(nested_obj, code)
                        break

            compare = operators.get(op)
            if actual is None or compare is None:
                print(f"[MapStore] Unable to evaluate condition code={code!r} operator={op!r}; failing closed.")
                return False

            if not compare(actual, expected):
                return False

        return True

    def get_neighbors(
        self, 
        current: Union[Tuple[int, int], Position, Location, object], 
        walkable: Set[Tuple[int, int, str]], 
        transitions: Dict[Tuple[int, int, str], Tuple[int, int, str]]
    ) -> List[Tuple[int, int, str]]:
        cur_tuple = self._normalize_location(current)
        x, y, layer = cur_tuple
        neighbors = []

        cardinals = [(x + 1, y, layer), (x - 1, y, layer), (x, y + 1, layer), (x, y - 1, layer)]
        for n in cardinals:
            if n in walkable:
                neighbors.append(n)

        if cur_tuple in transitions:
            neighbors.append(transitions[cur_tuple])

        return neighbors

    def find_content(
        self, 
        content_identifier: str, 
        layer: Optional[Union[str, Tuple[int, int], Position, Location, object]] = "overworld"
    ) -> Optional[Tuple[int, int, str]]:
        _, _, target_layer = self._normalize_location((0, 0), layer=str(layer) if isinstance(layer, str) else None)
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT x, y, layer FROM maps 
                WHERE (content_code = ? OR content_type = ?) 
                  AND layer = ? 
                LIMIT 1
            """,
                (content_identifier, content_identifier, target_layer),
            )
            row = cursor.fetchone()
            return (row[0], row[1], row[2]) if row else None

    def find_all(
        self, 
        content_identifier: str, 
        layer: Optional[Union[str, Tuple[int, int], Position, Location, object]] = None
    ) -> List[Tuple[int, int, str]]:
        with self._get_connection() as conn:
            if layer:
                _, _, target_layer = self._normalize_location((0, 0), layer=str(layer) if isinstance(layer, str) else None)
                cursor = conn.execute("""
                    SELECT x, y, layer FROM maps 
                    WHERE (content_code = ? OR content_type = ?) 
                      AND layer = ?
                """, (content_identifier, content_identifier, target_layer))
            else:
                cursor = conn.execute("""
                    SELECT x, y, layer FROM maps 
                    WHERE (content_code = ? OR content_type = ?)
                """, (content_identifier, content_identifier))
            return [(row[0], row[1], row[2]) for row in cursor.fetchall()]

    def get_shortest_path(
        self,
        start: Union[Tuple[int, int], Position, Location, object],
        goal: Union[Tuple[int, int], Position, Location, object],
        layer: Optional[str] = "overworld",
    ) -> List[Tuple[int, int, str]]:
        start_node = self._normalize_location(start, layer)
        goal_node = self._normalize_location(goal, layer)

        walkable = self.get_walkable_tiles()
        transitions = self.get_transitions()

        if start_node not in walkable or goal_node not in walkable:
            print(f"[Pathfinder] Start {start_node} or Goal {goal_node} is not a walkable tile!")
            return []

        def heuristic(a: Tuple[int, int, str], b: Tuple[int, int, str]) -> int:
            dist = abs(a[0] - b[0]) + abs(a[1] - b[1])
            if a[2] != b[2]:
                dist += 10
            return dist

        open_set = [(0, start_node)]
        came_from: Dict[Tuple[int, int, str], Tuple[int, int, str]] = {}

        g_score = {start_node: 0}
        f_score = {start_node: heuristic(start_node, goal_node)}

        while open_set:
            _, current = heapq.heappop(open_set)

            if current == goal_node:
                path = []
                while current in came_from:
                    path.append(current)
                    current = came_from[current]
                path.reverse()
                return path

            for neighbor in self.get_neighbors(current, walkable, transitions):
                tentative_g = g_score[current] + 1

                if neighbor not in g_score or tentative_g < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    f_score[neighbor] = tentative_g + heuristic(neighbor, goal_node)
                    heapq.heappush(open_set, (f_score[neighbor], neighbor))

        return []

    def find_closest(
        self, 
        from_target: Union[Tuple[int, int], Position, Location, object], 
        content_identifier: str, 
        layer: Optional[str] = None
    ) -> Optional[Tuple[int, int, str]]:
        start_node = self._normalize_location(from_target, layer)
        start_x, start_y, start_layer = start_node

        candidates = self.find_all(content_identifier, layer=start_layer)
        if not candidates:
            candidates = self.find_all(content_identifier, layer=None)

        if not candidates:
            return None

        same_layer_candidates = [c for c in candidates if c[2] == start_layer]

        if same_layer_candidates:
            return min(
                same_layer_candidates,
                key=lambda pos: abs(pos[0] - start_x) + abs(pos[1] - start_y)
            )

        best_candidate = None
        min_steps = float("inf")

        for target in candidates:
            path = self.get_shortest_path(start_node, target)
            if path and len(path) < min_steps:
                min_steps = len(path)
                best_candidate = target

        return best_candidate
