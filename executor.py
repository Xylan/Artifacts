#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
executor.py: The business logic for actually carrying out a WorkOrder,
step by step.

Split out of task_runner.py (formerly a "God module"): this file owns HOW
a claimed order gets worked -- switching tasks (with deposit-on-switch),
delivering/equipping finished gear, running one gather action, and running
one craft batch (sized to what the character can actually carry/afford).

Boundary vs character.py: Character owns the raw capabilities (how to talk
to the API to move, gather, craft, deposit, equip, ...) as its own methods.
This module is what DECIDES to call those capabilities and in what
sequence/quantity for a given order -- inventory-limit checks, bank stock
checks, and "is this order actually still workable" all live here (or in
scheduler.py), never on Character itself.

Boundary vs scheduler.py: scheduler.py decides WHICH order a character
should be working next (select_order_for/character_eligible); this module
only cares about executing whatever order it's handed.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from events import BankSynced, EquipmentDelivered, EquipmentRequested
from models import find_quantity
from orders import WorkOrder, OrderKind

if TYPE_CHECKING:
    from task_runner import TaskEngine


class Executor:
    """Operates on TaskEngine's shared account/db/order state. See module
    docstring."""

    def __init__(self, engine: "TaskEngine"):
        self.engine = engine

        # Reactive equipment delivery: EquipmentRequested names the exact
        # order that just gained a recipient; BankSynced carries no order
        # id, so its handler sweeps every order with pending
        # equip_requests instead. See ARCHITECTURE.md for the design
        # history. Subscriptions are tracked in self._subscriptions so
        # close() can unsubscribe them all.
        self._subscriptions = [
            (EquipmentRequested, self.engine.bus.subscribe(EquipmentRequested, self._on_equipment_requested)),
            (BankSynced, self.engine.bus.subscribe(BankSynced, self._on_bank_synced)),
        ]

    def close(self) -> None:
        """Unsubscribes every handler this instance registered on
        engine.bus. Called by TaskEngine.stop(). Idempotent --
        EventBus.unsubscribe() is a no-op for an already-removed handler,
        so calling this twice (or on an Executor that failed partway
        through __init__) is harmless."""
        for event_type, handler in self._subscriptions:
            self.engine.bus.unsubscribe(event_type, handler)

    # ------------------------------------------------------------------
    # Task switching
    # ------------------------------------------------------------------

    async def _switch_task(self, character, new_order: Optional[WorkOrder]) -> None:
        engine = self.engine
        old_id = engine._current_order.get(character.name)
        old_order = engine.orders.get(old_id) if old_id else None
        if old_order is new_order:
            return

        if old_order:
            print(f"[{character.name}] Switching off '{old_order.code}' "
                  f"({'idle' if new_order is None else new_order.code} next).")
            engine.scheduler.release(character, old_order)

        # Claim immediately, before any await below, so order.locked_to is
        # set before this coroutine can yield. select_order_for() (sync)
        # relies on locked_to being trustworthy the instant it runs; if the
        # claim happened after an await, two characters could both pass
        # its "not locked" check and end up working the same CRAFT order
        # concurrently.
        if new_order:
            engine.scheduler.claim(character, new_order)

        if old_order and not character.is_inventory_empty:
            # Requirement #4: deposit inventory whenever switching tasks. No
            # need to walk back afterward -- the character is already
            # claimed onto the new order (or idle) above, so returning to
            # the pre-deposit tile would just be an extra trip for nothing.
            await character.deposit_all(return_to_origin=False)
            await engine.account.sync_bank()

    # ------------------------------------------------------------------
    # Equipment delivery
    # ------------------------------------------------------------------

    async def _on_equipment_requested(self, event: EquipmentRequested) -> None:
        """React to a single order gaining a new (character, slot) equip
        request -- targets exactly that order rather than sweeping the
        pool."""
        order = self.engine.orders.get(event.order_id)
        if order is not None and order.equip_requests:
            await self._try_deliver_equipment(order)

    async def _on_bank_synced(self, event: BankSynced) -> None:
        """React to bank contents changing by re-attempting delivery on
        every order that currently has pending equip_requests -- BankSynced
        doesn't say *which* item changed, so this can't narrow further than
        that, but it's still scoped to only the (usually small) subset of
        orders someone is actually waiting on gear from, not every live
        order in engine.orders."""
        for order in list(self.engine.orders.values()):
            if order.equip_requests:
                await self._try_deliver_equipment(order)

    async def _try_deliver_equipment(self, order: WorkOrder, *, already_locked: Optional[str] = None) -> None:
        """Fulfills each queued (character, slot) request in
        order.equip_requests, as bank stock currently allows: move the
        recipient to the bank, unequip and deposit whatever's currently in
        that slot, withdraw one unit of order.code, and equip it. Pops off
        only what it can actually fulfill right now -- anything the bank
        can't cover yet stays queued for the next call.

        Called right after an order completes, by the EquipmentRequested/
        BankSynced subscribers above, and by the delivery safety-sweep
        loop's backstop sweep -- so a recipient who couldn't be served the
        first time still gets theirs once the rest of the order catches up.

        `already_locked` (the calling character's name) lets a
        self-equipping delivery -- the character is both the one who just
        finished the order and one of its equip_requests recipients -- run
        inline instead of re-acquiring its own already-held busy_lock,
        which would deadlock. `order._delivering` is a non-blocking
        re-entrancy guard so two concurrent calls on the same order can't
        both process the same queued request; it's a plain bool rather
        than a lock because blocking here could recreate the same deadlock
        the `already_locked` guard avoids. See ARCHITECTURE.md's
        concurrency-audit section for the full reasoning.
        """
        if order._delivering:
            return
        order._delivering = True
        try:
            await self._deliver_equipment_queue(order, already_locked=already_locked)
        finally:
            order._delivering = False

    async def _deliver_equipment_queue(self, order: WorkOrder, *, already_locked: Optional[str]) -> None:
        """The actual queue-processing loop, split out of
        _try_deliver_equipment so the `_delivering` re-entrancy guard wraps
        it cleanly in a try/finally."""
        engine = self.engine
        map_db = engine.db.maps

        while order.equip_requests:
            bank_qty = find_quantity(engine.account.bank.items, order.code)
            if bank_qty <= 0:
                break

            char_name, slot = order.equip_requests[0]
            requester = engine.account.characters.get(char_name)
            if not requester:
                order.equip_requests.pop(0)
                continue

            if char_name == already_locked:
                # Caller already holds requester.busy_lock -- run inline
                # rather than re-acquiring it.
                old_code = await self._deliver_one(requester, order, slot, map_db)
            else:
                # Serializes this move-unequip-deposit-withdraw-equip
                # sequence against requester's own character_loop
                # iteration, so an independent delivery call (event
                # handler or safety-sweep loop) can't interleave its moves
                # with a gather/craft step the requester's own loop is
                # mid-way through.
                async with requester.busy_lock:
                    old_code = await self._deliver_one(requester, order, slot, map_db)

            if old_code is None:
                # No bank could be resolved -- stop processing this order
                # for now rather than dropping the request; it stays
                # queued for the next call to retry.
                break

            if old_code:
                print(f"[{requester.name}] Unequipped '{old_code}' from {slot} to the bank, "
                      f"then equipped '{order.code}'.")
            else:
                print(f"[{requester.name}] Equipped '{order.code}' in {slot}.")
            order.equip_requests.pop(0)
            self.engine.bus.emit(EquipmentDelivered(
                order_id=order.id, character_name=requester.name, code=order.code, slot=slot,
            ))

    async def _deliver_one(self, requester, order: WorkOrder, slot: str, map_db) -> Optional[str]:
        """Runs the move-unequip-deposit-withdraw-equip sequence for a
        single queued request, assuming the caller already holds (or
        doesn't need) requester.busy_lock -- see
        _deliver_equipment_queue. Returns the old item's code that was
        unequipped, "" if the slot was empty, or None if a bank couldn't
        even be resolved (distinct from "" -- the caller stops processing
        the order rather than treating this as a completed, no-old-item
        delivery)."""
        engine = self.engine
        bank_pos = requester.get_closest_bank(map_db)
        if not bank_pos:
            print(f"[{requester.name}] Could not resolve a bank to deliver '{order.code}'.")
            return None
        await requester.smart_move(bank_pos, map_db=map_db)

        # Unequip whatever's currently in that slot (if anything) and
        # deposit it, so the slot is free before we try to put the new item
        # on -- and the old gear ends up back in the bank rather than just
        # sitting unequipped in inventory. Stacked utility slots track
        # their held quantity in a matching `<slot>_quantity` attribute;
        # other slots don't have one, so getattr falls back to 1 (a single
        # equipped item, the normal case).
        old_code = getattr(requester.equipment, slot, "") or ""
        if old_code:
            old_qty = getattr(requester.equipment, f"{slot}_quantity", 1) or 1
            await requester.unequip(slot, quantity=old_qty)
            await requester._execute_deposit([{"code": old_code, "quantity": old_qty}])

        await requester._execute_withdraw_items([{"code": order.code, "quantity": 1}])
        await engine.account.sync_bank()
        await requester.equip(order.code, slot)
        return old_code

    # ------------------------------------------------------------------
    # Gather step
    # ------------------------------------------------------------------

    async def _run_gather_step(self, character, order: WorkOrder) -> None:
        engine = self.engine
        await character.gather(resource=order.node_code, map_db=engine.db.maps)

        if character.is_inventory_full:
            await character.deposit_all()
            await engine.account.sync_bank()

        if engine.held(order.code) >= order.target_quantity:
            if not character.is_inventory_empty:
                await character.deposit_all()
                await engine.account.sync_bank()
            engine.scheduler.complete(order)
            # already_locked=character.name: character_loop already holds
            # busy_lock for this character, who could be one of
            # order.equip_requests's own recipients (e.g. gathering their
            # own upgrade material) -- see _try_deliver_equipment.
            await self._try_deliver_equipment(order, already_locked=character.name)

    # ------------------------------------------------------------------
    # Craft step
    # ------------------------------------------------------------------

    def _craft_batch_size(self, character, item, crafts_needed: int) -> int:
        """Bounds a craft run to however many actions actually fit in the
        character's inventory AND can actually be supplied by materials
        THIS character can get their hands on. Sizes the batch off free
        inventory space, using the heavier side of (ingredients consumed,
        net items gained) per craft, so a large remaining order (e.g. ore
        for 100 bars) doesn't overflow inventory_max_items either while
        ingredients are held mid-craft or after the output lands.

        Materials availability is checked via scheduler._available_for_craft
        (own inventory + bank only) rather than held() (which also counts
        every other character's inventory) -- a craft can only ever pull
        from the bank or from what's already in this character's hands,
        never from a teammate who hasn't deposited yet. Returns 0 (not a
        forced minimum of 1) if even a single craft's worth isn't actually
        available, so the caller can skip the craft attempt instead of
        issuing an action that's guaranteed to fail with a
        missing-materials error."""
        if not item.craft or not item.craft.items:
            return crafts_needed

        ingredients_per_craft = sum(ing.quantity for ing in item.craft.items)
        net_per_craft = max(ingredients_per_craft, item.craft.quantity)
        if net_per_craft <= 0:
            return crafts_needed

        free_space = max(0, character.inventory_max_items - character.inventory_used)
        max_by_space = free_space // net_per_craft

        max_by_materials = crafts_needed
        for ing in item.craft.items:
            if ing.quantity <= 0:
                continue
            available = self.engine.scheduler._available_for_craft(character, ing.code)
            max_by_materials = min(max_by_materials, available // ing.quantity)

        if max_by_materials <= 0:
            return 0

        # No forced minimum of 1 here -- max_by_space can independently be 0
        # (inventory already full of something else), and forcing a craft
        # attempt through in that case just fails downstream with an
        # inventory-full error instead of cleanly reporting 0 and letting
        # the caller back off.
        return min(crafts_needed, max_by_space, max_by_materials)

    async def _run_craft_step(self, character, order: WorkOrder) -> None:
        engine = self.engine
        remaining = order.target_quantity - engine.held(order.code)
        crafts_needed = max(1, -(-remaining // order.produces_per_action))

        item = engine.db.items.get_item_obj(order.code)
        batch = crafts_needed

        map_db = engine.db.maps
        to_withdraw = []

        if item and item.craft:
            batch = self._craft_batch_size(character, item, crafts_needed)
            if batch <= 0:
                # Nothing craftable right now -- either the select_order_for
                # check that approved this order raced with another
                # character consuming the same ingredients, or (for the
                # `current`-order inertia path) materials just ran out.
                # Release rather than sleep-and-retry: staying claimed here
                # would keep this exact dead order winning via inertia next
                # loop instead of the character picking up other work.
                print(f"[{character.name}] Not enough '{order.code}' ingredients on hand "
                      f"(own inventory + bank) to craft right now; releasing and looking for other work.")
                engine.scheduler.release(character, order)
                return
            if batch < crafts_needed:
                print(f"[{character.name}] Sizing '{order.code}' craft batch to {batch}/{crafts_needed} "
                      f"actions to fit available inventory/materials.")

            for ing in item.craft.items:
                needed_qty = ing.quantity * batch
                have_inv = find_quantity(character.inventory, ing.code)
                shortfall = needed_qty - have_inv
                if shortfall > 0:
                    bank_qty = find_quantity(engine.account.bank.items, ing.code)
                    withdraw_qty = min(shortfall, bank_qty)
                    if withdraw_qty > 0:
                        to_withdraw.append({"code": ing.code, "quantity": withdraw_qty})

        # Chain bank -> workshop -> bank directly (via the private _execute_*
        # methods + explicit smart_move) rather than letting withdraw_items()/
        # craft()/deposit_all() each independently "move there and return to
        # origin" via temporary_relocate, which would add redundant trips
        # back and forth instead of one bank -> workshop -> bank pass.
        if to_withdraw:
            bank_pos = character.get_closest_bank(map_db)
            if bank_pos:
                await character.smart_move(bank_pos, map_db=map_db)
                await character._execute_withdraw_items(to_withdraw)
                await engine.account.sync_bank()
            else:
                print(f"[{character.name}] Could not resolve a bank to withdraw '{order.code}' ingredients.")

        workshop_pos = map_db.find_closest(character, order.skill) if order.skill else None
        if workshop_pos:
            await character.smart_move(workshop_pos, map_db=map_db)
        await character._execute_craft(order.code, batch)

        # Deposit after every crafting batch (not just when the inventory
        # is completely full or the order finishes) so freshly-crafted
        # output -- and any leftover ingredients -- lands in the bank
        # immediately, where other characters/orders relying on it can see
        # it via held().
        if not character.is_inventory_empty:
            bank_pos = character.get_closest_bank(map_db)
            if bank_pos:
                await character.smart_move(bank_pos, map_db=map_db)
                await character._execute_deposit([
                    {"code": i.code, "quantity": i.quantity}
                    for i in character.inventory if i.code and i.quantity > 0
                ])
                await engine.account.sync_bank()

        if engine.held(order.code) >= order.target_quantity:
            engine.scheduler.complete(order)
            # already_locked=character.name -- see the matching comment in
            # _run_gather_step.
            await self._try_deliver_equipment(order, already_locked=character.name)
