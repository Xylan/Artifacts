#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
task_runner.py: Dynamic, priority-interrupt task scheduler for the character
roster.

This is the *live* driver of the roster, complementary to planning.py rather
than a replacement for it: planning.GearPlan/PlanRunner still resolve a
one-off wishlist into a static DAG and run it to completion. TaskEngine
instead owns a shared pool of WorkOrders (orders.py) that is continuously
updated -- orders get created (request_item), reprioritized, locked/
released, and completed while characters are already mid-run -- and every
character repeatedly asks "what's the best thing I could be doing right
now?" at well-defined breakpoints.

Priority tiers (orders.Priority): CRAFT > GATHER > KEEP_STOCK > DEFAULT.
See orders.INERTIA_BONUS for the anti-thrashing bias applied to whatever
order a character is currently working; DEFAULT-tier orders get none, so
they're preempted immediately by anything else (zero inertia).

Breakpoints: a gathering character only re-evaluates for a *different*
order right after a bank deposit (inventory-full flush, or its target being
reached) -- never mid gather-action, since a single gather() call is atomic
and can't be interrupted anyway. A crafting character re-evaluates between
craft batches for the same reason. This is what satisfies "gathering should
not be interrupted immediately mid-action; check for new crafting tasks at
natural breakpoints, such as when the character visits the bank."
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional

from account import Account
from database import GameDatabase
from orders import WorkOrder, OrderKind, Priority, INERTIA_BONUS
from roles import (
    CharacterRole, DEFAULT_ROLES, PURE_CRAFT_SKILLS, GATHER_SKILLS,
    CRAFT_ALLOWANCE_LEVEL, primary_owner_of, gather_rank,
)


@dataclass
class StockRule:
    """'Always have at least `minimum` of `code` across bank + inventories.'
    Generates the lowest-priority (KEEP_STOCK) orders once holdings dip
    below the threshold -- see TaskEngine.refresh_stock_orders()."""
    code: str
    minimum: int


