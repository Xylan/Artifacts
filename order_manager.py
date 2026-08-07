#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
order_manager.py: Order *creation* and *bookkeeping* for task_runner.TaskEngine.

Owns everything about deciding WHAT work should exist:

  * request_item / _bump_ingredients / request_equipment / request_upgrades_for
    -- turning "I want N of code X" into CRAFT/GATHER WorkOrders, recursively
    expanding craft ingredients.
  * Keep-in-stock rules (add_stock_rule, refresh_stock_orders) -- NOTE the
    *file-backed* loader (load_stock_rules_from_file) lives in
    config_watcher.py; this module only owns the StockRule list and turning
    a rule into a live order once holdings dip below its minimum.
  * Auto-convert (_build_single_use_conversions, refresh_auto_convert_orders)
    -- turning surplus single-use raw materials into their one finished
    product.
  * Default (fallback) gather tasks (set_default_gather_task,
    assign_default_gather_tasks).
  * Plan verification/debugging (verify, print_plan_tree).

It does NOT decide who works an order, when, or how (that's scheduler.py /
executor.py) -- it only creates, sizes, and tears down orders. All state
(self.engine.orders, self.engine.stock_rules, self.engine.default_orders)
lives on the shared TaskEngine instance passed in at construction; this
class is a thin operator over that shared state.

For why this module is shaped this way (the God-module split, the
event-bus conversion, the concurrency-audit fixes), see ARCHITECTURE.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from events import (
    BankSynced, EquipmentRequested, OrderCompleted, OrderCreated, OrderUpdated,
    StockBelowMinimum,
)
from models import Item, find_quantity
from orders import WorkOrder, OrderKind, Priority
from roles import GATHER_SKILLS

if TYPE_CHECKING:
    from task_runner import TaskEngine


@dataclass
class StockRule:
    """'Always have at least `minimum` of `code` across bank + inventories.'
    Generates the lowest-priority (KEEP_STOCK) orders once holdings dip
    below the threshold -- see OrderManager.refresh_stock_orders(). Loaded
    in bulk from stock_config.json by config_watcher.ConfigWatcher, or
    appended to one at a time via OrderManager.add_stock_rule()."""
    code: str
    minimum: int


