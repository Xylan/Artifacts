#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
order_manager.py: Order *creation* and *bookkeeping* for task_runner.TaskEngine.

Split out of task_runner.py (formerly a "God module") per the module
boundary described there: this file owns everything about deciding WHAT
work should exist --

  * request_item / _bump_ingredients / request_equipment / request_upgrades_for
    -- turning "I want N of code X" into CRAFT/GATHER WorkOrders, recursively
    expanding craft ingredients.
  * Keep-in-stock rules (add_stock_rule, refresh_stock_orders) -- NOTE the
    *file-backed* loader (load_stock_rules_from_file) has moved to
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
class is a thin, statelessly-reusable operator over that shared state,
mirroring executor.py/scheduler.py/config_watcher.py's pattern so every
piece of the old task_runner.py can be understood/tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from events import (
    BankSynced, EquipmentRequested, OrderCompleted, OrderCreated, OrderUpdated,
    StockBelowMinimum,
)
from models import Item
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

        # Reactive auto-convert (TODO task 8), replacing
        # TaskEngine._auto_convert_loop's old "re-scan every single-use
        # conversion candidate, every tick" poll. Same pattern as
        # Executor.__init__'s EquipmentRequested/BankSynced subscriptions:
        #   * OrderCompleted tells us exactly which code just finished --
        #     if a GATHER order for a raw material completes, that's new
        #     supply that might now clear a conversion's surplus floor, so
        #     _on_order_completed narrows straight to that one raw code via
        #     _maybe_auto_convert rather than re-checking every candidate.
        #   * BankSynced doesn't carry which code changed, so the best we
        #     can do without over/under-scoping is the same bounded sweep
        #     refresh_auto_convert_orders always did -- it only ever
        #     iterates the cached single_use_conversions dict (a handful of
        #     entries), never "everything," so reusing it here isn't a
        #     regression to full-pool scanning.
        # Reactive keep-in-stock threshold detection (TODO task 10),
        # replacing refresh_stock_orders()'s old "only ever runs at
        # startup or on a config reload" cadence. OrderCompleted and
        # BankSynced are exactly the two events that already fire at every
        # point the TODO calls out (deposits, gathers completing, crafts
        # completing all route through Executor's deposit-then-sync_bank
        # calls, which emit BankSynced; gathers/crafts finishing also emit
        # OrderCompleted) -- no new emission points needed elsewhere.
        # _check_stock_thresholds is the *detector* (bounded to
        # engine.stock_rules, emits StockBelowMinimum for anything under
        # its floor); _on_stock_below_minimum is refresh_stock_orders'
        # *reaction* to that event, narrowed to just the one code named by
        # the event rather than re-sweeping every rule.
        #
        # Subscriptions are tracked in self._subscriptions (TODO task 12) so
        # close() can unsubscribe them all -- see that method.
        self._subscriptions = [
            (OrderCompleted, self.engine.bus.subscribe(OrderCompleted, self._on_order_completed)),
            (BankSynced, self.engine.bus.subscribe(BankSynced, self._on_bank_synced)),
            (BankSynced, self.engine.bus.subscribe(BankSynced, self._check_stock_thresholds)),
            (OrderCompleted, self.engine.bus.subscribe(OrderCompleted, self._check_stock_thresholds)),
            (StockBelowMinimum, self.engine.bus.subscribe(StockBelowMinimum, self._on_stock_below_minimum)),
        ]

    def close(self) -> None:
        """Unsubscribes every handler this instance registered on
        engine.bus (TODO task 12: subscriber cleanup on TaskEngine.stop()).
        Idempotent -- see Executor.close()'s matching docstring. Note this
        subscribes _check_stock_thresholds to BOTH OrderCompleted and
        BankSynced as two separate registrations (see __init__) -- both are
        unsubscribed here, each keyed by its own (event_type, handler)
        pair, so EventBus.unsubscribe's list.remove() targets the right
        one."""
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
        priority: CRAFT for craftable items, GATHER for gatherable ones --
        per the Crafting > Gathering rule. Passing tier=Priority.KEEP_STOCK
        (or DEFAULT) instead forces that single tier across the whole
        expansion, so a keep-in-stock request never jumps ahead of a
        genuine equipment/gather request even if it happens to be craftable.
        request_equipment() below instead forces tier=Priority.EQUIP across
        the whole expansion (top-level order AND every recursive ingredient
        order), so an equip request -- craft step, gather steps, all of it --
        outranks and interrupts ordinary CRAFT/GATHER work rather than just
        matching its natural tier and waiting in line behind it.

        Returns the id of the top-level order for `code`, or None if `code`
        is neither craftable nor gatherable (buy it manually). Safe to call
        repeatedly -- an existing live order for the same code has its
        target bumped in place rather than being duplicated.

        This is the single place OrderCreated/OrderUpdated get emitted on
        `engine.bus` (a fresh order -> OrderCreated, bumping an existing
        one's target_quantity -> OrderUpdated), plus EquipmentRequested
        whenever a `requester`/`equip_slot` pair is queued. Every other
        OrderManager method that creates/bumps orders (_bump_ingredients,
        request_equipment, refresh_stock_orders, refresh_auto_convert_orders)
        funnels through here rather than emitting anything itself, so a
        subscriber only ever needs to listen in one place to hear about
        every order change regardless of which caller triggered it.
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
                # Queue this recipient too, rather than only ever honoring
                # the first character who ever requested `code` -- multiple
                # characters wanting the same upgrade is the common case
                # (request_upgrades_for is called once per character).
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

        # Check the bank before spinning up a new craft/gather order -- if
        # `code` is already sitting in the bank in sufficient quantity,
        # there's nothing left to produce. Previously a new order was
        # created unconditionally here: since select_order_for() already
        # refuses to hand out any order whose target is <= held() (bank +
        # inventories), an order that was fully covered by existing bank
        # stock the moment it was created would never get claimed/worked by
        # anyone -- but it also never ran through complete() (that only
        # fires at the end of _run_craft_step/_run_gather_step), so it just
        # sat in self.orders forever showing up as a "live" craft/gather
        # request for an item that, in practice, was already finished, and
        # any equip_requests attached to it never got delivered. Marking it
        # done immediately when the bank already covers it fixes both: the
        # order stops looking like unfinished work, and delivery (which
        # only cares about equip_requests, not order.done) proceeds right
        # away instead of waiting on a completion event that was never
        # going to happen.
        bank_qty = next((i.quantity for i in engine.account.bank.items if i.code == code), 0)
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
            engine.orders[order.id] = order
            engine.bus.emit(OrderCreated(
                order_id=order.id, code=order.code, kind=order.kind,
                priority=order.priority, target_quantity=order.target_quantity,
            ))
            if requester and equip_slot:
                engine.bus.emit(EquipmentRequested(
                    order_id=order.id, character_name=requester, code=code, slot=equip_slot,
                ))
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
            engine.orders[order.id] = order
            engine.bus.emit(OrderCreated(
                order_id=order.id, code=order.code, kind=order.kind,
                priority=order.priority, target_quantity=order.target_quantity,
            ))
            if requester and equip_slot:
                engine.bus.emit(EquipmentRequested(
                    order_id=order.id, character_name=requester, code=code, slot=equip_slot,
                ))
            if bank_qty >= quantity:
                engine.complete(order)
            return order.id

        print(f"[OrderManager] '{code}' is neither craftable nor gatherable -- skipping (buy/GE manually).")
        return None

    def _bump_ingredients(self, craft_order: WorkOrder, extra_output: int, tier: Optional[Priority]) -> None:
        """No event emission of its own -- every ingredient bump/creation
        below routes back through request_item(), which is the single
        place OrderCreated/OrderUpdated get emitted (see its docstring)."""
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
        top-level order for `code` and every recursive ingredient order
        request_item() spins up for it), not just the top-level order -- an
        equip request is meant to interrupt whatever a character is
        currently doing, and that only works end-to-end if the
        ingredient-gathering/crafting steps feeding it also outrank
        ordinary CRAFT/GATHER work along the way, not just the final craft
        step. Like _bump_ingredients, emits nothing directly -- request_item
        (and its recursive ingredient calls) is where OrderCreated/
        OrderUpdated/EquipmentRequested all fire, so every EQUIP-tier order
        this expands into is covered."""
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
        """Bottom-of-the-barrel: only tops up what isn't already covered by
        a live order for that code, and always at KEEP_STOCK tier so it
        never outranks a genuine gather/craft request. Called at startup
        (TaskEngine.run), reactively on ConfigChanged
        (ConfigWatcher._on_config_changed), and now also -- per rule, not as
        a full re-sweep -- via _on_stock_below_minimum below (TODO task 10).
        Bounded to engine.stock_rules (a handful of entries), never the
        whole order pool. Emits nothing directly -- _maybe_queue_stock_order
        (and, inside it, request_item()) is what fires OrderCreated/
        OrderUpdated for any KEEP_STOCK order this creates."""
        engine = self.engine
        for rule in engine.stock_rules:
            self._maybe_queue_stock_order(rule.code, rule.minimum)

    def _maybe_queue_stock_order(self, code: str, minimum: int) -> None:
        """Per-code keep-in-stock logic (TODO task 10, extracted from
        refresh_stock_orders so a single code can be topped up without
        re-sweeping every other stock rule -- mirrors how
        _maybe_auto_convert was pulled out of refresh_auto_convert_orders
        for task 8). No-ops if `code` is already at/above `minimum`, or if
        a live order for it already exists (whatever tier that order is
        at) -- both checks make repeated calls for the same shortfall
        idempotent, which matters since _on_stock_below_minimum can fire
        once per BankSynced/OrderCompleted while the shortfall persists."""
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
        _build_single_use_conversions) -- bounded to that cached dict (a
        handful of entries), never the whole order pool. This is now the
        BankSynced reactive path (see _on_bank_synced below) plus
        TaskEngine._auto_convert_safety_sweep_loop's much-lower-frequency
        backstop (TODO task 8); startup (TaskEngine.run) still calls it once
        up front too. Per-candidate logic lives in _maybe_auto_convert so
        OrderCompleted can react to a single just-finished raw material
        without re-checking every other candidate."""
        engine = self.engine
        if engine._single_use_conversions is None:
            engine._single_use_conversions = self._build_single_use_conversions()

        for raw_code in engine._single_use_conversions:
            self._maybe_auto_convert(raw_code)

    def _maybe_auto_convert(self, raw_code: str) -> None:
        """Priority.AUTO_CRAFT tier (above DEFAULT busywork, below every real
        KEEP_STOCK/GATHER/CRAFT/EQUIP request -- see orders.Priority). If
        `raw_code` is a 'pure' single-use default-gathered raw material (see
        _build_single_use_conversions), queues a craft order to convert
        whatever SURPLUS currently sits above that raw material's
        keep-in-stock floor into the finished item -- e.g. only turning
        copper_ore into copper_bar once there's more copper_ore on hand than
        we've committed to keeping in stock.

        The floor is the matching StockRule.minimum if one's been
        registered via add_stock_rule() for that raw material's code,
        otherwise 100 (per spec: 'or 100 if that isn't set'). Never dips the
        raw material below that floor, and never creates a second order for
        the same target item while one is already live (whatever tier it's
        at). Emits nothing directly -- the request_item() call below is
        what fires OrderCreated/OrderUpdated for any AUTO_CRAFT order this
        creates. No-op (silently) if `raw_code` isn't a known single-use
        conversion candidate at all -- lets callers like
        _on_order_completed pass any completed order's code through
        unconditionally rather than pre-filtering."""
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
        """React to a single order finishing -- if it was a GATHER order for
        a raw material that's a single-use conversion candidate, that's
        fresh supply that might newly clear the conversion's surplus floor,
        so check just that one code (TODO task 8) instead of re-scanning
        every candidate. _maybe_auto_convert no-ops harmlessly for any
        completed order whose code isn't a conversion candidate (e.g. a
        CRAFT order, or a GATHER order for something with no single-use
        consumer), so no pre-filtering is needed here."""
        self._maybe_auto_convert(event.code)

    async def _on_bank_synced(self, event: BankSynced) -> None:
        """React to bank contents changing by re-running the full (but
        bounded -- see refresh_auto_convert_orders) sweep over every
        single-use conversion candidate: BankSynced doesn't say *which*
        item changed, so this can't narrow further than that, but it's
        still scoped to just the cached conversion candidates, never the
        whole order pool."""
        self.refresh_auto_convert_orders()

    # ------------------------------------------------------------------
    # Keep-in-stock threshold detection (TODO task 10, reactive)
    # ------------------------------------------------------------------

    def _check_stock_thresholds(self, event: Optional[object] = None) -> None:
        """Detector half of TODO task 10: bounded sweep over
        engine.stock_rules (never the whole order pool -- same bound
        refresh_stock_orders itself always used) that emits
        StockBelowMinimum for every tracked code currently sitting under
        its floor. Subscribed to BankSynced and OrderCompleted in
        __init__, which between them fire at every point the TODO calls
        out -- deposits (Executor's deposit-then-sync_bank calls in
        _switch_task/_run_gather_step/_run_craft_step/
        _try_deliver_equipment all emit BankSynced), gathers completing,
        and crafts completing (both also emit OrderCompleted) -- so no new
        emission points were needed in executor.py/account.py themselves.

        Takes an optional `event` purely so it can be used directly as an
        EventBus handler for either event type without a wrapper lambda;
        the event's own fields are never inspected -- neither BankSynced
        nor OrderCompleted says *which* code(s) may have crossed a
        threshold, so, like OrderManager._on_bank_synced above, this can't
        narrow further than 'recheck every tracked rule.' Emitting is
        cheap/idempotent (no order is created here -- that's
        _on_stock_below_minimum's job, itself idempotent via
        _maybe_queue_stock_order's existing-order guard), so re-emitting
        for a shortfall that was already reported on a previous call is
        harmless."""
        engine = self.engine
        for rule in engine.stock_rules:
            current = engine.held(rule.code)
            if current < rule.minimum:
                engine.bus.emit(StockBelowMinimum(code=rule.code, current=current, minimum=rule.minimum))

    def _on_stock_below_minimum(self, event: StockBelowMinimum) -> None:
        """Reaction half of TODO task 10: refresh_stock_orders' response to
        a StockBelowMinimum event, narrowed to just the one code the event
        names (via _maybe_queue_stock_order) rather than re-sweeping every
        rule the way refresh_stock_orders() itself does at startup/on
        ConfigChanged. Safe to fire repeatedly for the same shortfall --
        _maybe_queue_stock_order no-ops once a KEEP_STOCK order already
        exists for the code."""
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