class TaskEngine:
    """Owns the live WorkOrder pool and one scheduling loop per character."""

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

    # ------------------------------------------------------------------
    # Holdings
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

    # ------------------------------------------------------------------
    # Order expansion (craft/gather dependency resolution)
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
        this is what request_upgrades_for()/request_equipment() use, so a
        gear request's own craft step outranks its ingredient gather steps,
        per the Crafting > Gathering rule. Passing tier=Priority.KEEP_STOCK
        (or DEFAULT) instead forces that single tier across the whole
        expansion, so a keep-in-stock request never jumps ahead of a
        genuine equipment/gather request even if it happens to be craftable.

        Returns the id of the top-level order for `code`, or None if `code`
        is neither craftable nor gatherable (buy it manually). Safe to call
        repeatedly -- an existing live order for the same code has its
        target bumped in place rather than being duplicated.
        """
        if quantity <= 0:
            return None

        existing = self._order_for_code(code)
        if existing:
            existing.target_quantity += quantity
            if existing.kind == OrderKind.CRAFT:
                self._bump_ingredients(existing, quantity, tier)
            if requester and not existing.requester:
                existing.requester = requester
                existing.equip_slot = equip_slot
            return existing.id

        item = self.db.items.get_item_obj(code)
        if item and item.craft:
            produces = max(1, item.craft.quantity)
            crafts_needed = -(-quantity // produces)  # ceil
            order = WorkOrder(
                kind=OrderKind.CRAFT,
                priority=tier if tier is not None else Priority.CRAFT,
                code=code, node_code=item.craft.skill, skill=item.craft.skill,
                skill_level=item.craft.level, target_quantity=quantity,
                produces_per_action=produces, parent_id=parent_id,
                requester=requester, equip_slot=equip_slot,
            )
            self.orders[order.id] = order
            for ing in item.craft.items:
                self.request_item(ing.code, ing.quantity * crafts_needed, tier=tier, parent_id=order.id)
            return order.id

        resource = self.db.resources.find_best_for_item(code)
        if resource:
            drop = next((d for d in resource["drops"] if d["code"] == code), {})
            avg_yield = max(1, (drop.get("min_quantity", 1) + drop.get("max_quantity", 1)) / 2)
            order = WorkOrder(
                kind=OrderKind.GATHER,
                priority=tier if tier is not None else Priority.GATHER,
                code=code, node_code=resource["code"], skill=resource["skill"],
                skill_level=resource["level"], target_quantity=quantity,
                produces_per_action=int(avg_yield), parent_id=parent_id,
                requester=requester, equip_slot=equip_slot,
            )
            self.orders[order.id] = order
            return order.id

        print(f"[TaskEngine] '{code}' is neither craftable nor gatherable -- skipping (buy/GE manually).")
        return None

    def _bump_ingredients(self, craft_order: WorkOrder, extra_output: int, tier: Optional[Priority]) -> None:
        item = self.db.items.get_item_obj(craft_order.code)
        if not item or not item.craft:
            return
        extra_crafts = -(-extra_output // craft_order.produces_per_action)
        for ing in item.craft.items:
            self.request_item(ing.code, ing.quantity * extra_crafts, tier=tier, parent_id=craft_order.id)

    def request_equipment(self, character_name: str, code: str, slot: str, quantity: int = 1) -> Optional[int]:
        """Requests `code` on behalf of `character_name`, who wants it
        delivered from the bank and equipped once the order completes --
        see _try_deliver_equipment()."""
        return self.request_item(code, quantity, requester=character_name, equip_slot=slot)

    def request_upgrades_for(self, character) -> List[int]:
        """Wraps planning.GearList.for_upgrades: auto-detects every
        craftable armor/weapon upgrade for `character` and requests it with
        equip-on-completion wired up (requirement #4)."""
        from planning import GearList
        gear_list = GearList.for_upgrades(character, self.db)
        ids = []
        for code, qty in gear_list.wants.items():
            item = self.db.items.get_item_obj(code)
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
        self.stock_rules.append(StockRule(code=code, minimum=minimum))

    def refresh_stock_orders(self) -> None:
        """Bottom-of-the-barrel: only tops up what isn't already covered by
        a live order for that code, and always at KEEP_STOCK tier so it
        never outranks a genuine gather/craft request. Call periodically
        (e.g. once per run() loop or on a timer) as holdings drift."""
        for rule in self.stock_rules:
            if self.held(rule.code) - rule.minimum >= 0:
                continue
            if self._order_for_code(rule.code):
                continue  # already being produced, whatever tier that order is at
            shortfall = rule.minimum - self.held(rule.code)
            self.request_item(rule.code, shortfall, tier=Priority.KEEP_STOCK)

    # ------------------------------------------------------------------
    # Default tasks (requirement #5: zero inertia, lowest priority)
    # ------------------------------------------------------------------

    def set_default_gather_task(self, character_name: str, resource_code: str) -> Optional[WorkOrder]:
        """Registers a fallback gather order for `character_name`, used only
        when nothing else is claimable for them. Effectively never
        "completes" (huge target) -- it's busywork, not a real goal."""
        resource = self.db.resources.get_resource(resource_code)
        if not resource:
            print(f"[TaskEngine] Unknown resource '{resource_code}' for default task.")
            return None
        drop = resource["drops"][0] if resource.get("drops") else {"code": resource_code}
        order = WorkOrder(
            kind=OrderKind.GATHER, priority=Priority.DEFAULT,
            code=drop["code"], node_code=resource["code"], skill=resource["skill"],
            skill_level=resource["level"], target_quantity=10 ** 9, produces_per_action=1,
            only_for=character_name,
        )
        self.orders[order.id] = order
        self.default_orders[character_name] = order
        return order

    def assign_default_gather_tasks(self) -> None:
        """Requirement #5/#3: gives every character without an explicit
        default order (set_default_gather_task not already called for
        them) a fallback gather task, so nobody sits fully idle when
        nothing else is claimable. Picks the lowest-level resource for the
        first skill in the character's role.gather_priority cascade that
        they currently meet the level requirement for -- e.g. if a
        character's list is [mining, woodcutting, fishing, alchemy] and
        they're not yet high enough level for any mining node, we fall
        through to woodcutting, etc. Safe to call repeatedly; characters
        that already have a default order (explicit or previously
        assigned here) are left untouched."""
        for name, character in self.account.characters.items():
            if name in self.default_orders:
                continue

            role = self.roles.get(name)
            priorities = role.gather_priority if role else GATHER_SKILLS

            chosen_resource = None
            for skill in priorities:
                char_level = getattr(character.skills, f"{skill}_level", 0)
                candidates = self.db.resources.get_by_skill(skill, max_level=char_level)
                if candidates:
                    chosen_resource = min(candidates, key=lambda r: r.get("level", 1))
                    break

            if not chosen_resource:
                print(f"[TaskEngine] No eligible default gather resource found for '{name}'.")
                continue

            self.set_default_gather_task(name, chosen_resource["code"])
            print(f"[TaskEngine] Default gather task for '{name}': '{chosen_resource['code']}' "
                  f"(skill={chosen_resource.get('skill')}).")

    # ------------------------------------------------------------------
    # Eligibility / scoring
    # ------------------------------------------------------------------

    def _craft_allowed(self, character, skill: str) -> bool:
        role = self.roles.get(character.name)
        if role and role.primary_craft == skill:
            return True
        if skill not in PURE_CRAFT_SKILLS:
            return True  # refining skills (mining/woodcutting/alchemy-as-craft) are open to all
        owner_name = primary_owner_of(skill, self.roles)
        if owner_name is None:
            return True  # nobody claims this skill -- open to whoever qualifies
        owner = self.account.characters.get(owner_name)
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
            score -= 0.1 * gather_rank(character.name, order.skill, self.roles)
        return score

    def _materials_available(self, order: WorkOrder) -> bool:
        """Requirement #1: only assign a craft order once every ingredient
        is currently available (bank + all inventories combined)."""
        item = self.db.items.get_item_obj(order.code)
        if not item or not item.craft:
            return True
        return all(self.held(ing.code) >= ing.quantity for ing in item.craft.items)

    def select_order_for(self, character) -> Optional[WorkOrder]:
        current_id = self._current_order.get(character.name)
        current = self.orders.get(current_id) if current_id else None

        best, best_score = None, float("-inf")

        if current and not current.done and self.character_eligible(character, current):
            inertia = 0 if current.priority == Priority.DEFAULT else INERTIA_BONUS
            best, best_score = current, self._score(character, current) + inertia

        for order in self.orders.values():
            if order is current or not self.character_eligible(character, order):
                continue
            if order.target_quantity <= self.held(order.code):
                continue  # already satisfied, nothing left to do
            if order.kind == OrderKind.CRAFT and not self._materials_available(order):
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
        self._current_order[character.name] = order.id

    def release(self, character, order: WorkOrder) -> None:
        order.claimed_by.discard(character.name)
        if order.kind == OrderKind.CRAFT and order.locked_to == character.name:
            order.locked_to = None
        if self._current_order.get(character.name) == order.id:
            self._current_order[character.name] = None

    def complete(self, order: WorkOrder) -> None:
        order.done = True
        order.claimed_by.clear()
        order.locked_to = None

    # ------------------------------------------------------------------
    # Plan verification (requirement #5)
    # ------------------------------------------------------------------

    def verify(self) -> bool:
        """Sanity-checks that every live CRAFT order's ingredient orders
        were sized to actually cover its target_quantity -- catches
        expansion bugs before characters start burning cooldowns on a plan
        that can never finish."""
        ok = True
        for order in self.orders.values():
            if order.kind != OrderKind.CRAFT or order.done:
                continue
            item = self.db.items.get_item_obj(order.code)
            if not item or not item.craft:
                continue
            crafts_needed = -(-order.target_quantity // order.produces_per_action)
            for ing in item.craft.items:
                needed = ing.quantity * crafts_needed
                ing_order = self._order_for_code(ing.code)
                covered = (ing_order.target_quantity if ing_order else 0) + self.held(ing.code)
                if covered < needed:
                    print(f"[TaskEngine] VERIFY FAIL: '{order.code}' needs {needed}x '{ing.code}' "
                          f"but only {covered} planned/held.")
                    ok = False
        print(f"[TaskEngine] Plan verification {'passed' if ok else 'FAILED'} "
              f"({len(self.orders)} live orders).")
        return ok

    def print_plan_tree(self) -> None:
        roots = [o for o in self.orders.values() if o.parent_id is None]

        def _print(order: WorkOrder, depth: int):
            tag = "DONE" if order.done else f"{order.kind.name}/{order.priority.name}"
            who = order.locked_to or (",".join(order.claimed_by) or "-")
            print(f"{'  ' * depth}- [{tag}] {order.code} x{order.target_quantity} "
                  f"(skill={order.skill or '-'}) -> {who}")
            for child in self.orders.values():
                if child.parent_id == order.id:
                    _print(child, depth + 1)

        for root in roots:
            _print(root, 0)

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _switch_task(self, character, new_order: Optional[WorkOrder]) -> None:
        old_id = self._current_order.get(character.name)
        old_order = self.orders.get(old_id) if old_id else None
        if old_order is new_order:
            return

        if old_order:
            print(f"[{character.name}] Switching off '{old_order.code}' "
                  f"({'idle' if new_order is None else new_order.code} next).")
            self.release(character, old_order)
            # Requirement #4: deposit inventory whenever switching tasks.
            if not character.is_inventory_empty:
                await character.actions.deposit_all()
                await self.account.sync_bank()

        if new_order:
            self.claim(character, new_order)

    async def _try_deliver_equipment(self, character, order: WorkOrder) -> None:
        """Once a requested item is fully produced, hand it to the
        requester and equip it (requirement #4)."""
        if not order.requester or not order.equip_slot:
            return
        requester = self.account.characters.get(order.requester)
        if not requester:
            return
        await requester.actions.withdraw_items([{"code": order.code, "quantity": 1}])
        await requester.actions.equip(order.code, order.equip_slot)
        print(f"[{requester.name}] Equipped '{order.code}' in {order.equip_slot}.")

    async def _run_gather_step(self, character, order: WorkOrder) -> None:
        await character.actions.gather(resource=order.node_code, map_db=self.db.maps)

        if character.is_inventory_full:
            await character.actions.deposit_all()
            await self.account.sync_bank()

        if self.held(order.code) >= order.target_quantity:
            if not character.is_inventory_empty:
                await character.actions.deposit_all()
                await self.account.sync_bank()
            self.complete(order)
            await self._try_deliver_equipment(character, order)

    def _craft_batch_size(self, character, item, crafts_needed: int) -> int:
        """Bounds a craft run to however many actions actually fit in the
        character's inventory AND can actually be supplied by materials on
        hand. Withdrawing ingredients for the *entire* remaining order
        (e.g. ore for 100 bars) in one shot is what was blowing past
        inventory_max_items and failing the withdraw/craft -- this sizes
        the batch off free inventory space, using the heavier side of
        (ingredients consumed, net items gained) per craft so we don't
        overflow either while ingredients are held mid-craft or after the
        output lands. It also caps by held(ingredient) // needed_per_craft
        for every ingredient, so we never plan a batch bigger than what's
        actually available across bank + all inventories combined --
        without this, a craft call could be issued for a quantity we don't
        have the materials for and fail outright."""
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
            available = self.held(ing.code)
            max_by_materials = min(max_by_materials, available // ing.quantity)

        return max(1, min(crafts_needed, max_by_space, max_by_materials))

    async def _run_craft_step(self, character, order: WorkOrder) -> None:
        remaining = order.target_quantity - self.held(order.code)
        crafts_needed = max(1, -(-remaining // order.produces_per_action))

        item = self.db.items.get_item_obj(order.code)
        batch = crafts_needed

        if item and item.craft:
            batch = self._craft_batch_size(character, item, crafts_needed)
            if batch < crafts_needed:
                print(f"[{character.name}] Sizing '{order.code}' craft batch to {batch}/{crafts_needed} "
                      f"actions to fit available inventory space.")

            to_withdraw = []
            for ing in item.craft.items:
                needed_qty = ing.quantity * batch
                have_inv = next((i.quantity for i in character.inventory if i.code == ing.code), 0)
                shortfall = needed_qty - have_inv
                if shortfall > 0:
                    bank_qty = next((i.quantity for i in self.account.bank.items if i.code == ing.code), 0)
                    withdraw_qty = min(shortfall, bank_qty)
                    if withdraw_qty > 0:
                        to_withdraw.append({"code": ing.code, "quantity": withdraw_qty})
            if to_withdraw:
                await character.actions.withdraw_items(to_withdraw)
                await self.account.sync_bank()

        await character.actions.craft(order.code, quantity=batch, workshop=order.skill, map_db=self.db.maps)

        # Deposit after every crafting batch (not just when the inventory
        # is completely full or the order finishes) so freshly-crafted
        # output -- and any leftover ingredients -- lands in the bank
        # immediately, where other characters/orders relying on it can see
        # it via held().
        if not character.is_inventory_empty:
            await character.actions.deposit_all()
            await self.account.sync_bank()

        if self.held(order.code) >= order.target_quantity:
            self.complete(order)
            await self._try_deliver_equipment(character, order)

    async def character_loop(self, character) -> None:
        while self.running:
            order = self.select_order_for(character)
            await self._switch_task(character, order)

            if order is None:
                await asyncio.sleep(self.poll_interval)
                continue

            try:
                if order.kind == OrderKind.GATHER:
                    await self._run_gather_step(character, order)
                else:
                    await self._run_craft_step(character, order)
            except Exception as e:
                print(f"[{character.name}] Error running order #{order.id} '{order.code}': {e!r}")
                await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Requirement #4, 'Clean Slate': every character deposits gold and
        inventory into the bank before the scheduler starts handing out work."""
        for character in self.account.characters.values():
            if not character.is_inventory_empty:
                await character.actions.deposit_all()
            if character.gold > 0:
                await character.actions.deposit_gold(character.gold)
        await self.account.sync_bank()

    async def run(self) -> None:
        self.assign_default_gather_tasks()
        self.refresh_stock_orders()
        self.verify()
        self.print_plan_tree()

        self.running = True
        loops = [self.character_loop(c) for c in self.account.characters.values()]
        await asyncio.gather(*loops)

    def stop(self) -> None:
        self.running = False
