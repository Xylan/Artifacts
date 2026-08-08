#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
events.py: Async pub/sub event bus + domain event dataclasses.

`emit()` is non-blocking: it schedules each subscribed handler as its own
`asyncio.Task` and returns immediately, without awaiting them. Handler
exceptions are caught and logged, not propagated.

Usage:
    bus = EventBus()
    bus.subscribe(OrderCreated, handler)
    bus.emit(OrderCreated(order_id=1, code="copper_ore", kind=OrderKind.GATHER,
                           priority=Priority.GATHER))
"""
from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, DefaultDict, List, Optional, Type

from orders import OrderKind, Priority

Handler = Callable[[Any], Any]  # Any -> None or Awaitable[None]


class EventBus:
    """Minimal pub/sub bus: sync-registered subscribers, async-dispatched."""

    def __init__(self) -> None:
        self._subscribers: DefaultDict[Type, List[Handler]] = defaultdict(list)

    def subscribe(self, event_type: Type, handler: Handler) -> Handler:
        """Registers `handler` for `event_type`. Returns `handler` unchanged
        so callers can reuse the reference later with unsubscribe()."""
        self._subscribers[event_type].append(handler)
        return handler

    def unsubscribe(self, event_type: Type, handler: Handler) -> None:
        """Idempotent: no-op if `handler` wasn't subscribed."""
        try:
            self._subscribers[event_type].remove(handler)
        except ValueError:
            pass

    def emit(self, event: Any) -> List["asyncio.Task"]:
        """Schedules every handler subscribed to `type(event)` as its own
        task and returns immediately without awaiting them. Returns the
        created tasks, e.g. for `await asyncio.gather(*bus.emit(event))`
        in tests."""
        handlers = self._subscribers.get(type(event), [])
        return [asyncio.create_task(self._run_handler(handler, event)) for handler in handlers]

    @staticmethod
    async def _run_handler(handler: Handler, event: Any) -> None:
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            print(f"[EventBus] Handler {handler!r} raised for {event!r}: {e!r}")


# ----------------------------------------------------------------------
# Domain events -- flat (ids + the fields a subscriber needs), never a full
# WorkOrder/Character object. Subscribers look up the live object via
# engine.orders[order_id] / engine.account.get_character(...).
# ----------------------------------------------------------------------

@dataclass
class OrderCreated:
    """A new WorkOrder was added to engine.orders."""
    order_id: int
    code: str
    kind: OrderKind
    priority: Priority
    target_quantity: int


@dataclass
class OrderUpdated:
    """An existing WorkOrder's target_quantity or priority changed."""
    order_id: int
    code: str
    target_quantity: int
    priority: Priority


@dataclass
class OrderClaimed:
    order_id: int
    character_name: str


@dataclass
class OrderReleased:
    order_id: int
    character_name: str


@dataclass
class OrderCompleted:
    order_id: int
    code: str


@dataclass
class BankSynced:
    pass


@dataclass
class EquipmentRequested:
    """A character queued an equip_request on an order."""
    order_id: int
    character_name: str
    code: str
    slot: str


@dataclass
class EquipmentDelivered:
    """A queued equip_request was withdrawn from the bank and equipped."""
    order_id: int
    character_name: str
    code: str
    slot: str


@dataclass
class StockBelowMinimum:
    """A tracked item's held() total dropped under its StockRule.minimum."""
    code: str
    current: int
    minimum: int


@dataclass
class ConfigChanged:
    """stock_config.json's mtime changed."""
    path: str
