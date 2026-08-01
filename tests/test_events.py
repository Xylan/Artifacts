#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_events.py: Minimal manual/async smoke test for the event-driven
conversion (TODO task 13). There's no existing test suite/framework in this
project, so this is a plain asyncio script, not pytest -- run it directly:

    python3 tests/test_events.py

It does NOT spin up a real TaskEngine (that needs a live Account/GameDatabase/
API token). Instead it uses a minimal FakeEngine that exposes only the shared
state Scheduler/Executor/OrderManager/ConfigWatcher actually read or write
(engine.bus/orders/stock_rules/account/held()/_order_for_code(), etc. -- see
each module's own docstring) and constructs the REAL collaborator classes
against it, so the actual reactive wiring under test is the real code, not a
mock of it. Heavier mechanics that need a live bank/API (the actual
withdraw-unequip-equip sequence in Executor._deliver_one, the actual craft/
gather DB lookups in OrderManager.request_item) are stubbed out per-test so
each test isolates just the event-wiring behavior task 13 asks for:

  1. Characters wake promptly instead of waiting a full poll_interval
     (test_scheduler_wakeups).
  2. Delivery/auto-convert fire only when the relevant events occur, scoped
     to just the order/code implicated (test_executor_reactive_delivery,
     test_order_manager_reactive).
  3. Config changes propagate without the old timer -- off a real mtime
     diff on a real temp file (test_config_watcher_reactive).
"""
import asyncio
from types import SimpleNamespace

from events import (
    EventBus, OrderCreated, OrderCompleted, OrderReleased,
    EquipmentRequested, BankSynced, StockBelowMinimum,
)
from orders import WorkOrder, OrderKind, Priority
from character import Character
from scheduler import Scheduler
from executor import Executor
from order_manager import OrderManager, StockRule
from config_watcher import ConfigWatcher


def make_character(name, mining_level=5):
    """Builds a real character.Character from a minimal raw API-shaped dict
    (api=None, map_db=None -- neither is touched by anything these tests
    exercise)."""
    data = {
        "name": name, "account": "acct", "skin": "men1", "level": 1, "xp": 0, "max_xp": 100,
        "gold": 0, "speed": 100,
        "mining_level": mining_level, "mining_xp": 0, "mining_max_xp": 150,
        "woodcutting_level": 1, "woodcutting_xp": 0, "woodcutting_max_xp": 150,
        "fishing_level": 1, "fishing_xp": 0, "fishing_max_xp": 150,
        "weaponcrafting_level": 1, "weaponcrafting_xp": 0, "weaponcrafting_max_xp": 150,
        "gearcrafting_level": 1, "gearcrafting_xp": 0, "gearcrafting_max_xp": 150,
        "jewelrycrafting_level": 1, "jewelrycrafting_xp": 0, "jewelrycrafting_max_xp": 150,
        "cooking_level": 1, "cooking_xp": 0, "cooking_max_xp": 150,
        "alchemy_level": 1, "alchemy_xp": 0, "alchemy_max_xp": 150,
        "hp": 100, "max_hp": 100,
        "x": 0, "y": 0, "layer": "overworld", "map_id": 1,
        "inventory_max_items": 100, "inventory": [],
    }
    return Character(data, api=None, map_db=None)


class FakeEngine:
    """Minimal duck-typed stand-in for task_runner.TaskEngine -- carries
    only the shared state Scheduler/Executor/OrderManager/ConfigWatcher
    actually read/write, without any real Account/GameDatabase/API wiring.
    Collaborators (engine.scheduler/executor/order_manager/config_watcher)
    are attached by each test individually, mirroring which ones it needs."""

    def __init__(self):
        self.bus = EventBus()
        self.orders = {}
        self.stock_rules = []
        self.default_orders = {}
        self._current_order = {}
        self.roles = {}
        self.running = True
        self.poll_interval = 0.05
        self._single_use_conversions = {}
        self.account = SimpleNamespace(characters={}, bank=SimpleNamespace(items=[]))
        self.db = None  # only touched by code paths these smoke tests avoid

    def held(self, code):
        total = sum(i.quantity for c in self.account.characters.values() for i in c.inventory if i.code == code)
        total += sum(i.quantity for i in self.account.bank.items if i.code == code)
        return total

    def _order_for_code(self, code):
        for order in self.orders.values():
            if not order.done and order.code == code:
                return order
        return None

    def complete(self, order):
        self.scheduler.complete(order)


async def gather_emitted(bus, event):
    """EventBus.emit() is fire-and-forget (see events.py) -- await the
    returned tasks so each test can assert on a handler's side effects
    immediately after emitting, instead of racing it."""
    await asyncio.gather(*bus.emit(event))


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, f"FAILED: {label}"


# ----------------------------------------------------------------------
# 1. Scheduler: characters wake promptly instead of waiting poll_interval
# ----------------------------------------------------------------------
async def test_scheduler_wakeups():
    engine = FakeEngine()
    xylan = make_character("Xylan", mining_level=5)
    other = make_character("Other", mining_level=0)
    engine.account.characters = {"Xylan": xylan, "Other": other}
    engine.scheduler = Scheduler(engine)

    order = WorkOrder(kind=OrderKind.GATHER, priority=Priority.GATHER, code="copper_ore",
                       node_code="copper_rocks", skill="mining", skill_level=1, target_quantity=10)
    engine.orders[order.id] = order

    check("both characters start with work_available unset",
          not xylan.work_available.is_set() and not other.work_available.is_set())

    await gather_emitted(engine.bus, OrderCreated(order_id=order.id, code=order.code,
                                                    kind=order.kind, priority=order.priority,
                                                    target_quantity=order.target_quantity))

    check("eligible character (mining_level 5 >= 1) woke on OrderCreated", xylan.work_available.is_set())
    check("ineligible character (mining_level 0 < 1) did NOT wake", not other.work_available.is_set())

    # Promptness: character_loop's idle wait uses IDLE_WAIT_FALLBACK_MULTIPLIER
    # (10) * poll_interval as its fallback timeout. Wire up a fresh idle wait
    # exactly like character_loop's, fire OrderReleased partway through, and
    # assert it resolves in well under the fallback window instead of timing out.
    xylan.work_available.clear()
    fallback_timeout = engine.poll_interval * Scheduler.IDLE_WAIT_FALLBACK_MULTIPLIER

    async def idle_wait():
        start = asyncio.get_event_loop().time()
        try:
            await asyncio.wait_for(xylan.work_available.wait(), timeout=fallback_timeout)
        except asyncio.TimeoutError:
            pass
        return asyncio.get_event_loop().time() - start

    waiter = asyncio.ensure_future(idle_wait())
    await asyncio.sleep(0.01)  # let the waiter actually start blocking
    await gather_emitted(engine.bus, OrderReleased(order_id=order.id, character_name="Xylan"))
    elapsed = await waiter

    check(f"idle wait resolved promptly on OrderReleased ({elapsed:.3f}s << fallback {fallback_timeout:.3f}s)",
          elapsed < fallback_timeout * 0.5)

    engine.scheduler.close()


# ----------------------------------------------------------------------
# 2. Executor: delivery fires only when relevant events occur
# ----------------------------------------------------------------------
async def test_executor_reactive_delivery():
    engine = FakeEngine()
    engine.scheduler = SimpleNamespace()  # not exercised by Executor's reactive handlers
    engine.executor = Executor(engine)

    order_with_requests = WorkOrder(kind=OrderKind.CRAFT, code="copper_boots",
                                     equip_requests=[("Xylan", "boots_slot")])
    order_without_requests = WorkOrder(kind=OrderKind.CRAFT, code="copper_ring")
    engine.orders[order_with_requests.id] = order_with_requests
    engine.orders[order_without_requests.id] = order_without_requests

    calls = []

    async def fake_deliver(order, *, already_locked=None):
        calls.append(order.id)

    engine.executor._try_deliver_equipment = fake_deliver

    # EquipmentRequested for the order that actually has pending requests -> delivers.
    await gather_emitted(engine.bus, EquipmentRequested(
        order_id=order_with_requests.id, character_name="Xylan", code="copper_boots", slot="boots_slot"))
    check("EquipmentRequested for an order with pending requests triggers delivery",
          calls == [order_with_requests.id])

    # EquipmentRequested for an unknown order id -> no delivery attempted.
    calls.clear()
    await gather_emitted(engine.bus, EquipmentRequested(
        order_id=999999, character_name="Xylan", code="copper_boots", slot="boots_slot"))
    check("EquipmentRequested for an unknown order id triggers nothing", calls == [])

    # BankSynced -> sweeps every order with pending equip_requests, skips the rest.
    calls.clear()
    await gather_emitted(engine.bus, BankSynced())
    check("BankSynced delivers only orders with pending equip_requests",
          calls == [order_with_requests.id])

    engine.executor.close()


# ----------------------------------------------------------------------
# 3. OrderManager: auto-convert and keep-in-stock fire only reactively
#    when the relevant events occur (not on some timer)
# ----------------------------------------------------------------------
async def test_order_manager_reactive():
    engine = FakeEngine()
    engine.order_manager = OrderManager(engine)

    auto_convert_calls = []
    engine.order_manager._maybe_auto_convert = lambda raw_code: auto_convert_calls.append(raw_code)
    sweep_calls = []
    engine.order_manager.refresh_auto_convert_orders = lambda: sweep_calls.append(True)

    await gather_emitted(engine.bus, OrderCompleted(order_id=1, code="copper_ore"))
    check("OrderCompleted narrows auto-convert to just that code",
          auto_convert_calls == ["copper_ore"] and sweep_calls == [])

    await gather_emitted(engine.bus, BankSynced())
    check("BankSynced triggers the bounded auto-convert sweep (no code to narrow to)",
          sweep_calls == [True])

    # Keep-in-stock: _check_stock_thresholds (real) should emit StockBelowMinimum
    # only for rules currently under their floor, in reaction to BankSynced/OrderCompleted.
    engine.stock_rules = [StockRule(code="copper_ore", minimum=10), StockRule(code="feather", minimum=5)]
    engine.account.bank.items = [SimpleNamespace(code="copper_ore", quantity=2)]  # under 10
    # "feather" held() == 0, also under 5 -- both rules should fire.

    below_events = []
    engine.bus.subscribe(StockBelowMinimum, lambda e: below_events.append(e.code))

    queue_calls = []
    engine.order_manager._maybe_queue_stock_order = lambda code, minimum: queue_calls.append(code)

    await gather_emitted(engine.bus, BankSynced())
    check("BankSynced -> _check_stock_thresholds emits StockBelowMinimum for both under-floor rules",
          set(below_events) == {"copper_ore", "feather"})
    check("StockBelowMinimum -> _on_stock_below_minimum narrows to _maybe_queue_stock_order per code",
          set(queue_calls) == {"copper_ore", "feather"})

    # Once above the floor, no event should fire for that code.
    below_events.clear()
    queue_calls.clear()
    engine.account.bank.items = [SimpleNamespace(code="copper_ore", quantity=50)]
    engine.stock_rules = [StockRule(code="copper_ore", minimum=10)]
    await gather_emitted(engine.bus, OrderCompleted(order_id=2, code="copper_ore"))
    check("A rule currently AT/ABOVE its floor does not emit StockBelowMinimum", below_events == [])

    engine.order_manager.close()


# ----------------------------------------------------------------------
# 4. ConfigWatcher: config changes propagate reactively, off a real mtime
#    diff, not the old unconditional-reparse timer
# ----------------------------------------------------------------------
async def test_config_watcher_reactive():
    import json
    import tempfile
    import os

    engine = FakeEngine()
    refresh_calls = []
    engine.order_manager = SimpleNamespace(refresh_stock_orders=lambda: refresh_calls.append(True))
    engine.config_watcher = ConfigWatcher(engine)
    engine.config_watcher.MTIME_CHECK_INTERVAL = 0.02  # fast for the test only

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "stock_config.json")
        with open(path, "w") as f:
            json.dump({"copper_ore": 10}, f)

        engine.config_watcher.load_stock_rules_from_file(path)
        check("initial load populates stock_rules and does not itself trigger refresh",
              len(engine.stock_rules) == 1 and engine.stock_rules[0].code == "copper_ore"
              and refresh_calls == [])

        loop_task = asyncio.ensure_future(engine.config_watcher.loop())
        await asyncio.sleep(0.05)
        check("loop() does nothing while the file is unchanged", refresh_calls == [])

        # Real file edit -- bumps mtime.
        await asyncio.sleep(0.01)  # ensure a distinct mtime on filesystems with coarse resolution
        with open(path, "w") as f:
            json.dump({"copper_ore": 25, "feather": 5}, f)
        os.utime(path, None)  # nudge mtime just in case

        await asyncio.sleep(0.15)  # a few MTIME_CHECK_INTERVAL ticks
        check("editing the file triggers ConfigChanged -> reload + refresh_stock_orders",
              len(engine.stock_rules) == 2 and refresh_calls == [True])

        engine.running = False
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    engine.config_watcher.close()


async def main():
    await test_scheduler_wakeups()
    await test_executor_reactive_delivery()
    await test_order_manager_reactive()
    await test_config_watcher_reactive()
    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
