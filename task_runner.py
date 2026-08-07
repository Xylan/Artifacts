#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
task_runner.py: Dynamic, priority-interrupt task scheduler for the character
roster. TaskEngine is a thin orchestrator over four collaborator modules;
see ARCHITECTURE.md for why it's split this way and how the event-bus
wiring works.

This is the *live* driver of the roster, complementary to planning.py rather
than a replacement for it: planning.GearPlan/PlanRunner resolve a one-off
wishlist into a static DAG and run it to completion, while TaskEngine owns a
continuously-updated shared pool of WorkOrders (orders.py) -- orders get
created (request_item), reprioritized, locked/released, and completed while
characters are already mid-run -- and every character repeatedly asks
"what's the best thing I could be doing right now?" at well-defined
breakpoints.

Module boundary (see function_map.md for the full index):
  * order_manager.py (TaskEngine.order_manager) -- deciding WHAT work
    should exist: request_item/request_equipment, stock rules, auto-convert,
    default tasks, plan verification.
  * scheduler.py (TaskEngine.scheduler) -- deciding WHO works which order
    right now: character_eligible/_score/select_order_for, the
    claim/release/complete lifecycle, and character_loop.
  * executor.py (TaskEngine.executor) -- deciding HOW a claimed order is
    carried out: switching tasks, delivering/equipping gear, one gather
    step, one craft batch.
  * config_watcher.py (TaskEngine.config_watcher) -- the only module that
    touches the filesystem: reading stock_config.json into shared state and
    reactively pushing changes to OrderManager when the file's mtime moves.

TaskEngine itself holds the shared state these four collaborators operate
on (self.orders, self.stock_rules, self.default_orders,
self._current_order, ...), a handful of trivial state-query helpers used by
all four (held, _order_for_code, complete), a thin public facade that
forwards to the right collaborator (so external callers like main.py don't
need to know about the split), and top-level lifecycle (initialize/run/stop
plus the background loops that tie the collaborators together).

Priority tiers (orders.Priority): EQUIP > CRAFT > GATHER > KEEP_STOCK >
AUTO_CRAFT > DEFAULT. EQUIP is reserved for equip requests
(OrderManager.request_equipment) and outranks everything else, so a
character waiting on gear preempts whatever they're currently doing rather
than finishing it first. AUTO_CRAFT sits just above DEFAULT busywork: it
auto-converts surplus of a "pure" single-use default-gathered raw material
(one that's only ever an ingredient in exactly one recipe, e.g.
copper_ore -> copper_bar) into its finished item, without ever eating into
that raw material's keep-in-stock floor -- see
OrderManager.refresh_auto_convert_orders(). See orders.INERTIA_BONUS for
the anti-thrashing bias applied to whatever order a character is currently
working; DEFAULT-tier orders get none, so they're preempted immediately by
anything else.

Breakpoints: a gathering character only re-evaluates for a *different*
order right after a bank deposit (inventory-full flush, or its target being
reached) -- never mid gather-action, since a single gather() call is atomic
and can't be interrupted. A crafting character re-evaluates between craft
batches for the same reason.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from account import Account
from config_watcher import ConfigWatcher
from database import GameDatabase
from events import EventBus
from executor import Executor
from models import Item
from order_manager import OrderManager, StockRule
from orders import WorkOrder
from roles import CharacterRole, DEFAULT_ROLES
from scheduler import Scheduler


