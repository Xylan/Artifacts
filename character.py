import functools
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import (
    Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple, Union,
)
from datetime import datetime, timezone
import time
import math
import asyncio

from database import MapStore
from models import Position, Location, InventoryItem, Task, parse_reset


@dataclass
class Skills:
    mining_level: int = 1
    mining_xp: int = 0
    mining_max_xp: int = 150
    woodcutting_level: int = 1
    woodcutting_xp: int = 0
    woodcutting_max_xp: int = 150
    fishing_level: int = 1
    fishing_xp: int = 0
    fishing_max_xp: int = 150
    weaponcrafting_level: int = 1
    weaponcrafting_xp: int = 0
    weaponcrafting_max_xp: int = 150
    gearcrafting_level: int = 1
    gearcrafting_xp: int = 0
    gearcrafting_max_xp: int = 150
    jewelrycrafting_level: int = 1
    jewelrycrafting_xp: int = 0
    jewelrycrafting_max_xp: int = 150
    cooking_level: int = 1
    cooking_xp: int = 0
    cooking_max_xp: int = 150
    alchemy_level: int = 1
    alchemy_xp: int = 0
    alchemy_max_xp: int = 150


@dataclass
class Stats:
    hp: int
    max_hp: int
    haste: int = 0
    critical_strike: int = 0
    wisdom: int = 0
    prospecting: int = 0
    initiative: int = 100
    threat: int = 0
    attack_fire: int = 0
    attack_earth: int = 0
    attack_water: int = 0
    attack_air: int = 0
    dmg: int = 0
    dmg_fire: int = 0
    dmg_earth: int = 0
    dmg_water: int = 0
    dmg_air: int = 0
    res_fire: int = 0
    res_earth: int = 0
    res_water: int = 0
    res_air: int = 0


@dataclass
class Equipment:
    weapon_slot: str = ""
    rune_slot: str = ""
    shield_slot: str = ""
    helmet_slot: str = ""
    body_armor_slot: str = ""
    leg_armor_slot: str = ""
    boots_slot: str = ""
    ring1_slot: str = ""
    ring2_slot: str = ""
    amulet_slot: str = ""
    artifact1_slot: str = ""
    artifact2_slot: str = ""
    artifact3_slot: str = ""
    utility1_slot: str = ""
    utility1_slot_quantity: int = 0
    utility2_slot: str = ""
    utility2_slot_quantity: int = 0
    bag_slot: str = ""


class Cooldown:

    def __init__(self, duration_seconds: float = 0.0):
        self.duration = float(duration_seconds)
        self.end_time = time.time() + self.duration

    def start(self, duration: float = None):
        if duration is not None:
            self.duration = float(duration)
        self.end_time = time.time() + self.duration

    @property
    def remaining(self) -> float:
        return max(0.0, self.end_time - time.time())

    @property
    def is_ready(self) -> bool:
        return time.time() >= self.end_time


def _remaining_from_expiration(expiration_raw: Any) -> float:
    """Computes actual remaining cooldown seconds from the API's absolute
    `cooldown_expiration` timestamp."""
    expiration = parse_reset(expiration_raw)
    if not expiration:
        return 0.0
    now = datetime.now(expiration.tzinfo or timezone.utc)
    return max(0.0, (expiration - now).total_seconds())


def sync_character_state(func):
    """Decorator that automatically updates character state after an action
    API call. Used throughout Character's action methods below -- every raw
    API call that returns character-shaped data gets wrapped with this so
    the in-memory Character stays in sync without callers remembering to do
    it themselves."""
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        data = await func(self, *args, **kwargs)
        if isinstance(data, dict):
            self.update_from_dict(data)
        return data
    return wrapper


