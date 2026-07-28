#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
models.py: Lightweight, dependency-free value types shared across character.py,
account.py, and database/*.py. Kept separate from character.py so that modules
like MapStore (which only need Position/Location) don't have to import the
entire Character model, and so account.py's Bank/PendingItem don't have to
import character.py just for InventoryItem.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def parse_reset(value: Any) -> Optional[datetime]:
    """Parses a timestamp that may arrive as a unix epoch number or an ISO
    datetime string (the Artifacts API has used both across versions/fields).
    Shared by account.py (rate limits, membership expiration) and Event below."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@dataclass
class Position:
    x: int
    y: int


@dataclass
class Location:
    position: Position
    layer: str
    map_id: int


@dataclass
class InventoryItem:
    slot: int
    code: str
    quantity: int


@dataclass
class Task:
    """Read-only convenience view over a character's current task fields
    (character.task / task_type / task_progress / task_total stay as flat
    attributes on Character to match the API's flat schema and update_from_dict's
    generic sync -- this is just a nicer way to look at them together, via
    Character.current_task)."""
    code: str = ""
    type: str = ""
    progress: int = 0
    total: int = 0

    @property
    def is_complete(self) -> bool:
        return bool(self.code) and self.progress >= self.total > 0

    @property
    def is_active(self) -> bool:
        return bool(self.code)


@dataclass
class Resource:
    """Mirrors a row from ResourceStore -- a gatherable node (ore/tree/fishing
    spot), as opposed to a monster. See ResourceStore.get_resource_obj()."""
    code: str
    name: str
    skill: str = ""
    level: int = 1
    drops: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Resource":
        return cls(
            code=data["code"],
            name=data.get("name", ""),
            skill=data.get("skill", ""),
            level=data.get("level", 1),
            drops=data.get("drops", []) or [],
        )


@dataclass
class RecipeIngredient:
    code: str
    quantity: int


@dataclass
class CraftRecipe:
    """Mirrors CraftSchema (Item.craft in the API's ItemSchema) -- the skill,
    level, and ingredient list required to craft an item."""
    skill: str = ""
    level: int = 1
    items: List[RecipeIngredient] = field(default_factory=list)
    quantity: int = 1  # amount produced per craft action

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["CraftRecipe"]:
        if not data:
            return None
        return cls(
            skill=data.get("skill", ""),
            level=data.get("level", 1),
            items=[
                RecipeIngredient(code=i["code"], quantity=i["quantity"])
                for i in data.get("items", []) or []
            ],
            quantity=data.get("quantity", 1),
        )


@dataclass
class ItemCondition:
    code: str
    operator: str
    value: int


@dataclass
class ItemEffect:
    code: str
    value: int
    description: str = ""


# ItemType values (per the API's ItemType enum) that occupy an equipment slot.
EQUIPABLE_TYPES = {
    "weapon", "shield", "helmet", "body_armor", "leg_armor", "boots",
    "ring", "amulet", "artifact", "rune", "utility", "bag",
}


@dataclass
class Item:
    """Mirrors a row from ItemStore -- a typed view over the raw ItemSchema
    JSON, including its craft recipe (if craftable), conditions, and combat
    effects. See ItemStore.get_item_obj()/get_all_items_obj()."""
    code: str
    name: str
    level: int = 1
    type: str = ""
    subtype: str = ""
    description: str = ""
    tradeable: bool = True
    recyclable: bool = False
    conditions: List[ItemCondition] = field(default_factory=list)
    effects: List[ItemEffect] = field(default_factory=list)
    craft: Optional[CraftRecipe] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Item":
        return cls(
            code=data["code"],
            name=data.get("name", ""),
            level=data.get("level", 1),
            type=data.get("type", ""),
            subtype=data.get("subtype", ""),
            description=data.get("description", ""),
            tradeable=data.get("tradeable", True),
            recyclable=data.get("recyclable", False),
            conditions=[
                ItemCondition(code=c["code"], operator=c["operator"], value=c["value"])
                for c in (data.get("conditions") or [])
            ],
            effects=[
                ItemEffect(code=e["code"], value=e["value"], description=e.get("description", ""))
                for e in (data.get("effects") or [])
            ],
            craft=CraftRecipe.from_dict(data.get("craft")),
        )

    @property
    def is_craftable(self) -> bool:
        return self.craft is not None

    @property
    def is_equipable(self) -> bool:
        return self.type in EQUIPABLE_TYPES


class TaskType(str, Enum):
    GATHER = "gather"
    CRAFT = "craft"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


@dataclass
class PlanTask:
    """A single gather-or-craft step in a GearPlan (see planning.py). Pure
    data -- no live Character reference, just a name in `assigned_to` --
    so a plan can be built, persisted (TaskStore), and handed to any
    character roster at execution time.

    `target_quantity` is the total amount of `code` that needs to exist
    across all characters' inventories + the bank; PlanRunner loops the
    matching action until that target is met rather than running a fixed
    number of actions, since gather yields are random.
    """
    id: int
    type: TaskType
    code: str                    # item code this task accumulates (armor, ore, whatever)
    target_quantity: int         # total amount of `code` needed across characters+bank
    node_code: str = ""          # GATHER only: resource node to visit (may differ from `code`)
    skill: str = ""
    skill_level: int = 1
    produces_per_action: int = 1
    depends_on: List[int] = field(default_factory=list)  # other task ids
    assigned_to: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING

    @property
    def is_assigned(self) -> bool:
        return self.assigned_to is not None

    @property
    def is_done(self) -> bool:
        return self.status == TaskStatus.DONE


@dataclass
class Event:
    """Mirrors ActiveEventSchema from GET /events/active -- a currently-live
    world event (bonus resource nodes, invasions, roaming NPC merchants, etc).
    See Account.sync_active_events()."""
    name: str
    code: str
    map_x: int
    map_y: int
    map_content_type: Optional[str] = None
    map_content_code: Optional[str] = None
    duration_minutes: int = 0
    expiration: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        map_info = data.get("map") or {}
        content = map_info.get("content") or {}
        return cls(
            name=data.get("name", ""),
            code=data.get("code", ""),
            map_x=map_info.get("x", 0),
            map_y=map_info.get("y", 0),
            map_content_type=content.get("type"),
            map_content_code=content.get("code"),
            duration_minutes=data.get("duration", 0),
            expiration=parse_reset(data.get("expiration")),
            created_at=parse_reset(data.get("created_at")),
        )