class TaskEngine:
    """Owns the live WorkOrder pool (shared state) and one scheduling loop
    per character. Delegates order-creation, scheduling, and execution
    logic to order_manager.OrderManager / scheduler.Scheduler /
    executor.Executor respectively -- see module docstring above."""

    # Multiplier on poll_interval for _delivery_safety_sweep_loop: equipment
    # delivery is driven reactively by Executor's EquipmentRequested/
    # BankSynced subscriptions, so this loop is purely a backstop --
    # deliberately much less frequent than a per-tick scan, mirroring
    # Scheduler.IDLE_WAIT_FALLBACK_MULTIPLIER's role as "safety net, not
    # primary mechanism."
    DELIVERY_SWEEP_MULTIPLIER = 20

    # Same idea for _auto_convert_safety_sweep_loop: auto-conversion is
    # driven reactively by OrderManager's OrderCompleted/BankSynced
    # subscriptions, so this loop is purely a backstop against a
    # missed/raced event, not the primary mechanism.
    AUTO_CONVERT_SWEEP_MULTIPLIER = 20

    def __init__(
        self,
        account: Account,
        db: GameDatabase,
        roles: Optional[Dict[str, CharacterRole]] = None,
        poll_interval: float = 1.0,
    ):
        self.account = account
        self.db = db
        self.roles: Dict[str, CharacterRole] = roles or DEFAULT_ROLES
        self.poll_interval = poll_interval

        self.orders: Dict[int, WorkOrder] = {}
        self.stock_rules: List[StockRule] = []
        self.default_orders: Dict[str, WorkOrder] = {}   # character name -> its DEFAULT-tier order

        self._current_order: Dict[str, Optional[int]] = {}  # character name -> order id they're on
        self.running = False

        # Event bus (events.py) -- shared by all four collaborators via
        # their `engine` back-reference (engine.bus), so no constructor
        # signature changes were needed on OrderManager/Scheduler/Executor/
        # ConfigWatcher. Created before they're instantiated below so it's
        # always available by the time any collaborator's __init__ runs.
        self.bus = EventBus()
        # Late-bind onto Account too: Account is constructed and first
        # synced in main.py before this engine/bus exist, so it can't take
        # the bus as a constructor arg -- see Account.__init__'s comment on
        # self.bus. sync_bank()/sync_pending_items() emit BankSynced on
        # whatever bus is set here.
        self.account.set_bus(self.bus)

        # code -> the single Item that consumes it, for every default-gathered
        # raw material that's an ingredient in exactly one recipe. Computed
        # once (lazily, from the full item catalog) and cached -- see
        # OrderManager._build_single_use_conversions()/refresh_auto_convert_orders().
        self._single_use_conversions: Optional[Dict[str, Item]] = None

        # Collaborator modules -- see the module-boundary list in this
        # file's docstring. Each one just holds a reference back to this
        # engine and reads/writes the shared state declared above.
        self.order_manager = OrderManager(self)
        self.scheduler = Scheduler(self)
        self.executor = Executor(self)
        self.config_watcher = ConfigWatcher(self)

    # ------------------------------------------------------------------
    # Holdings (shared state-query helper, used by all four collaborators)
    # ------------------------------------------------------------------

    def held(self, code: str) -> int:
        """Total quantity of `code` across every character's inventory plus
        the bank -- mirrors planning.PlanRunner._held's accounting rule so
        completion is judged consistently under either system."""
        total = sum(
            item.quantity
            for character in self.account.characters.values()
            for item in character.inventory
            if item.code == code
        )
        total += sum(item.quantity for item in self.account.bank.items if item.code == code)
        return total

    def _order_for_code(self, code: str) -> Optional[WorkOrder]:
        for order in self.orders.values():
            if not order.done and order.code == code:
                return order
        return None

    def complete(self, order: WorkOrder) -> None:
        """Thin forward to Scheduler.complete -- kept on the engine because
        OrderManager and Executor both need to mark an order done without
        depending on each other directly, only on the shared engine facade."""
        self.scheduler.complete(order)

    # ------------------------------------------------------------------
    # Public facade -- forwards to the collaborator that owns each
    # responsibility, so external callers (main.py, tests) don't need to
    # know about the internal module split.
    # ------------------------------------------------------------------

    def request_item(self, code: str, quantity: int, **kwargs) -> Optional[int]:
        return self.order_manager.request_item(code, quantity, **kwargs)

    def request_equipment(self, character_name: str, code: str, slot: str, quantity: int = 1) -> Optional[int]:
        return self.order_manager.request_equipment(character_name, code, slot, quantity)

    def request_upgrades_for(self, character) -> List[int]:
        return self.order_manager.request_upgrades_for(character)

    def add_stock_rule(self, code: str, minimum: int) -> None:
        self.order_manager.add_stock_rule(code, minimum)

    def load_stock_rules_from_file(self, path: str = "stock_config.json") -> None:
        self.config_watcher.load_stock_rules_from_file(path)

    def refresh_stock_orders(self) -> None:
        self.order_manager.refresh_stock_orders()

    def refresh_auto_convert_orders(self) -> None:
        self.order_manager.refresh_auto_convert_orders()

    def set_default_gather_task(self, character_name: str, resource_code: str) -> Optional[WorkOrder]:
        return self.order_manager.set_default_gather_task(character_name, resource_code)

    def assign_default_gather_tasks(self) -> None:
        self.order_manager.assign_default_gather_tasks()

    def character_eligible(self, character, order: WorkOrder) -> bool:
        return self.scheduler.character_eligible(character, order)

    def select_order_for(self, character) -> Optional[WorkOrder]:
        return self.scheduler.select_order_for(character)

    def claim(self, character, order: WorkOrder) -> None:
        self.scheduler.claim(character, order)

    def release(self, character, order: WorkOrder) -> None:
        self.scheduler.release(character, order)

    def verify(self) -> bool:
        return self.order_manager.verify()

    def print_plan_tree(self) -> None:
        self.order_manager.print_plan_tree()

    async def character_loop(self, character) -> None:
        await self.scheduler.character_loop(character)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Requirement #4, 'Clean Slate': every character deposits gold and
        inventory into the bank before the scheduler starts handing out work.
        Runs all characters concurrently, and skips the walk back to each
        character's pre-deposit tile -- the scheduler assigns their first
        order immediately after this, so returning to origin would just be
        an extra trip in the wrong direction."""
        async def _clean_slate(character):
            if not character.is_inventory_empty:
                await character.deposit_all(return_to_origin=False)
            if character.gold > 0:
                await character.deposit_gold(character.gold, return_to_origin=False)

        await asyncio.gather(*(_clean_slate(c) for c in self.account.characters.values()))
        await self.account.sync_bank()

    async def _auto_convert_safety_sweep_loop(self) -> None:
        """Every poll_interval * AUTO_CONVERT_SWEEP_MULTIPLIER seconds, re-runs
        the bounded refresh_auto_convert_orders() sweep. Auto-conversion is
        normally driven reactively by OrderManager's OrderCompleted/BankSynced
        subscriptions; this loop exists only as a backstop in case a reactive
        event is ever missed. See ARCHITECTURE.md for the full rationale."""
        while self.running:
            await asyncio.sleep(self.poll_interval * self.AUTO_CONVERT_SWEEP_MULTIPLIER)
            try:
                self.order_manager.refresh_auto_convert_orders()
            except Exception as e:
                # Mirrors _delivery_safety_sweep_loop's per-sweep guard: one
                # bad sweep must not propagate out of asyncio.gather() in
                # run() and tear down every character's loop along with it.
                print(f"[TaskEngine] Error refreshing auto-convert orders: {e!r}")

    async def _delivery_safety_sweep_loop(self) -> None:
        """Every poll_interval * DELIVERY_SWEEP_MULTIPLIER seconds, re-checks
        every order with pending equip_requests for a deliverable item.
        Equipment delivery is normally driven reactively by Executor's
        EquipmentRequested/BankSynced subscriptions; this loop exists only
        as a backstop in case a reactive event is ever missed. See
        ARCHITECTURE.md for the full rationale."""
        while self.running:
            await asyncio.sleep(self.poll_interval * self.DELIVERY_SWEEP_MULTIPLIER)
            for order in list(self.orders.values()):
                if order.equip_requests:
                    try:
                        await self.executor._try_deliver_equipment(order)
                    except Exception as e:
                        # Mirrors character_loop's per-order guard: one failed
                        # delivery (e.g. a bank/API hiccup) must not propagate
                        # out of asyncio.gather() in run() and tear down every
                        # other character's loop along with it.
                        print(f"[TaskEngine] Error delivering equipment for order #{order.id} "
                              f"'{order.code}': {e!r}")

    async def run(self) -> None:
        self.order_manager.assign_default_gather_tasks()
        self.order_manager.refresh_stock_orders()
        self.order_manager.refresh_auto_convert_orders()
        self.order_manager.verify()
        self.order_manager.print_plan_tree()

        self.running = True
        loops = [self.scheduler.character_loop(c) for c in self.account.characters.values()]
        loops.append(self._delivery_safety_sweep_loop())
        loops.append(self._auto_convert_safety_sweep_loop())
        loops.append(self.config_watcher.loop())
        await asyncio.gather(*loops)

    def stop(self) -> None:
        """Stops the run() loops (they all check self.running) and
        unsubscribes every bus handler the four collaborators registered in
        their own __init__, via each collaborator's close(). See
        ARCHITECTURE.md for why this cleanup is needed. Each close() is
        idempotent, so calling stop() more than once is harmless."""
        self.running = False
        self.order_manager.close()
        self.scheduler.close()
        self.executor.close()
        self.config_watcher.close()
