#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
events.py: Lightweight async pub/sub event bus + domain event dataclasses,
foundation for converting task_runner's four polling loops (see TODO) into
reactive subscribers.

EventBus is intentionally tiny -- pure `asyncio`, no new dependency. It is
NOT a general message queue: `emit()` is synchronous/non-blocking (it just
schedules each subscribed handler as its own `asyncio.Task` via
`asyncio.create_task` and returns immediately). This is a deliberate design
choice ahead of the task 12 concurrency audit: emit() is expected to be
called from inside code that may itself be holding a `character.action_lock`
/ `busy_lock` (e.g. Scheduler.claim/release/complete, Executor's steps), and
awaiting subscriber handlers synchronously there would risk a reentrant
deadlock if a handler calls back into the engine (e.g. a StockBelowMinimum
handler that turns around and claims/releases an order). Fire-and-forget
scheduling avoids that: subscribers run on their own task once the current
call stack yields, never inline inside the emitter's lock.

Each handler (sync or async, both are accepted) is wrapped so an exception
inside one subscriber is caught and logged rather than propagating -- a bad
subscriber must not kill the task that raised the event, mirroring the
existing try/except guards in task_runner.py's loops.

Usage:
    bus = EventBus()
    bus.subscribe(OrderCreated, handler)         # handler(event) -> None or awaitable
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
    """Minimal synchronous-emit / async-dispatch pub/sub bus.

    subscribe(event_type, handler) registers a callable (sync or async) to
    run whenever an instance of `event_type` is emitted. emit(event) fires
    every handler registered for `type(event)` as an independent task -- see
    module docstring for why this is fire-and-forget rather than awaited
    inline."""

    def __init__(self) -> None:
        self._subscribers: DefaultDict[Type, List[Handler]] = defaultdict(list)

    def subscribe(self, event_type: Type, handler: Handler) -> Handler:
        """Registers `handler` for `event_type`. Returns `handler` unchanged
        so callers can do `self._h = bus.subscribe(X, self._h)` and later
        pass the same reference to unsubscribe()."""
        self._subscribers[event_type].append(handler)
        return handler

    def unsubscribe(self, event_type: Type, handler: Handler) -> None:
        """No-op if `handler` was never subscribed (or already removed) --
        useful for idempotent cleanup, e.g. TaskEngine.stop()."""
        try:
            self._subscribers[event_type].remove(handler)
        except ValueError:
            pass

    def emit(self, event: Any) -> List["asyncio.Task"]:
        """Schedules every handler subscribed to `type(event)` as its own
        asyncio.Task and returns immediately (does NOT await them). Returns
        the list of created tasks -- callers that specifically need to wait
        for handlers to finish (e.g. the smoke test in task 13) can
        `await asyncio.gather(*bus.emit(event))`; normal callers should
        ignore the return value."""
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
# Domain events
# ----------------------------------------------------------------------
# Kept intentionally small/flat (ids + the handful of fields a subscriber
# actually needs to decide what to do) rather than embedding whole
# WorkOrder/Character objects -- subscribers that need the full live object
# look it up via engine.orders[order_id] / engine.account.get_character(...),
# so an event can never carry a stale copy of mutable state.

@dataclass
class OrderCreated:
    """A brand-new WorkOrder was added to engine.orders. Emitted by
    OrderManager.request_item / _bump_ingredients / request_equipment /
    refresh_stock_orders / refresh_auto_convert_orders whenever they create
    (rather than bump) an order."""
    order_id: int
    code: str
    kind: OrderKind
    priority: Priority
    target_quantity: int


@dataclass
class OrderUpdated:
    """An existing WorkOrder's target_quantity (or priority, e.g. an EQUIP
    escalation) changed in place. Emitted by the same OrderManager methods
    as OrderCreated whenever they bump an order that already existed."""
    order_id: int
    code: str
    target_quantity: int
    priority: Priority


@dataclass
class OrderClaimed:
    """Emitted by Scheduler.claim."""
    order_id: int
    character_name: str


@dataclass
class OrderReleased:
    """Emitted by Scheduler.release."""
    order_id: int
    character_name: str


@dataclass
class OrderCompleted:
    """Emitted by Scheduler.complete once an order's target_quantity is
    reached and it's marked done."""
    order_id: int
    code: str


@dataclass
class BankSynced:
    """Emitted at the end of Account.sync_bank() (and optionally
    sync_pending_items()) -- the trigger _delivery_loop and
    _auto_convert_loop react to instead of polling on a timer."""
    pass


@dataclass
class EquipmentRequested:
    """Emitted by OrderManager.request_equipment when a character queues an
    equip_request on an order (new or already-existing)."""
    order_id: int
    character_name: str
    code: str
    slot: str


@dataclass
class EquipmentDelivered:
    """Emitted by Executor._try_deliver_equipment once a single queued
    (character_name, slot) equip_request has actually been withdrawn from
    the bank and equipped."""
    order_id: int
    character_name: str
    code: str
    slot: str


@dataclass
class StockBelowMinimum:
    """A tracked item's held() total dropped under its StockRule.minimum.
    Emitted wherever inventory/bank state changes push it under that floor
    (deposits, gathers, crafts completing); OrderManager.refresh_stock_orders
    reacts to this in addition to running at startup/config-reload."""
    code: str
    current: int
    minimum: int


@dataclass
class ConfigChanged:
    """stock_config.json's mtime changed (or an equivalent real filesystem
    notification fired). Emitted by ConfigWatcher in place of its old timer
    poll; triggers load_stock_rules_from_file + refresh_stock_orders."""
    path: str