class Character:
    """Character model: parses raw API dictionary responses directly in
    __init__, and also owns the full set of actions it can perform (move,
    gather, craft, bank, equip, trade, tasks, ...) as plain methods -- e.g.
    `xylan.rest()`, `xylan.gather()`."""

    def __init__(self, raw_data: Union[Dict[str, Any], List[Dict[str, Any]]], api=None, map_db: Optional[MapStore] = None):
        # Unpack if a single-element list like [{'name': 'Xylan', ...}] is passed
        data = raw_data[0] if isinstance(raw_data, list) else raw_data

        # Core Metadata
        self.name: str = data["name"]
        self.account: str = data["account"]
        self.skin: str = data["skin"]
        self.level: int = data["level"]
        self.xp: int = data["xp"]
        self.max_xp: int = data["max_xp"]
        self.gold: int = data["gold"]
        self.speed: int = data["speed"]

        # API client + map DB used by every action method below.
        self.api = api
        self.map_db = map_db

        # Serializes every API-calling action against THIS character,
        # regardless of which coroutine issues it. Without this, two
        # coroutines can end up acting on the same character concurrently --
        # e.g. this character's own character_loop mid-action while a
        # DIFFERENT character's coroutine calls _try_deliver_equipment() on
        # this one after finishing a craft order that was requested for it.
        # Both would independently call character.wait_cooldown(), see the
        # cooldown as satisfied, and fire overlapping requests -- producing
        # CharacterInCooldownError races and, in the worst case, malformed
        # in-flight state. ArtifactsAPI.request() acquires this before doing
        # anything else whenever a character is passed in, so at most one
        # action per character is ever in flight.
        self.action_lock = asyncio.Lock()

        # Guards a whole multi-step "navigate then act" sequence (e.g.
        # move-to-bank -> withdraw -> equip, or move-to-resource -> gather)
        # against a DIFFERENT concurrent sequence touching this same
        # character -- e.g. Executor delivering an equipped item to this
        # character while this character's own character_loop is mid-gather.
        # action_lock alone doesn't prevent this: it only serializes
        # individual HTTP calls, so two unrelated
        # multi-call sequences can still interleave their moves/actions
        # between each other's individual lock acquisitions, leaving the
        # character standing somewhere neither sequence expects (surfacing
        # as spurious 598 "not found on this map" / 490 "already at
        # destination" errors). Deliberately a SEPARATE lock from
        # action_lock rather than reusing it here, since asyncio.Lock isn't
        # reentrant -- wrapping one of these sequences in `async with
        # action_lock` would deadlock the very first inner api.request()
        # call, which re-acquires action_lock itself.
        self.busy_lock = asyncio.Lock()

        # Event-driven idle signal: Scheduler.character_loop awaits this while idle.
        # Scheduler subscribes to OrderCreated/OrderUpdated/OrderCompleted/
        # OrderReleased on engine.bus and calls `.set()` on whichever
        # characters' events are relevant (or, for OrderCompleted, on
        # everyone -- see Scheduler._wake_eligible/_wake_all) whenever
        # something appears that this character could plausibly now act on.
        # character_loop clears it right after waking so the next wait()
        # blocks again until the next real change.
        self.work_available = asyncio.Event()

        # Complex / Grouped Sub-Models
        self.skills = Skills(
            mining_level=data["mining_level"],
            mining_xp=data["mining_xp"],
            mining_max_xp=data["mining_max_xp"],
            woodcutting_level=data["woodcutting_level"],
            woodcutting_xp=data["woodcutting_xp"],
            woodcutting_max_xp=data["woodcutting_max_xp"],
            fishing_level=data["fishing_level"],
            fishing_xp=data["fishing_xp"],
            fishing_max_xp=data["fishing_max_xp"],
            weaponcrafting_level=data["weaponcrafting_level"],
            weaponcrafting_xp=data["weaponcrafting_xp"],
            weaponcrafting_max_xp=data["weaponcrafting_max_xp"],
            gearcrafting_level=data["gearcrafting_level"],
            gearcrafting_xp=data["gearcrafting_xp"],
            gearcrafting_max_xp=data["gearcrafting_max_xp"],
            jewelrycrafting_level=data["jewelrycrafting_level"],
            jewelrycrafting_xp=data["jewelrycrafting_xp"],
            jewelrycrafting_max_xp=data["jewelrycrafting_max_xp"],
            cooking_level=data["cooking_level"],
            cooking_xp=data["cooking_xp"],
            cooking_max_xp=data["cooking_max_xp"],
            alchemy_level=data["alchemy_level"],
            alchemy_xp=data["alchemy_xp"],
            alchemy_max_xp=data["alchemy_max_xp"],
        )

        self.stats = Stats(
            hp=data["hp"],
            max_hp=data["max_hp"],
            haste=data.get("haste", 0),
            critical_strike=data.get("critical_strike", 0),
            wisdom=data.get("wisdom", 0),
            prospecting=data.get("prospecting", 0),
            initiative=data.get("initiative", 100),
            threat=data.get("threat", 0),
            attack_fire=data.get("attack_fire", 0),
            attack_earth=data.get("attack_earth", 0),
            attack_water=data.get("attack_water", 0),
            attack_air=data.get("attack_air", 0),
            dmg=data.get("dmg", 0),
            dmg_fire=data.get("dmg_fire", 0),
            dmg_earth=data.get("dmg_earth", 0),
            dmg_water=data.get("dmg_water", 0),
            dmg_air=data.get("dmg_air", 0),
            res_fire=data.get("res_fire", 0),
            res_earth=data.get("res_earth", 0),
            res_water=data.get("res_water", 0),
            res_air=data.get("res_air", 0),
        )

        self.equipment = Equipment(
            weapon_slot=data.get("weapon_slot", ""),
            rune_slot=data.get("rune_slot", ""),
            shield_slot=data.get("shield_slot", ""),
            helmet_slot=data.get("helmet_slot", ""),
            body_armor_slot=data.get("body_armor_slot", ""),
            leg_armor_slot=data.get("leg_armor_slot", ""),
            boots_slot=data.get("boots_slot", ""),
            ring1_slot=data.get("ring1_slot", ""),
            ring2_slot=data.get("ring2_slot", ""),
            amulet_slot=data.get("amulet_slot", ""),
            artifact1_slot=data.get("artifact1_slot", ""),
            artifact2_slot=data.get("artifact2_slot", ""),
            artifact3_slot=data.get("artifact3_slot", ""),
            utility1_slot=data.get("utility1_slot", ""),
            utility1_slot_quantity=data.get("utility1_slot_quantity", 0),
            utility2_slot=data.get("utility2_slot", ""),
            utility2_slot_quantity=data.get("utility2_slot_quantity", 0),
            bag_slot=data.get("bag_slot", ""),
        )

        self.location = Location(
            position=Position(x=data["x"], y=data["y"]),
            layer=data["layer"],
            map_id=data["map_id"],
        )

        # Tasks & Cooldowns
        self._cooldown = Cooldown(_remaining_from_expiration(data.get("cooldown_expiration")))

        self.task: str = data.get("task", "")
        self.task_type: str = data.get("task_type", "")
        self.task_progress: int = data.get("task_progress", 0)
        self.task_total: int = data.get("task_total", 0)

        # Inventory & Active Effects
        self.inventory_max_items: int = data.get("inventory_max_items", 100)
        self.inventory: List[InventoryItem] = [
            InventoryItem(
                slot=item["slot"],
                code=item["code"],
                quantity=item["quantity"]
            )
            for item in data.get("inventory", [])
            if item and item.get("code")
        ]
        self.effects: List[str] = data.get("effects", [])

    # ------------------------------------------------------------------
    # State / properties
    # ------------------------------------------------------------------

    @property
    def cooldown(self) -> int:
        return math.ceil(self._cooldown.remaining)

    @cooldown.setter
    def cooldown(self, seconds: float):
        self._cooldown.start(seconds)

    @property
    def is_ready(self) -> bool:
        return self._cooldown.is_ready

    @property
    def inventory_used(self) -> int:
        """Returns the total count of items currently carried across all inventory slots."""
        return sum(item.quantity for item in self.inventory)

    @property
    def is_inventory_full_by_slots(self) -> bool:
        """Returns True if all available inventory slots contain items."""
        return len(self.inventory) >= self.inventory_max_items

    @property
    def is_inventory_full(self) -> bool:
        """Returns True if total item quantity is maxed OR if all inventory slots are occupied."""
        return self.inventory_used >= self.inventory_max_items or self.is_inventory_full_by_slots

    @property
    def is_inventory_empty(self) -> bool:
        """Returns True if the inventory contains no items."""
        return len(self.inventory) == 0 or self.inventory_used == 0

    @property
    def current_task(self) -> Task:
        """Read-only Task view built from the flat task/task_type/task_progress/
        task_total attributes (kept flat so update_from_dict's generic per-key
        sync keeps working against the API's flat character schema)."""
        return Task(
            code=self.task,
            type=self.task_type,
            progress=self.task_progress,
            total=self.task_total,
        )

    async def wait_cooldown(self):
        if not self._cooldown.is_ready:
            await asyncio.sleep(self._cooldown.remaining)

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        """Dynamically updates any attribute on Character or nested objects if it exists."""
        if not isinstance(data, dict):
            return

        # 1. Target the main character payload
        char_info = data.get("character", data)

        # 2. Automatically update top-level & nested attributes
        for key, value in char_info.items():
            if hasattr(self, key):
                if not key == "cooldown":
                    # Directly on Character (e.g. self.level, self.xp, self.gold)
                    setattr(self, key, value)
                else:
                    self.cooldown = value.get("remaining_seconds", 0) if isinstance(value, dict) else value
            else:
                # Check inside nested objects (self.stats, self.skills, self.equipment)
                for nested_obj in [self.stats, self.skills, self.equipment]:
                    if hasattr(nested_obj, key):
                        setattr(nested_obj, key, value)
                        break

        # 3. Handle special structural fields
        if "x" in char_info and "y" in char_info:
            self.location.position.x = char_info["x"]
            self.location.position.y = char_info["y"]

        if "inventory" in char_info:
            self.inventory = [
                InventoryItem(
                    slot=item["slot"],
                    code=item["code"],
                    quantity=item["quantity"]
                )
                for item in char_info["inventory"]
                if item and item.get("code")
            ]

    def is_at(self, target: Union["Position", "Location", tuple[int, int], int], y: Optional[int] = None) -> bool:
        """Checks if the character is currently at a given location or position.

    Accepts:
        - Position: character.is_at(my_position)
        - Location: character.is_at(my_location)
        - Tuple: character.is_at((0, 1))
        - Ints: character.is_at(0, 1)
    """
        if hasattr(target, "position"):
            # Handles Location objects (extracts inner Position)
            target_x, target_y = target.position.x, target.position.y
        elif hasattr(target, "x") and hasattr(target, "y"):
            # Handles Position objects directly
            target_x, target_y = target.x, target.y
        elif isinstance(target, tuple):
            # Handles (x, y) tuples
            target_x, target_y = target[0], target[1]
        elif isinstance(target, int) and isinstance(y, int):
            # Handles raw x, y integers
            target_x, target_y = target, y
        else:
            raise ValueError("Invalid target format. Expected Location, Position, (x, y) tuple, or x, y integers.")

        return self.location.position.x == target_x and self.location.position.y == target_y

    def __getstate__(self):
        """Prevents Spyder/pickle from crashing on the non-picklable
        asyncio.Lock/asyncio.Event objects and the api client. Fresh
        lock/event objects are recreated on unpickle -- identity doesn't
        need to survive a pickle round-trip, only serializing behavior
        within a running process."""
        state = self.__dict__.copy()
        state["action_lock"] = None
        state["busy_lock"] = None
        state["work_available"] = None
        state["api"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        if self.__dict__.get("action_lock") is None:
            self.action_lock = asyncio.Lock()
        if self.__dict__.get("busy_lock") is None:
            self.busy_lock = asyncio.Lock()
        if self.__dict__.get("work_available") is None:
            self.work_available = asyncio.Event()

    def __repr__(self) -> str:
        return f"<Character name='{self.name}' level={self.level} gold={self.gold}>"

    # ------------------------------------------------------------------
    # Action helpers (target normalization, bank lookup)
    # ------------------------------------------------------------------

    def _normalize_target(
        self,
        target: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object]
    ) -> Tuple[int, int]:
        """Extracts (x, y) coordinates from various position/location types."""
        if hasattr(target, "position"):
            return target.position.x, target.position.y
        if hasattr(target, "x") and hasattr(target, "y"):
            return target.x, target.y
        if isinstance(target, (tuple, list)):
            return target[0], target[1]
        raise ValueError(f"Unrecognized target type: {type(target)}")

    def get_closest_bank(self, map_db: Optional[MapStore] = None) -> Optional[Tuple[int, int, str]]:
        """Uses find_closest to locate the nearest bank relative to the character."""
        active_db = map_db or self.map_db
        if not active_db:
            return None
        return active_db.find_closest(self, "bank")

    def is_at_bank(self, map_db: Optional[MapStore] = None) -> bool:
        """Checks if character's current position (including layer) matches the closest bank position."""
        closest_bank = self.get_closest_bank(map_db)
        if not closest_bank:
            return False

        current_x = self.location.position.x
        current_y = self.location.position.y
        current_layer = self.location.layer
        bank_x, bank_y, bank_layer = closest_bank[0], closest_bank[1], closest_bank[2]

        # find_closest() can return a bank on a DIFFERENT layer than the
        # character (e.g. no bank on the current layer, so it falls back to
        # searching across all layers). Comparing only x/y let a same-coordinate
        # tile on another layer register as "at the bank" -- causing bank
        # actions to fire without ever navigating there (API error 598: Bank
        # not found on this map).
        return current_x == bank_x and current_y == bank_y and current_layer == bank_layer

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------

    @sync_character_state
    async def move_to(
        self,
        target: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object]
    ) -> dict:
        """Handles movement logic, API calling, and local state sync."""
        target_x, target_y = self._normalize_target(target)
        target_pos = Position(target_x, target_y)

        if self.is_at(target_pos):
            print(f"[{self.name}] Already at ({target_x}, {target_y}).")
            return {}

        return await self.api.move(self, target_x, target_y)

    @sync_character_state
    async def transition(self) -> dict:
        """Fires the transition action (doors, boats, caves, cross-layer moves, etc.).

        The character must already be standing on the tile that defines
        interactions.transition; the endpoint takes no body and uses whatever
        transition is attached to the character's current tile."""
        return await self.api.transition(self)

    # ------------------------------------------------------------------
    # Combat / rest
    # ------------------------------------------------------------------

    async def fight(
        self,
        target: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Executes a fight action for this character.

        If `target` is given (e.g. "chicken"), first resolves the closest
        matching tile via MapStore.find_closest and moves there via
        smart_move before firing the fight action. Without `target`
        fights on whatever tile the character currently occupies.
        """
        active_db = await self._navigate_to_content(target, map_db)
        if target and active_db is None:
            return {}
        return await self._execute_fight()

    @sync_character_state
    async def _execute_fight(self) -> dict:
        """Internal helper that fires the raw fight API call (wrapped with state sync)."""
        return await self.api.fight(self)

    @sync_character_state
    async def rest(self) -> dict:
        """Executes a rest action for this character."""
        return await self.api.rest(self)

    # ------------------------------------------------------------------
    # Bank: items (deposit/withdraw), gold, expansion
    # ------------------------------------------------------------------

    @sync_character_state
    async def _execute_deposit(self, items: list[dict]) -> dict:
        """Internal helper to fire the raw deposit API call (wrapped with state sync)."""
        return await self.api.bank_deposit_item(self, items)

    async def deposit_items(
        self,
        items: list[dict],
        map_db: Optional[MapStore] = None,
        return_to_origin: bool = True
    ) -> dict:
        """Deposits items. If not at closest bank, auto-relocates to nearest bank via find_closest
        and (unless return_to_origin=False) returns after."""
        if not items:
            return {}

        active_db = map_db or self.map_db

        # Case 1: Already at nearest bank — deposit immediately
        if self.is_at_bank(active_db):
            return await self._execute_deposit(items)

        # Case 2: Away from bank — move to closest bank, deposit, then optionally return to starting position
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.name}] Unable to resolve bank position using find_closest!")
            return {}

        print(f"[{self.name}] Not at bank. Moving to nearest bank {closest_bank} to deposit...")
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db, return_to_origin=return_to_origin):
            return await self._execute_deposit(items)

    async def deposit_all(self, map_db: Optional[MapStore] = None, return_to_origin: bool = True) -> dict:
        """Deposits all items currently in inventory (auto-relocates via find_closest;
        returns to the starting tile afterward unless return_to_origin=False)."""
        items_to_deposit = [
            {"code": item.code, "quantity": item.quantity}
            for item in self.inventory
            if item and item.code and item.quantity > 0
        ]

        if not items_to_deposit:
            print(f"[{self.name}] Inventory is already empty!")
            return {}

        print(f"[{self.name}] Depositing {len(items_to_deposit)} item types into the bank...")
        return await self.deposit_items(items_to_deposit, map_db=map_db, return_to_origin=return_to_origin)

    @sync_character_state
    async def _execute_deposit_gold(self, quantity: int) -> dict:
        return await self.api.bank_deposit_gold(self, quantity)

    async def deposit_gold(self, quantity: int, map_db: Optional[MapStore] = None, return_to_origin: bool = True) -> dict:
        """Deposits gold. Auto-relocates to nearest bank and (unless return_to_origin=False)
        returns after, same as deposit_items."""
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_deposit_gold(quantity)
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db, return_to_origin=return_to_origin):
            return await self._execute_deposit_gold(quantity)

    @sync_character_state
    async def _execute_withdraw_items(self, items: list[dict]) -> dict:
        return await self.api.bank_withdraw_item(self, items)

    async def withdraw_items(
        self,
        items: list[dict],
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Withdraws items from the bank. items: [{'code':..., 'quantity':...}, ...].
        Auto-relocates to nearest bank and returns after, same as deposit_items."""
        if not items:
            return {}
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_withdraw_items(items)
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_withdraw_items(items)

    @sync_character_state
    async def _execute_withdraw_gold(self, quantity: int) -> dict:
        return await self.api.bank_withdraw_gold(self, quantity)

    async def withdraw_gold(self, quantity: int, map_db: Optional[MapStore] = None) -> dict:
        """Withdraws gold. Auto-relocates to nearest bank and returns after."""
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_withdraw_gold(quantity)
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_withdraw_gold(quantity)

    @sync_character_state
    async def _execute_buy_bank_expansion(self) -> dict:
        return await self.api.bank_buy_expansion(self)

    async def buy_bank_expansion(self, map_db: Optional[MapStore] = None) -> dict:
        """Buys a 20-slot bank expansion. Auto-relocates to nearest bank and returns after."""
        active_db = map_db or self.map_db
        if self.is_at_bank(active_db):
            return await self._execute_buy_bank_expansion()
        closest_bank = self.get_closest_bank(active_db)
        if not closest_bank:
            print(f"[{self.name}] Unable to resolve bank position using find_closest!")
            return {}
        async with self.temporary_relocate(destination=closest_bank, map_db=active_db):
            return await self._execute_buy_bank_expansion()

    # ------------------------------------------------------------------
    # Navigation helpers (used by fight/gather/craft/npc/GE/tasks)
    # ------------------------------------------------------------------

    async def smart_move(
        self,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        map_db: Optional[MapStore] = None
    ) -> dict:
        """Navigates character to destination using shortest path in MapStore."""
        active_db = map_db or self.map_db
        if not active_db:
            raise ValueError("No MapStore provided to smart_move!")

        target_x, target_y = self._normalize_target(destination)

        # Already there -- skip pathfinding entirely. get_shortest_path()
        # returns an empty path both when there's genuinely no route AND
        # when start == goal (A* terminates on the first pop with nothing in
        # came_from), so without this check "already at the destination"
        # was being logged and treated identically to "no route exists".
        current_x = self.location.position.x
        current_y = self.location.position.y
        if current_x == target_x and current_y == target_y:
            return {}

        path = active_db.get_shortest_path(self.location, destination)

        if not path:
            print(f"[{self.name}] No valid route found to ({target_x}, {target_y})!")
            return {}

        transitions = active_db.get_transitions()
        prev_node = (
            self.location.position.x,
            self.location.position.y,
            self.location.layer,
        )

        orders: List[Tuple[str, Tuple[int, int, str]]] = []
        for i, node in enumerate(path):
            is_last = i == len(path) - 1
            if transitions.get(prev_node) == node:
                orders.append(("move", prev_node))
                orders.append(("transition", prev_node))
            elif is_last:
                orders.append(("move", node))
            prev_node = node

        print(f"[{self.name}] Route planned ({len(orders)} orders) to target ({target_x}, {target_y}).")

        last_result: dict = {}
        for kind, node in orders:
            if kind == "move":
                last_result = await self.move_to(node)
            else:
                conditions = active_db.get_tile_conditions(node)["transition"]
                if not active_db.check_conditions(self, conditions):
                    print(
                        f"[{self.name}] Does not meet transition conditions at "
                        f"{node}: {conditions}. Aborting route."
                    )
                    return last_result
                print(f"[{self.name}] Using transition at {node}...")
                last_result = await self.transition()

        return last_result

    @asynccontextmanager
    async def temporary_relocate(
        self,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        map_db: Optional[MapStore] = None,
        return_to_origin: bool = True
    ) -> AsyncGenerator[None, None]:
        """Context manager: Remembers starting spot, moves to destination, yields,
        then returns back -- unless return_to_origin=False, in which case the
        character is left at the destination (useful when the caller knows
        something else is about to move them again anyway)."""
        active_db = map_db or self.map_db
        starting_pos = (
            self.location.position.x,
            self.location.position.y,
            self.location.layer
        )

        try:
            await self.smart_move(destination=destination, map_db=active_db)
            yield
        finally:
            if return_to_origin:
                print(f"[{self.name}] Task complete. Returning to origin {starting_pos}...")
                await self.smart_move(destination=starting_pos, map_db=active_db)

    async def run_and_return(
        self,
        destination: Union[Position, Location, Tuple[int, int], Tuple[int, int, str], object],
        action_coro: Callable[..., Any],
        *args,
        map_db: Optional[MapStore] = None,
        **kwargs
    ) -> Any:
        """Helper to run a single async function at destination and return back to origin."""
        async with self.temporary_relocate(destination, map_db=map_db):
            return await action_coro(*args, **kwargs)

    async def _navigate_to_content(
        self,
        content_identifier: Optional[str],
        map_db: Optional[MapStore] = None,
    ) -> Optional[MapStore]:
        """Resolves the closest tile matching content_identifier and moves there via smart_move."""
        active_db = map_db or self.map_db

        if not content_identifier:
            return active_db

        if not active_db:
            raise ValueError("No MapStore provided for target resolution!")

        destination = active_db.find_closest(self, content_identifier)
        if not destination:
            print(f"[{self.name}] Could not resolve '{content_identifier}' via find_closest!")
            return None

        await self.smart_move(destination, map_db=active_db)
        return active_db

    # ------------------------------------------------------------------
    # Gathering / Crafting / Recycling
    # ------------------------------------------------------------------

    async def gather(
        self,
        resource: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Harvests the resource node on the character's current tile."""
        active_db = await self._navigate_to_content(resource, map_db)
        if resource and active_db is None:
            return {}
        return await self._execute_gather()

    @sync_character_state
    async def _execute_gather(self) -> dict:
        return await self.api.gathering(self)

    async def craft(
        self,
        code: str,
        quantity: int = 1,
        workshop: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Crafts `code` x `quantity`."""
        active_db = await self._navigate_to_content(workshop, map_db)
        if workshop and active_db is None:
            return {}
        return await self._execute_craft(code, quantity)

    @sync_character_state
    async def _execute_craft(self, code: str, quantity: int = 1) -> dict:
        return await self.api.crafting(self, code, quantity)

    async def recycle(
        self,
        code: str,
        quantity: int = 1,
        enhanced: bool = False,
        workshop: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Recycles equipment/weapons back into resources."""
        active_db = await self._navigate_to_content(workshop, map_db)
        if workshop and active_db is None:
            return {}
        return await self._execute_recycle(code, quantity, enhanced)

    @sync_character_state
    async def _execute_recycle(self, code: str, quantity: int = 1, enhanced: bool = False) -> dict:
        return await self.api.recycling(self, code, quantity, enhanced)

    # ------------------------------------------------------------------
    # Equipment
    # ------------------------------------------------------------------

    @sync_character_state
    async def equip_items(self, items: List[dict]) -> dict:
        """Equips multiple items at once.
        items: [{'code': ..., 'slot': ..., 'quantity': 1}, ...]"""
        return await self.api.equip(self, items)

    async def equip(self, code: str, slot: str, quantity: int = 1) -> dict:
        """Convenience wrapper for equipping a single item.

        Accepts either the API's bare ItemSlot value ('shield', 'ring1', ...)
        or the internal Equipment-attribute spelling used elsewhere in this
        codebase ('shield_slot', 'ring1_slot', ...) and normalizes to the
        former -- the EquipSchema.slot enum the API actually expects has no
        '_slot' suffix, and sending the suffixed form fails with a 422
        Invalid payload."""
        api_slot = slot[:-5] if slot.endswith("_slot") else slot
        payload = {"code": code, "slot": api_slot, "quantity": quantity}
        return await self.equip_items([payload])

    @sync_character_state
    async def unequip_items(self, slots: List[dict]) -> dict:
        """Unequips multiple slots at once."""
        return await self.api.unequip(self, slots)

    async def unequip(self, slot: str, quantity: int = 1) -> dict:
        """Convenience wrapper for unequipping a single slot. Same '_slot'-suffix
        normalization as equip() -- see its docstring."""
        api_slot = slot[:-5] if slot.endswith("_slot") else slot
        payload = {"slot": api_slot, "quantity": quantity}
        return await self.unequip_items([payload])

    @sync_character_state
    async def use_item(self, code: str, quantity: int = 1) -> dict:
        """Uses a consumable item (e.g. food, potions) from inventory."""
        return await self.api.use(self, code, quantity)

    # ------------------------------------------------------------------
    # NPC trading
    # ------------------------------------------------------------------

    async def npc_buy(
        self,
        code: str,
        quantity: int,
        npc: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Buys an item from an NPC on the character's map."""
        active_db = await self._navigate_to_content(npc, map_db)
        if npc and active_db is None:
            return {}
        return await self._execute_npc_buy(code, quantity)

    @sync_character_state
    async def _execute_npc_buy(self, code: str, quantity: int) -> dict:
        return await self.api.npc_buy(self, code, quantity)

    async def npc_sell(
        self,
        code: str,
        quantity: int,
        npc: Optional[str] = None,
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Sells an item to an NPC on the character's map."""
        active_db = await self._navigate_to_content(npc, map_db)
        if npc and active_db is None:
            return {}
        return await self._execute_npc_sell(code, quantity)

    @sync_character_state
    async def _execute_npc_sell(self, code: str, quantity: int) -> dict:
        return await self.api.npc_sell(self, code, quantity)

    # ------------------------------------------------------------------
    # Grand Exchange
    # ------------------------------------------------------------------

    async def _at_grand_exchange(self, map_db: Optional[MapStore] = None) -> Optional[MapStore]:
        return await self._navigate_to_content("grand_exchange", map_db)

    async def ge_buy(self, order_id: str, quantity: int, map_db: Optional[MapStore] = None) -> dict:
        """Buys (partially or fully) from an existing sell order. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_buy(order_id, quantity)

    @sync_character_state
    async def _execute_ge_buy(self, order_id: str, quantity: int) -> dict:
        return await self.api.ge_buy(self, order_id, quantity)

    async def ge_create_sell_order(
        self, code: str, quantity: int, price: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Creates a sell order at the Grand Exchange. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_create_sell_order(code, quantity, price)

    @sync_character_state
    async def _execute_ge_create_sell_order(self, code: str, quantity: int, price: int) -> dict:
        return await self.api.ge_create_sell_order(self, code, quantity, price)

    async def ge_create_buy_order(
        self, code: str, quantity: int, price: int, map_db: Optional[MapStore] = None
    ) -> dict:
        """Creates a buy order at the Grand Exchange. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_create_buy_order(code, quantity, price)

    @sync_character_state
    async def _execute_ge_create_buy_order(self, code: str, quantity: int, price: int) -> dict:
        return await self.api.ge_create_buy_order(self, code, quantity, price)

    async def ge_fill_buy_order(self, order_id: str, quantity: int, map_db: Optional[MapStore] = None) -> dict:
        """Sells items directly into someone else's buy order. Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_fill_buy_order(order_id, quantity)

    @sync_character_state
    async def _execute_ge_fill_buy_order(self, order_id: str, quantity: int) -> dict:
        return await self.api.ge_fill_buy_order(self, order_id, quantity)

    async def ge_cancel_order(self, order_id: str, map_db: Optional[MapStore] = None) -> dict:
        """Cancels one of your own orders (sell or buy). Auto-navigates to the GE."""
        active_db = await self._at_grand_exchange(map_db)
        if active_db is None:
            return {}
        return await self._execute_ge_cancel_order(order_id)

    @sync_character_state
    async def _execute_ge_cancel_order(self, order_id: str) -> dict:
        return await self.api.ge_cancel_order(self, order_id)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def _at_tasks_master(
        self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None
    ) -> Optional[MapStore]:
        return await self._navigate_to_content(tasks_master, map_db)

    async def task_new(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Accepts a new task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_new()

    @sync_character_state
    async def _execute_task_new(self) -> dict:
        return await self.api.task_new(self)

    async def task_complete(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Completes the character's current (finished) task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_complete()

    @sync_character_state
    async def _execute_task_complete(self) -> dict:
        return await self.api.task_complete(self)

    async def task_cancel(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Cancels the current task for 1 task coin. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_cancel()

    @sync_character_state
    async def _execute_task_cancel(self) -> dict:
        return await self.api.task_cancel(self)

    async def task_exchange(self, tasks_master: str = "tasks_master", map_db: Optional[MapStore] = None) -> dict:
        """Exchanges 6 task coins for a random reward. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_exchange()

    @sync_character_state
    async def _execute_task_exchange(self) -> dict:
        return await self.api.task_exchange(self)

    async def task_trade(
        self,
        code: str,
        quantity: int,
        tasks_master: str = "tasks_master",
        map_db: Optional[MapStore] = None,
    ) -> dict:
        """Trades items toward the character's current item-type task. Auto-navigates to the tasks master."""
        active_db = await self._at_tasks_master(tasks_master, map_db)
        if active_db is None:
            return {}
        return await self._execute_task_trade(code, quantity)

    @sync_character_state
    async def _execute_task_trade(self, code: str, quantity: int) -> dict:
        return await self.api.task_trade(self, code, quantity)

    # ------------------------------------------------------------------
    # Give (character-to-character, same tile), pending items, misc
    # ------------------------------------------------------------------

    @sync_character_state
    async def give_gold(self, quantity: int, to_character: str) -> dict:
        """Gives gold to another of your characters standing on the same tile."""
        return await self.api.give_gold(self, quantity, to_character)

    @sync_character_state
    async def give_items(self, items: list[dict], to_character: str) -> dict:
        """Gives items to another of your characters standing on the same tile."""
        return await self.api.give_items(self, items, to_character)

    @sync_character_state
    async def claim_pending_item(self, pending_item_id: str) -> dict:
        """Claims a pending item (achievement/GE/event reward) into this character's inventory."""
        return await self.api.claim_item(self, pending_item_id)

    @sync_character_state
    async def delete_item(self, code: str, quantity: int) -> dict:
        """Permanently deletes an item from inventory. No confirmation -- use with care."""
        return await self.api.delete_item(self, code, quantity)

    @sync_character_state
    async def change_skin(self, skin: str) -> dict:
        """Changes the character's skin to one the account owns."""
        return await self.api.change_skin(self, skin)

    @sync_character_state
    async def rename(self, new_name: str) -> dict:
        """Renames the character (members only)."""
        return await self.api.rename_character(self, new_name)
