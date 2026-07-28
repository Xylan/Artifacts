from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
import time
import math
import asyncio

from models import Position, Location, InventoryItem, Task


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
        print(duration_seconds)

    def start(self, duration: float = None):
        if duration is not None:
            self.duration = float(duration)
        self.end_time = time.time() + self.duration
        print(duration)

    @property
    def remaining(self) -> float:
        return max(0.0, self.end_time - time.time())

    @property
    def is_ready(self) -> bool:
        return time.time() >= self.end_time


class Character:
    """Character model that parses raw API dictionary responses directly in __init__."""

    def __init__(self, raw_data: Union[Dict[str, Any], List[Dict[str, Any]]], api=None, map_db=None):
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

        # CharacterActions instance, created directly (no separate bind() step)
        # so `character.actions.rest()` -- and `character.rest()` via
        # __getattr__ below -- work as soon as the character exists.
        from CharacterActions import CharacterActions
        self.actions: CharacterActions = CharacterActions(self, api, map_db)

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
        self._cooldown = Cooldown(data.get("cooldown", 0))

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

    def __getattr__(self, name: str) -> Any:
        """Only invoked when normal attribute lookup fails (so it never
        shadows real methods/properties like is_at, update_from_dict, etc).
        Falls through to the bound CharacterActions proxy so e.g.
        `character.rest()` works directly, not just `character.actions.rest()`."""
        actions = self.__dict__.get("actions")
        if actions is not None and hasattr(actions, name):
            return getattr(actions, name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __repr__(self) -> str:
        return f"<Character name='{self.name}' level={self.level} gold={self.gold}>"
