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
