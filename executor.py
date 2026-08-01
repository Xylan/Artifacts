#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
executor.py: The business logic for actually carrying out a WorkOrder,
step by step.

Split out of task_runner.py (formerly a "God module"): this file owns HOW
a claimed order gets worked -- switching tasks (with deposit-on-switch),
delivering/equipping finished gear, running one gather action, and running
one craft batch (sized to what the character can actually carry/afford).

Boundary vs CharacterActions.py: CharacterActions stays a library of raw
capabilities (how to talk to the API to move, gather, craft, deposit,
equip, ...). This module is what DECIDES to call those capabilities and in
what sequence/quantity for a given order -- inventory-limit checks, bank
stock checks, and "is this order actually still workable" all live here
(or in scheduler.py), never in CharacterActions.

Boundary vs scheduler.py: scheduler.py decides WHICH order a character
should be working next (select_order_for/character_eligible); this module
only cares about executing whatever order it's handed.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from events import BankSynced, EquipmentDelivered, EquipmentRequested
from orders import WorkOrder, OrderKind

if TYPE_CHECKING:
    from task_runner import TaskEngine


class Executor:
    """Operates on TaskEngine's shared account/db/order state. See module
    docstring."""

    def __init__(self, engine: "TaskEngine"):
        self.engine = engine

        # Reactive equipment delivery (TODO task 7), replacing
        # TaskEngine._delivery_loop's old "scan every order, every tick"
        # poll. Subscribe here (mirroring Scheduler.__init__'s wakeup
        # subscriptions) rather than in TaskEngine, since this module
        # already owns _try_deliver_equipment -- the event handlers below
        # are just "when might a delivery newly be possible?" triggers for
        # that same method:
        #   * EquipmentRequested tells us exactly which order just gained a
        #     recipient, so we can target that one order directly.
        #   * BankSynced doesn't carry an order id (bank contents changed,
        #     but not *which* item) -- the best we can do without over- or
        #     under-scoping is sweep every order that currently has
        #     *pending* equip_requests, which is still "not the whole
        #     pool" (every other live gather/craft order is skipped), just
        #     not narrowed to one order the way EquipmentRequested lets us.
        #
        # Subscriptions are tracked in self._subscriptions (TODO task 12) so
        # close() can unsubscribe them all -- see that method.
        self._subscriptions = [
            (EquipmentRequested, self.engine.bus.subscribe(EquipmentRequested, self._on_equipment_requested)),
            (BankSynced, self.engine.bus.subscribe(BankSynced, self._on_bank_synced)),
        ]

    def close(self) -> None:
        """Unsubscribes every handler this instance registered on
        engine.bus (TODO task 12: subscriber cleanup on TaskEngine.stop()).
        Idempotent -- EventBus.unsubscribe() is a no-op for an
        already-removed handler, so calling this twice (or on an Executor
        that failed partway through __init__) is harmless."""
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

        # Claim immediately, before any await below -- this is what
        # scheduler.select_order_for() relies on to make order.locked_to
        # trustworthy. select_order_for() (sync) and this claim() call
        # happen back-to-back on the same event-loop tick with nothing in
        # between that can yield, so no other character's coroutine can run
        # its own select_order_for() in the gap and see this order as
        # still unclaimed. Claiming used to happen AFTER the deposit await
        # below, which meant two (or more) characters could each pass the
        # "not locked" check in select_order_for() before either of them
        # actually set locked_to -- both would then proceed to work the
        # same CRAFT order concurrently (duplicate withdraws/crafts against
        # the same materials). Claiming up front closes that window.
        if new_order:
            engine.scheduler.claim(character, new_order)

        if old_order and not character.is_inventory_empty:
            # Requirement #4: deposit inventory whenever switching tasks. No
            # need to walk back afterward -- the character is already
            # claimed onto the new order (or idle) above, so returning to
            # the pre-deposit tile would just be an extra trip for nothing.
            await character.actions.deposit_all(return_to_origin=False)
            await engine.account.sync_bank()

    # ------------------------------------------------------------------
    # Equipment delivery
    # ------------------------------------------------------------------

    async def _on_equipment_requested(self, event: EquipmentRequested) -> None:
        """React to a single order gaining a new (character, slot) equip
        request -- targets exactly that order rather than sweeping the
        pool, per TODO task 7."""
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
        order.equip_requests, as bank stock currently allows, by sending
        the recipient to the bank to swap gear there -- NOT by pushing the
        item out to wherever the character happens to be. For each request
        this: moves the character to the bank, unequips whatever's
        currently in that slot (if anything) and deposits it, withdraws one
        unit of order.code, and equips it. Pops off only what it can
        actually fulfill right now -- if the bank doesn't have enough for
        everyone yet, whatever's left stays queued for the next call rather
        than being dropped.

        Doing the unequip-deposit-withdraw-equip sequence at the bank (in
        that order) matters: equipping straight over an occupied slot
        previously failed outright, silently stranding the new item in the
        character's inventory (and, since something eventually deposits
        that inventory, right back in the bank) instead of ever getting
        worn -- gear could sit there indefinitely even though the craft/
        gather order that produced it looked complete.

        Called right after an order completes (best-effort, immediate,
        directly from _run_gather_step/_run_craft_step), by the
        EquipmentRequested/BankSynced subscribers above (TODO task 7 --
        reactively, whenever a new recipient queues up or bank contents
        change), and by TaskEngine._delivery_safety_sweep_loop's much-lower-
        frequency backstop sweep -- so recipients who couldn't be served
        the first time (e.g. only 1 of 5 requested copies had actually
        landed in the bank yet) still get theirs once the rest of the
        order's crafting/gathering catches up.

        Concurrency (TODO task 12 audit): those four call sites can now
        legitimately race on the SAME order at effectively the same
        instant, which raises two hazards the pre-event-bus single-caller
        design never had to deal with:

        1. Two concurrent calls both reading order.equip_requests[0] before
           either has popped it would double-deliver the same request (two
           withdraws/equips for one queued unit). `order._delivering`
           (orders.WorkOrder) guards against this: it's a plain bool, set
           True for the duration of the actual queue-processing loop below
           and checked at entry with no `await` in between the check and
           the set, so it's atomic on the single-threaded event loop -- a
           losing concurrent call sees True and returns immediately rather
           than reprocessing the queue. Deliberately NOT an asyncio.Lock:
           see point 2 for why blocking here would be dangerous.
        2. The direct call from _run_gather_step/_run_craft_step happens
           while character_loop's `async with character.busy_lock` is
           already held for the character that just finished the order --
           if that same character is also one of order.equip_requests's
           recipients (self-equipping: gathering/crafting their own
           upgrade), this method must NOT try to `async with
           requester.busy_lock` for that entry, since asyncio.Lock isn't
           reentrant and the calling task already holds it (instant
           self-deadlock). `already_locked` (the calling character's name,
           passed by _run_gather_step/_run_craft_step) lets the matching
           queue entry skip the redundant acquire and run inline instead.
           This is also why point 1's guard is a non-blocking bool rather
           than an awaited lock: if a reactive call (which does NOT hold
           any busy_lock) blocked waiting for a delivery-in-progress flag
           held by a direct call that is itself waiting on a *different*
           requester's busy_lock, and that requester's own character_loop
           was in turn waiting on this same order's delivery flag, blocking
           would create exactly the reentrant-deadlock cycle this audit set
           out to catch. A losing call returning immediately instead means
           no call here ever waits on anything but a single requester's
           busy_lock -- never on another call finishing.
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
        _try_deliver_equipment so the `_delivering` re-entrancy guard (TODO
        task 12) wraps it cleanly in a try/finally."""
        engine = self.engine
        map_db = engine.db.maps

        while order.equip_requests:
            bank_qty = next((i.quantity for i in engine.account.bank.items if i.code == order.code), 0)
            if bank_qty <= 0:
                break

            char_name, slot = order.equip_requests[0]
            requester = engine.account.characters.get(char_name)
            if not requester:
                order.equip_requests.pop(0)
                continue

            if char_name == already_locked:
                # Caller already holds requester.busy_lock (see
                # _try_deliver_equipment's docstring, point 2) -- run
                # inline rather than re-acquiring it.
                old_code = await self._deliver_one(requester, order, slot, map_db)
            else:
                # busy_lock serializes this whole move-unequip-deposit-
                # withdraw-equip sequence against `requester`'s own
                # character_loop iteration (see Character.busy_lock) --
                # without it, this delivery (running from an independent
                # event-handler task or the safety-sweep loop) could
                # interleave its moves with a gather/craft step the
                # requester's own loop is mid-way through, leaving them
                # standing somewhere neither side expects (spurious
                # 598/490 errors).
                async with requester.busy_lock:
                    old_code = await self._deliver_one(requester, order, slot, map_db)

            if old_code is None:
                # _deliver_one couldn't even resolve a bank to deliver at --
                # stop processing this order for now (mirrors the original
                # pre-task-12 `break`) rather than popping/dropping the
                # request; it stays queued for the next call to retry.
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
        bank_pos = requester.actions.get_closest_bank(map_db)
        if not bank_pos:
            print(f"[{requester.name}] Could not resolve a bank to deliver '{order.code}'.")
            return None
        await requester.actions.smart_move(bank_pos, map_db=map_db)

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
            await requester.actions.unequip(slot, quantity=old_qty)
            await requester.actions._execute_deposit([{"code": old_code, "quantity": old_qty}])

        await requester.actions._execute_withdraw_items([{"code": order.code, "quantity": 1}])
        await engine.account.sync_bank()
        await requester.actions.equip(order.code, slot)
        return old_code

    # ------------------------------------------------------------------
    # Gather step
    # ------------------------------------------------------------------

    async def _run_gather_step(self, character, order: WorkOrder) -> None:
        engine = self.engine
        await character.actions.gather(resource=order.node_code, map_db=engine.db.maps)

        if character.is_inventory_full:
            await character.actions.deposit_all()
            await engine.account.sync_bank()

        if engine.held(order.code) >= order.target_quantity:
            if not character.is_inventory_empty:
                await character.actions.deposit_all()
                await engine.account.sync_bank()
            engine.scheduler.complete(order)
            # already_locked=character.name (TODO task 12): character_loop
            # already holds character.busy_lock for the whole switch+act
            # cycle this call is part of -- see _try_deliver_equipment's
            # docstring, point 2, for why this must NOT be omitted (this
            # character could be one of order.equip_requests's own
            # recipients, e.g. gathering their own upgrade material).
            await self._try_deliver_equipment(order, already_locked=character.name)

    # ------------------------------------------------------------------
    # Craft step
    # ------------------------------------------------------------------

    def _craft_batch_size(self, character, item, crafts_needed: int) -> int:
        """Bounds a craft run to however many actions actually fit in the
        character's inventory AND can actually be supplied by materials
        THIS character can get their hands on. Withdrawing ingredients for
        the *entire* remaining order (e.g. ore for 100 bars) in one shot is
        what was blowing past inventory_max_items and failing the
        withdraw/craft -- this sizes the batch off free inventory space,
        using the heavier side of (ingredients consumed, net items gained)
        per craft so we don't overflow either while ingredients are held
        mid-craft or after the output lands.

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
                have_inv = next((i.quantity for i in character.inventory if i.code == ing.code), 0)
                shortfall = needed_qty - have_inv
                if shortfall > 0:
                    bank_qty = next((i.quantity for i in engine.account.bank.items if i.code == ing.code), 0)
                    withdraw_qty = min(shortfall, bank_qty)
                    if withdraw_qty > 0:
                        to_withdraw.append({"code": ing.code, "quantity": withdraw_qty})

        # Chain bank -> workshop -> bank directly (via the private _execute_*
        # methods + explicit smart_move) instead of letting withdraw_items()/
        # craft()/deposit_all() each independently "move there and return to
        # origin" via temporary_relocate. That was producing two full round
        # trips per batch -- workshop -> bank -> workshop (withdraw's own
        # return) -> workshop (craft, a no-op move) -> bank -> workshop
        # (deposit's own return) -- instead of one bank -> workshop -> bank
        # pass.
        if to_withdraw:
            bank_pos = character.actions.get_closest_bank(map_db)
            if bank_pos:
                await character.actions.smart_move(bank_pos, map_db=map_db)
                await character.actions._execute_withdraw_items(to_withdraw)
                await engine.account.sync_bank()
            else:
                print(f"[{character.name}] Could not resolve a bank to withdraw '{order.code}' ingredients.")

        workshop_pos = map_db.find_closest(character, order.skill) if order.skill else None
        if workshop_pos:
            await character.actions.smart_move(workshop_pos, map_db=map_db)
        await character.actions._execute_craft(order.code, batch)

        # Deposit after every crafting batch (not just when the inventory
        # is completely full or the order finishes) so freshly-crafted
        # output -- and any leftover ingredients -- lands in the bank
        # immediately, where other characters/orders relying on it can see
        # it via held().
        if not character.is_inventory_empty:
            bank_pos = character.actions.get_closest_bank(map_db)
            if bank_pos:
                await character.actions.smart_move(bank_pos, map_db=map_db)
                await character.actions._execute_deposit([
                    {"code": i.code, "quantity": i.quantity}
                    for i in character.inventory if i.code and i.quantity > 0
                ])
                await engine.account.sync_bank()

        if engine.held(order.code) >= order.target_quantity:
            engine.scheduler.complete(order)
            # already_locked=character.name -- see the matching comment in
            # _run_gather_step (TODO task 12).
            await self._try_deliver_equipment(order, already_locked=character.name)
