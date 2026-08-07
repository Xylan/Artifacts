#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scheduler.py: Matches eligible characters to open orders, and drives the
per-character loop.

Answers "who is allowed to work this order, how good a fit are they, and
which order should this character be doing right now" -- character_eligible,
_score, select_order_for -- plus the claim/release/complete lifecycle that
keeps WorkOrder.locked_to/claimed_by trustworthy, and character_loop, the
actual infinite per-character coroutine that ties select -> switch -> act
together.

It deliberately does NOT contain the mechanics of *how* a gather or craft
action is carried out (that's executor.py) or how orders are created/sized
(that's order_manager.py) -- see task_runner.py's module docstring for the
full boundary description. See ARCHITECTURE.md for the design history
behind the module split and the event-driven idle-wakeup design below.
"""
from __future__ import annotations

import asyncio
from typing import Optional, TYPE_CHECKING

from events import OrderCreated, OrderUpdated, OrderClaimed, OrderReleased, OrderCompleted
from orders import WorkOrder, OrderKind, Priority, INERTIA_BONUS
from roles import PURE_CRAFT_SKILLS, GATHER_SKILLS, CRAFT_ALLOWANCE_LEVEL, primary_owner_of, gather_rank

if TYPE_CHECKING:
    from task_runner import TaskEngine


class Scheduler:
    """Operates on TaskEngine's shared order pool/character roster. See
    module docstring."""

    # Safety-net multiplier on engine.poll_interval for character_loop's
    # idle wait: bounds how long a missed/mis-targeted wakeup can strand an
    # idle character. The event-driven wakeups below (_on_order_created/
    # _on_order_updated/_on_order_released/_on_order_completed) are the
    # primary wake path; this is only the fallback.
    IDLE_WAIT_FALLBACK_MULTIPLIER = 10

    def __init__(self, engine: "TaskEngine"):
        self.engine = engine

        # Subscribe at construction time so every order-pool change that
        # could plausibly free up work for an idle character reaches
        # character_loop's wait() below. Tracked in self._subscriptions so
        # close() can unsubscribe them all.
        self._subscriptions = [
            (OrderCreated, self.engine.bus.subscribe(OrderCreated, self._on_order_created)),
            (OrderUpdated, self.engine.bus.subscribe(OrderUpdated, self._on_order_updated)),
            (OrderReleased, self.engine.bus.subscribe(OrderReleased, self._on_order_released)),
            (OrderCompleted, self.engine.bus.subscribe(OrderCompleted, self._on_order_completed)),
        ]

    def close(self) -> None:
        """Unsubscribes every handler this instance registered on
        engine.bus. Idempotent -- called by TaskEngine.stop()."""
        for event_type, handler in self._subscriptions:
            self.engine.bus.unsubscribe(event_type, handler)

    # ------------------------------------------------------------------
    # Eligibility / scoring
    # ------------------------------------------------------------------

    def _craft_allowed(self, character, skill: str) -> bool:
        engine = self.engine
        role = engine.roles.get(character.name)
        if role and role.primary_craft == skill:
            return True
        if skill not in PURE_CRAFT_SKILLS:
            return True  # refining skills (mining/woodcutting/alchemy-as-craft) are open to all
        owner_name = primary_owner_of(skill, engine.roles)
        if owner_name is None:
            return True  # nobody claims this skill -- open to whoever qualifies
        owner = engine.account.characters.get(owner_name)
        owner_level = getattr(owner.skills, f"{skill}_level", 0) if owner else 0
        return owner_level >= CRAFT_ALLOWANCE_LEVEL

    def character_eligible(self, character, order: WorkOrder) -> bool:
        if order.done:
            return False
        if order.only_for and order.only_for != character.name:
            return False

        if order.kind == OrderKind.CRAFT:
            if order.locked_to not in (None, character.name):
                return False
            char_level = getattr(character.skills, f"{order.skill}_level", 0)
            if char_level < order.skill_level:
                return False
            return self._craft_allowed(character, order.skill)

        char_level = getattr(character.skills, f"{order.skill}_level", 0)
        return char_level >= order.skill_level

    def _score(self, character, order: WorkOrder) -> float:
        score = float(order.base_priority)
        if order.skill in GATHER_SKILLS:
            # Cascading personal preference is a tie-breaker only -- a
            # fraction of a priority point, never enough to outrank a
            # genuinely higher-priority order.
            score -= 0.1 * gather_rank(character.name, order.skill, self.engine.roles)
        return score

    def _available_for_craft(self, character, code: str) -> int:
        """Amount of `code` `character` can actually draw on for crafting
        right now: what's already sitting in their own inventory, plus
        whatever's in the bank (withdrawable by anyone). Deliberately
        excludes every OTHER character's inventory -- held() counts those
        too, but this character has no way to pull materials out of a
        teammate's hands, only out of the bank once that teammate deposits."""
        own_inventory = next((i.quantity for i in character.inventory if i.code == code), 0)
        bank_qty = next((i.quantity for i in self.engine.account.bank.items if i.code == code), 0)
        return own_inventory + bank_qty

    def _materials_available(self, character, order: WorkOrder) -> bool:
        """Requirement #1: only assign a craft order to `character` once
        every ingredient is currently available to THEM specifically --
        their own inventory plus the bank, not the roster's total holdings."""
        item = self.engine.db.items.get_item_obj(order.code)
        if not item or not item.craft:
            return True
        return all(
            self._available_for_craft(character, ing.code) >= ing.quantity
            for ing in item.craft.items
        )

    def select_order_for(self, character) -> Optional[WorkOrder]:
        engine = self.engine
        current_id = engine._current_order.get(character.name)
        current = engine.orders.get(current_id) if current_id else None

        best, best_score = None, float("-inf")

        # Inertia only applies while `current` is still actually workable:
        # character_eligible alone only checks skill level/lock ownership,
        # not live material stock, so a craft order whose ingredients have
        # run dry needs this extra check too or the character would stay
        # locked onto it via inertia with nothing to actually do.
        current_still_workable = (
            current is not None
            and not current.done
            and self.character_eligible(character, current)
            and current.target_quantity > engine.held(current.code)
            and (current.kind != OrderKind.CRAFT or self._materials_available(character, current))
        )
        if current_still_workable:
            inertia = 0 if current.priority == Priority.DEFAULT else INERTIA_BONUS
            best, best_score = current, self._score(character, current) + inertia

        for order in engine.orders.values():
            if order is current or not self.character_eligible(character, order):
                continue
            if order.target_quantity <= engine.held(order.code):
                continue  # already satisfied, nothing left to do
            if order.kind == OrderKind.CRAFT and not self._materials_available(character, order):
                continue
            score = self._score(character, order)
            if score > best_score:
                best, best_score = order, score

        return best

    # ------------------------------------------------------------------
    # Claim / release / complete (requirement #2: "never hold a task
    # you aren't currently performing")
    # ------------------------------------------------------------------

    def claim(self, character, order: WorkOrder) -> None:
        if order.kind == OrderKind.CRAFT:
            order.locked_to = character.name
        order.claimed_by.add(character.name)
        self.engine._current_order[character.name] = order.id
        self.engine.bus.emit(OrderClaimed(order_id=order.id, character_name=character.name))

    def release(self, character, order: WorkOrder) -> None:
        order.claimed_by.discard(character.name)
        if order.kind == OrderKind.CRAFT and order.locked_to == character.name:
            order.locked_to = None
        if self.engine._current_order.get(character.name) == order.id:
            self.engine._current_order[character.name] = None
        self.engine.bus.emit(OrderReleased(order_id=order.id, character_name=character.name))

    def complete(self, order: WorkOrder) -> None:
        order.done = True
        order.claimed_by.clear()
        order.locked_to = None
        self.engine.bus.emit(OrderCompleted(order_id=order.id, code=order.code))

    # ------------------------------------------------------------------
    # Event-driven idle wakeups
    # ------------------------------------------------------------------
    # Whenever the order pool changes in a way that could plausibly give an
    # idle character something to do, these subscribers set() the relevant
    # character(s)' work_available Event, so character_loop's
    # `await character.work_available.wait()` returns immediately instead
    # of waiting out the fallback timeout.

    def _wake_eligible(self, order: WorkOrder) -> None:
        """Sets work_available for every roster character currently
        eligible for `order` (same check select_order_for/claim already
        use). Safe to call for a character who's currently busy, not just
        idle ones -- Event.set() on someone mid-action just means their
        *next* idle wait resolves immediately, which costs one extra (cheap)
        select_order_for() call, not a correctness problem. That's also why
        this doesn't bother tracking who's actually idle right now."""
        for character in self.engine.account.characters.values():
            if self.character_eligible(character, order):
                character.work_available.set()

    def _wake_all(self) -> None:
        """Wakes every roster character. Used where the set of characters
        actually unblocked is cheaper to over-approximate than compute
        precisely -- e.g. OrderCompleted can free bank materials that make
        some other order's _materials_available() true for characters that
        have nothing to do with the order that just finished."""
        for character in self.engine.account.characters.values():
            character.work_available.set()

    def _on_order_created(self, event: OrderCreated) -> None:
        order = self.engine.orders.get(event.order_id)
        if order is not None:
            self._wake_eligible(order)

    def _on_order_updated(self, event: OrderUpdated) -> None:
        order = self.engine.orders.get(event.order_id)
        if order is not None:
            self._wake_eligible(order)

    def _on_order_released(self, event: OrderReleased) -> None:
        # A released order is available for someone else to pick up again --
        # same targeting as OrderCreated/OrderUpdated.
        order = self.engine.orders.get(event.order_id)
        if order is not None:
            self._wake_eligible(order)

    def _on_order_completed(self, event: OrderCompleted) -> None:
        self._wake_all()

    # ------------------------------------------------------------------
    # The per-character live loop
    # ------------------------------------------------------------------

    async def character_loop(self, character) -> None:
        engine = self.engine
        while engine.running:
            order = self.select_order_for(character)

            # Holds busy_lock for the whole switch+act cycle so that a
            # concurrent _try_deliver_equipment() call targeting this same
            # character (from a separate task) can't interleave its own
            # moves in between this loop's move-then-act steps. See
            # Character.busy_lock for why this can't just reuse action_lock.
            async with character.busy_lock:
                await engine.executor._switch_task(character, order)

                if order is None:
                    do_sleep = True
                else:
                    do_sleep = False
                    try:
                        if order.kind == OrderKind.GATHER:
                            await engine.executor._run_gather_step(character, order)
                        else:
                            await engine.executor._run_craft_step(character, order)
                    except Exception as e:
                        print(f"[{character.name}] Error running order #{order.id} '{order.code}': {e!r}")
                        do_sleep = True

            if do_sleep:
                # Waits on work_available instead of unconditionally
                # sleeping poll_interval. The Scheduler subscribers above
                # set() this character's work_available whenever the order
                # pool changes in a way that could give them something to
                # do, so this normally returns as soon as real work appears.
                # `wait_for`'s timeout is a fallback against a missed or
                # mis-targeted wakeup, so a character is never stranded idle
                # forever, only delayed up to the fallback window.
                try:
                    await asyncio.wait_for(
                        character.work_available.wait(),
                        timeout=engine.poll_interval * self.IDLE_WAIT_FALLBACK_MULTIPLIER,
                    )
                except asyncio.TimeoutError:
                    pass
                character.work_available.clear()