class OrderManager:
    """Operates on TaskEngine's shared order pool. See module docstring."""

    def __init__(self, engine: "TaskEngine"):
        self.engine = engine

        # Auto-convert reacts to OrderCompleted (narrows to the one
        # completed code via _maybe_auto_convert) and BankSynced (which
        # carries no code, so it re-runs the bounded
        # refresh_auto_convert_orders sweep instead). Keep-in-stock
        # threshold detection follows the same shape: _check_stock_thresholds
        # (the detector, bounded to engine.stock_rules) is subscribed to
        # both events too, and emits StockBelowMinimum for anything under
        # its floor; _on_stock_below_minimum is the reaction, narrowed to
        # just the one code named by the event. See ARCHITECTURE.md for why
        # these two events cover every point a shortfall/surplus can appear.
        #
        # Subscriptions are tracked in self._subscriptions so close() can
        # unsubscribe them all -- see that method.
        self._subscriptions = [
            (OrderCompleted, self.engine.bus.subscribe(OrderCompleted, self._on_order_completed)),
            (BankSynced, self.engine.bus.subscribe(BankSynced, self._on_bank_synced)),
            (BankSynced, self.engine.bus.subscribe(BankSynced, self._check_stock_thresholds)),
            (OrderCompleted, self.engine.bus.subscribe(OrderCompleted, self._check_stock_thresholds)),
            (StockBelowMinimum, self.engine.bus.subscribe(StockBelowMinimum, self._on_stock_below_minimum)),
        ]

    def close(self) -> None:
        """Unsubscribes every handler this instance registered on
        engine.bus. Called by TaskEngine.stop(); idempotent. Note
        _check_stock_thresholds is subscribed to BOTH OrderCompleted and
        BankSynced as two separate registrations (see __init__), each
        unsubscribed here by its own (event_type, handler) pair."""
        for event_type, handler in self._subscriptions:
            self.engine.bus.unsubscribe(event_type, handler)

    # ------------------------------------------------------------------
    # Order creation / expansion (craft/gather dependency resolution)
    # ------------------------------------------------------------------

    def request_item(
        self,
        code: str,
        quantity: int,
        tier: Optional[Priority] = None,
        requester: Optional[str] = None,
        equip_slot: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> Optional[int]:
        """Ensures at least `quantity` more of `code` will be produced,
        creating/bumping CRAFT or GATHER orders (recursively, for craft
        ingredients) as needed.

        `tier=None` (the default) gives each generated order its "natural"
        priority: CRAFT for craftable items, GATHER for gatherable ones.
        Passing tier=Priority.KEEP_STOCK (or DEFAULT) instead forces that
        single tier across the whole expansion, so a keep-in-stock request
        never jumps ahead of a genuine equipment/gather request even if it
        happens to be craftable. request_equipment() below instead forces
        tier=Priority.EQUIP across the whole expansion, so an equip request
        outranks and interrupts ordinary CRAFT/GATHER work.

        Returns the id of the top-level order for `code`, or None if `code`
        is neither craftable nor gatherable (buy it manually). Safe to call
        repeatedly -- an existing live order for the same code has its
        target bumped in place rather than being duplicated.

        This is the single place OrderCreated/OrderUpdated get emitted on
        `engine.bus` (a fresh order -> OrderCreated, bumping an existing
        one's target_quantity -> OrderUpdated), plus EquipmentRequested
        whenever a `requester`/`equip_slot` pair is queued. Every other
        OrderManager method that creates/bumps orders funnels through here
        rather than emitting anything itself.
        """
        engine = self.engine
        if quantity <= 0:
            return None

        existing = engine._order_for_code(code)
        if existing:
            existing.target_quantity += quantity
            if existing.kind == OrderKind.CRAFT:
                self._bump_ingredients(existing, quantity, tier)
            if requester and equip_slot:
                # Queue this recipient too, rather than only honoring the
                # first character who requested `code` -- multiple
                # characters wanting the same upgrade is common.
                existing.equip_requests.extend([(requester, equip_slot)] * quantity)
                engine.bus.emit(EquipmentRequested(
                    order_id=existing.id, character_name=requester, code=code, slot=equip_slot,
                ))
            # Emitted after the equip_requests bump above so a subscriber
            # reacting to OrderUpdated already sees the final target/equip
            # state if it looks the order back up via engine.orders[...].
            engine.bus.emit(OrderUpdated(
                order_id=existing.id, code=existing.code,
                target_quantity=existing.target_quantity, priority=existing.priority,
            ))
            return existing.id

        # If `code` is already in the bank in sufficient quantity, mark the
        # new order done immediately (via engine.complete() below) instead
        # of leaving it live and unclaimed -- select_order_for() never
        # hands out an order whose target is already covered by held()
        # (bank + inventories), so an uncompleted order like that would
        # otherwise sit forever looking like unfinished work, and any
        # equip_requests attached to it would never get delivered.
        bank_qty = find_quantity(engine.account.bank.items, code)
        equip_requests = [(requester, equip_slot)] * quantity if requester and equip_slot else []

        item = engine.db.items.get_item_obj(code)
        if item and item.craft:
            produces = max(1, item.craft.quantity)
            crafts_needed = -(-quantity // produces)  # ceil
            order = WorkOrder(
                kind=OrderKind.CRAFT,
                priority=tier if tier is not None else Priority.CRAFT,
                code=code, node_code=item.craft.skill, skill=item.craft.skill,
                skill_level=item.craft.level, target_quantity=quantity,
                produces_per_action=produces, parent_id=parent_id,
                equip_requests=equip_requests,
            )
            self._finalize_new_order(order, requester, equip_slot, code)
            if bank_qty >= quantity:
                engine.complete(order)
            else:
                for ing in item.craft.items:
                    self.request_item(ing.code, ing.quantity * crafts_needed, tier=tier, parent_id=order.id)
            return order.id

        resource = engine.db.resources.find_best_for_item(code)
        if resource:
            drop = next((d for d in resource["drops"] if d["code"] == code), {})
            avg_yield = max(1, (drop.get("min_quantity", 1) + drop.get("max_quantity", 1)) / 2)
            order = WorkOrder(
                kind=OrderKind.GATHER,
                priority=tier if tier is not None else Priority.GATHER,
                code=code, node_code=resource["code"], skill=resource["skill"],
                skill_level=resource["level"], target_quantity=quantity,
                produces_per_action=int(avg_yield), parent_id=parent_id,
                equip_requests=equip_requests,
            )
            self._finalize_new_order(order, requester, equip_slot, code)
            if bank_qty >= quantity:
                engine.complete(order)
            return order.id

        print(f"[OrderManager] '{code}' is neither craftable nor gatherable -- skipping (buy/GE manually).")
        return None

    def _finalize_new_order(
        self, order: WorkOrder, requester: Optional[str], equip_slot: Optional[str], code: str,
    ) -> None:
        """Shared tail of request_item's CRAFT and GATHER branches: inserts
        the freshly-built `order` into engine.orders and emits OrderCreated,
        plus EquipmentRequested if a requester/equip_slot pair was given.
        Does not decide completion -- callers still check bank_qty and
        expand ingredients (CRAFT only) themselves, since that differs
        between the two branches."""
        engine = self.engine
        engine.orders[order.id] = order
        engine.bus.emit(OrderCreated(
            order_id=order.id, code=order.code, kind=order.kind,
            priority=order.priority, target_quantity=order.target_quantity,
        ))
        if requester and equip_slot:
            engine.bus.emit(EquipmentRequested(
                order_id=order.id, character_name=requester, code=code, slot=equip_slot,
            ))

    def _bump_ingredients(self, craft_order: WorkOrder, extra_output: int, tier: Optional[Priority]) -> None:
        """Cascades a target bump down to a craft order's ingredient
        orders. Emits nothing directly -- routes back through
        request_item(), which owns all order-change emissions."""
        item = self.engine.db.items.get_item_obj(craft_order.code)
        if not item or not item.craft:
            return
        extra_crafts = -(-extra_output // craft_order.produces_per_action)
        for ing in item.craft.items:
            self.request_item(ing.code, ing.quantity * extra_crafts, tier=tier, parent_id=craft_order.id)

    def request_equipment(self, character_name: str, code: str, slot: str, quantity: int = 1) -> Optional[int]:
        """Requests `code` on behalf of `character_name`, who wants it
        delivered from the bank and equipped once the order completes --
        see executor.Executor._try_deliver_equipment(). Forces
        Priority.EQUIP across the entire craft/gather expansion (the
        top-level order for `code` and every recursive ingredient order),
        so an equip request interrupts and outranks ordinary CRAFT/GATHER
        work at every step, not just the final craft. Emits nothing
        directly -- routes through request_item(), same as
        _bump_ingredients."""
        return self.request_item(code, quantity, tier=Priority.EQUIP, requester=character_name, equip_slot=slot)

    def request_upgrades_for(self, character) -> List[int]:
        """Wraps planning.GearList.for_upgrades: auto-detects every
        craftable armor/weapon upgrade for `character` and requests it with
        equip-on-completion wired up (requirement #4)."""
        from planning import GearList
        gear_list = GearList.for_upgrades(character, self.engine.db)
        ids = []
        for code, qty in gear_list.wants.items():
            item = self.engine.db.items.get_item_obj(code)
            if item and item.is_equipable:
                slot = f"{item.type}_slot"
                order_id = self.request_equipment(character.name, code, slot, qty)
            else:
                order_id = self.request_item(code, qty)
            if order_id:
                ids.append(order_id)
        return ids

    # ------------------------------------------------------------------
    # Keep-in-stock
    # ------------------------------------------------------------------

    def add_stock_rule(self, code: str, minimum: int) -> None:
        self.engine.stock_rules.append(StockRule(code=code, minimum=minimum))

    def refresh_stock_orders(self) -> None:
        """Full sweep over engine.stock_rules: tops up anything not already
        covered by a live order for that code, always at KEEP_STOCK tier so
        it never outranks a genuine gather/craft request. Called at startup
        (TaskEngine.run) and reactively on ConfigChanged
        (ConfigWatcher._on_config_changed); per-code shortfalls are also
        caught without a full re-sweep via _on_stock_below_minimum below.
        Bounded to engine.stock_rules, never the whole order pool. Emits
        nothing directly -- _maybe_queue_stock_order does."""
        engine = self.engine
        for rule in engine.stock_rules:
            self._maybe_queue_stock_order(rule.code, rule.minimum)

    def _maybe_queue_stock_order(self, code: str, minimum: int) -> None:
        """Per-code keep-in-stock logic, extracted so a single code can be
        topped up without re-sweeping every other stock rule. No-ops if
        `code` is already at/above `minimum`, or if a live order for it
        already exists (whatever tier) -- both checks make repeated calls
        for the same shortfall idempotent."""
        engine = self.engine
        current = engine.held(code)
        if current - minimum >= 0:
            return
        if engine._order_for_code(code):
            return  # already being produced, whatever tier that order is at
        self.request_item(code, minimum - current, tier=Priority.KEEP_STOCK)

    # ------------------------------------------------------------------
    # Auto-convert (single-use default-gathered raw materials)
    # ------------------------------------------------------------------

    def _build_single_use_conversions(self) -> Dict[str, Item]:
        """One-time (cached on engine._single_use_conversions) scan of the
        full item catalog, mapping each default-gathered raw material's
        code to the single Item that consumes it as a craft ingredient --
        but ONLY for raw materials used by exactly one recipe (e.g.
        copper_ore -> copper_bar, raw_chicken -> cooked_chicken). A raw
        material feeding two or more different items isn't a 'pure' 1:1
        conversion, so it's deliberately excluded: we wouldn't know which
        of its consumers to auto-craft into."""
        engine = self.engine
        raw_codes = {order.code for order in engine.default_orders.values()}
        consumers: Dict[str, List[Item]] = {}
        for item in engine.db.items.get_all_items_obj():
            if not item.craft:
                continue
            for ing in item.craft.items:
                if ing.code in raw_codes:
                    consumers.setdefault(ing.code, []).append(item)
        return {code: matches[0] for code, matches in consumers.items() if len(matches) == 1}

    def refresh_auto_convert_orders(self) -> None:
        """Full sweep over every 'pure' single-use conversion candidate (see
        _build_single_use_conversions) -- bounded to that cached dict, never
        the whole order pool. Runs on the BankSynced reactive path (see
        _on_bank_synced below), on TaskEngine's low-frequency safety-sweep
        backstop, and once at startup (TaskEngine.run). Per-candidate logic
        lives in _maybe_auto_convert so a reactive handler can react to a
        single code without re-checking every other candidate."""
        engine = self.engine
        if engine._single_use_conversions is None:
            engine._single_use_conversions = self._build_single_use_conversions()

        for raw_code in engine._single_use_conversions:
            self._maybe_auto_convert(raw_code)

    def _maybe_auto_convert(self, raw_code: str) -> None:
        """If `raw_code` is a 'pure' single-use default-gathered raw
        material (see _build_single_use_conversions), queues a
        Priority.AUTO_CRAFT order to convert whatever SURPLUS currently
        sits above that raw material's keep-in-stock floor into the
        finished item -- e.g. only turning copper_ore into copper_bar once
        there's more copper_ore on hand than committed to keeping in stock.

        The floor is the matching StockRule.minimum if registered via
        add_stock_rule(), otherwise 100. Never dips the raw material below
        that floor, and never creates a second order for the same target
        item while one is already live. Emits nothing directly -- routes
        through request_item(). Silently no-ops if `raw_code` isn't a known
        conversion candidate, so callers like _on_order_completed can pass
        any code through unconditionally."""
        engine = self.engine
        if engine._single_use_conversions is None:
            engine._single_use_conversions = self._build_single_use_conversions()

        target_item = engine._single_use_conversions.get(raw_code)
        if target_item is None:
            return

        if engine._order_for_code(target_item.code):
            return  # a conversion (or some other request) for this item is already in flight

        rule = next((r for r in engine.stock_rules if r.code == raw_code), None)
        floor = rule.minimum if rule else 100

        spare = engine.held(raw_code) - floor
        if spare <= 0:
            return

        ingredient = next((i for i in target_item.craft.items if i.code == raw_code), None)
        if not ingredient or ingredient.quantity <= 0:
            return

        craftable = spare // ingredient.quantity
        if craftable <= 0:
            return

        produced = craftable * max(1, target_item.craft.quantity)
        self.request_item(target_item.code, produced, tier=Priority.AUTO_CRAFT)

    async def _on_order_completed(self, event: OrderCompleted) -> None:
        """Checks just the one completed order's code for a possible
        auto-convert opportunity, rather than re-scanning every candidate.
        _maybe_auto_convert no-ops harmlessly for a code that isn't a
        conversion candidate."""
        self._maybe_auto_convert(event.code)

    async def _on_bank_synced(self, event: BankSynced) -> None:
        """BankSynced doesn't say which code changed, so this re-runs the
        full (but still bounded) refresh_auto_convert_orders sweep."""
        self.refresh_auto_convert_orders()

    # ------------------------------------------------------------------
    # Keep-in-stock threshold detection
    # ------------------------------------------------------------------

    def _check_stock_thresholds(self, event: Optional[object] = None) -> None:
        """Detector half of keep-in-stock threshold detection: bounded
        sweep over engine.stock_rules that emits StockBelowMinimum for
        every tracked code currently under its floor. Subscribed to both
        BankSynced and OrderCompleted in __init__, which between them fire
        at every point a shortfall could newly appear (deposits, gathers/
        crafts completing).

        Takes an optional, unused `event` so it can be used directly as an
        EventBus handler for either event type -- neither event says
        *which* code changed, so this always rechecks every tracked rule.
        Re-emitting for an already-reported shortfall is harmless; creating
        the actual order is _on_stock_below_minimum's job, which is
        idempotent."""
        engine = self.engine
        for rule in engine.stock_rules:
            current = engine.held(rule.code)
            if current < rule.minimum:
                engine.bus.emit(StockBelowMinimum(code=rule.code, current=current, minimum=rule.minimum))

    def _on_stock_below_minimum(self, event: StockBelowMinimum) -> None:
        """Reaction half of keep-in-stock threshold detection: narrows to
        just the one code the event names (via _maybe_queue_stock_order)
        rather than re-sweeping every rule. Safe to fire repeatedly for the
        same shortfall -- idempotent via _maybe_queue_stock_order's
        existing-order guard."""
        self._maybe_queue_stock_order(event.code, event.minimum)

    # ------------------------------------------------------------------
    # Default tasks (requirement #5: zero inertia, lowest priority)
    # ------------------------------------------------------------------

    def set_default_gather_task(self, character_name: str, resource_code: str) -> Optional[WorkOrder]:
        """Registers a fallback gather order for `character_name`, used only
        when nothing else is claimable for them. Effectively never
        "completes" (huge target) -- it's busywork, not a real goal."""
        engine = self.engine
        resource = engine.db.resources.get_resource(resource_code)
        if not resource:
            print(f"[OrderManager] Unknown resource '{resource_code}' for default task.")
            return None
        drop = resource["drops"][0] if resource.get("drops") else {"code": resource_code}
        order = WorkOrder(
            kind=OrderKind.GATHER, priority=Priority.DEFAULT,
            code=drop["code"], node_code=resource["code"], skill=resource["skill"],
            skill_level=resource["level"], target_quantity=10 ** 9, produces_per_action=1,
            only_for=character_name,
        )
        engine.orders[order.id] = order
        engine.default_orders[character_name] = order
        return order

    def assign_default_gather_tasks(self) -> None:
        """Requirement #5/#3: gives every character without an explicit
        default order (set_default_gather_task not already called for
        them) a fallback gather task, so nobody sits fully idle when
        nothing else is claimable. Picks the lowest-level resource for the
        first skill in the character's role.gather_priority cascade that
        they currently meet the level requirement for. Safe to call
        repeatedly; characters that already have a default order (explicit
        or previously assigned here) are left untouched."""
        engine = self.engine
        for name, character in engine.account.characters.items():
            if name in engine.default_orders:
                continue

            role = engine.roles.get(name)
            priorities = role.gather_priority if role else GATHER_SKILLS

            chosen_resource = None
            for skill in priorities:
                char_level = getattr(character.skills, f"{skill}_level", 0)
                candidates = engine.db.resources.get_by_skill(skill, max_level=char_level)
                if candidates:
                    chosen_resource = min(candidates, key=lambda r: r.get("level", 1))
                    break

            if not chosen_resource:
                print(f"[OrderManager] No eligible default gather resource found for '{name}'.")
                continue

            self.set_default_gather_task(name, chosen_resource["code"])
            print(f"[OrderManager] Default gather task for '{name}': '{chosen_resource['code']}' "
                  f"(skill={chosen_resource.get('skill')}).")

    # ------------------------------------------------------------------
    # Plan verification / debugging
    # ------------------------------------------------------------------

    def verify(self) -> bool:
        """Sanity-checks that every live CRAFT order's ingredient orders
        were sized to actually cover its target_quantity -- catches
        expansion bugs before characters start burning cooldowns on a plan
        that can never finish."""
        engine = self.engine
        ok = True
        for order in engine.orders.values():
            if order.kind != OrderKind.CRAFT or order.done:
                continue
            item = engine.db.items.get_item_obj(order.code)
            if not item or not item.craft:
                continue
            crafts_needed = -(-order.target_quantity // order.produces_per_action)
            for ing in item.craft.items:
                needed = ing.quantity * crafts_needed
                ing_order = engine._order_for_code(ing.code)
                covered = (ing_order.target_quantity if ing_order else 0) + engine.held(ing.code)
                if covered < needed:
                    print(f"[OrderManager] VERIFY FAIL: '{order.code}' needs {needed}x '{ing.code}' "
                          f"but only {covered} planned/held.")
                    ok = False
        print(f"[OrderManager] Plan verification {'passed' if ok else 'FAILED'} "
              f"({len(engine.orders)} live orders).")
        return ok

    def print_plan_tree(self) -> None:
        engine = self.engine
        roots = [o for o in engine.orders.values() if o.parent_id is None]

        def _print(order: WorkOrder, depth: int):
            tag = "DONE" if order.done else f"{order.kind.name}/{order.priority.name}"
            who = order.locked_to or (",".join(order.claimed_by) or "-")
            print(f"{'  ' * depth}- [{tag}] {order.code} x{order.target_quantity} "
                  f"(skill={order.skill or '-'}) -> {who}")
            for child in engine.orders.values():
                if child.parent_id == order.id:
                    _print(child, depth + 1)

        for root in roots:
            _print(root, 0)
