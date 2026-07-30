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

Priority tiers (orders.Priority): EQUIP > CRAFT > GATHER > KEEP_STOCK >
AUTO_CRAFT > DEFAULT. EQUIP is reserved for equip requests
(TaskEngine.request_equipment) and outranks everything else -- including
CRAFT plus the inertia bonus below -- so a character waiting on gear
preempts whatever they're currently doing rather than finishing it first.
AUTO_CRAFT sits just above DEFAULT busywork: it auto-converts surplus of a
"pure" single-use default-gathered raw material (one that's only ever an
ingredient in exactly one recipe, e.g. copper_ore -> copper_bar) into its
finished item, without ever eating into that raw material's keep-in-stock
floor -- see TaskEngine.refresh_auto_convert_orders(). See orders.INERTIA_BONUS
for the anti-thrashing bias applied to whatever order a character is
currently working; DEFAULT-tier orders get none, so they're preempted
immediately by anything else (zero inertia).

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
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from account import Account
from database import GameDatabase
from models import Item
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
        # Path most recently passed to load_stock_rules_from_file(), if any --
        # remembered so _stock_config_loop can keep re-reading the same file
        # on a timer without the caller having to pass the path twice.
        self._stock_config_path: Optional[str] = None
        self.default_orders: Dict[str, WorkOrder] = {}   # character name -> its DEFAULT-tier order

        self._current_order: Dict[str, Optional[int]] = {}  # character name -> order id they're on
        self.running = False

        # code -> the single Item that consumes it, for every default-gathered
        # raw material that's an ingredient in exactly one recipe. Computed
        # once (lazily, from the full item catalog) and cached -- see
        # _build_single_use_conversions()/refresh_auto_convert_orders().
        self._single_use_conversions: Optional[Dict[str, Item]] = None

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
        """
        if quantity <= 0:
            return None

        existing = self._order_for_code(code)
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
        bank_qty = next((i.quantity for i in self.account.bank.items if i.code == code), 0)
        equip_requests = [(requester, equip_slot)] * quantity if requester and equip_slot else []

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
                equip_requests=equip_requests,
            )
            self.orders[order.id] = order
            if bank_qty >= quantity:
                self.complete(order)
            else:
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
                equip_requests=equip_requests,
            )
            self.orders[order.id] = order
            if bank_qty >= quantity:
                self.complete(order)
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
        see _try_deliver_equipment(). Forces Priority.EQUIP across the
        entire craft/gather expansion (the top-level order for `code` and
        every recursive ingredient order request_item() spins up for it),
        not just the top-level order -- an equip request is meant to
        interrupt whatever a character is currently doing, and that only
        works end-to-end if the ingredient-gathering/crafting steps feeding
        it also outrank ordinary CRAFT/GATHER work along the way, not just
        the final craft step."""
        return self.request_item(code, quantity, tier=Priority.EQUIP, requester=character_name, equip_slot=slot)

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

    def load_stock_rules_from_file(self, path: str = "stock_config.json") -> None:
        """Reads a JSON object of {item_code: minimum_quantity} pairs from
        `path` and REPLACES self.stock_rules with them -- lets keep-in-stock
        targets be tuned by editing a file instead of hardcoding
        add_stock_rule() calls in main.py. Full replacement (rather than
        merging/appending) makes this idempotent and safe to call
        repeatedly: re-running it after the file changes picks up
        additions, edits, AND removals in one shot, rather than only ever
        accumulating stale rules for entries someone deleted from the file.
        Remembers `path` on self._stock_config_path so _stock_config_loop
        can keep reloading it periodically without the caller re-passing it.

        A missing file is a no-op (first run without a config file present
        is fine -- keep-in-stock is opt-in). A malformed file (bad JSON, not
        an object, or a non-int/negative minimum for some code) logs a
        warning and skips only the bad entries -- never crashes the
        scheduler over a typo in a hand-edited file.
        """
        self._stock_config_path = path
        p = Path(path)
        if not p.exists():
            return

        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[TaskEngine] Failed to read stock config '{path}': {e!r}")
            return

        if not isinstance(raw, dict):
            print(f"[TaskEngine] Stock config '{path}' must be a JSON object of "
                  f"{{item_code: minimum}} pairs -- skipping.")
            return

        new_rules: List[StockRule] = []
        for code, minimum in raw.items():
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
                print(f"[TaskEngine] Skipping invalid stock rule '{code}': {minimum!r} "
                      f"(must be a non-negative integer).")
                continue
            new_rules.append(StockRule(code=code, minimum=minimum))

        self.stock_rules = new_rules

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
    # Auto-convert (single-use default-gathered raw materials)
    # ------------------------------------------------------------------

    def _build_single_use_conversions(self) -> Dict[str, Item]:
        """One-time (cached) scan of the full item catalog, mapping each
        default-gathered raw material's code to the single Item that
        consumes it as a craft ingredient -- but ONLY for raw materials used
        by exactly one recipe (e.g. copper_ore -> copper_bar, raw_chicken ->
        cooked_chicken). A raw material feeding two or more different items
        isn't a 'pure' 1:1 conversion, so it's deliberately excluded: we
        wouldn't know which of its consumers to auto-craft into. Scanning
        every item is only done once per run (cached on
        self._single_use_conversions) rather than on every
        refresh_auto_convert_orders() tick."""
        raw_codes = {order.code for order in self.default_orders.values()}
        consumers: Dict[str, List[Item]] = {}
        for item in self.db.items.get_all_items_obj():
            if not item.craft:
                continue
            for ing in item.craft.items:
                if ing.code in raw_codes:
                    consumers.setdefault(ing.code, []).append(item)
        return {code: matches[0] for code, matches in consumers.items() if len(matches) == 1}

    def refresh_auto_convert_orders(self) -> None:
        """Priority.AUTO_CRAFT tier (above DEFAULT busywork, below every real
        KEEP_STOCK/GATHER/CRAFT/EQUIP request -- see orders.Priority). For
        every 'pure' single-use default-gathered raw material (see
        _build_single_use_conversions), automatically queues a craft order
        to convert whatever SURPLUS currently sits above that raw material's
        keep-in-stock floor into the finished item -- e.g. only turning
        copper_ore into copper_bar once there's more copper_ore on hand than
        we've committed to keeping in stock.

        The floor is the matching StockRule.minimum if one's been
        registered via add_stock_rule() for that raw material's code,
        otherwise 100 (per spec: 'or 100 if that isn't set'). Never dips the
        raw material below that floor, and never creates a second order for
        the same target item while one is already live (whatever tier it's
        at) -- avoids re-bumping a target based on ore that's already been
        earmarked for the in-flight order but hasn't been consumed
        (crafted) yet, which would otherwise look like fresh surplus on
        every tick and cause runaway over-ordering. Call periodically (see
        TaskEngine._auto_convert_loop) -- once an order completes and the
        raw material's held() total actually drops, the next tick will
        correctly see less (or no) surplus."""
        if self._single_use_conversions is None:
            self._single_use_conversions = self._build_single_use_conversions()

        for raw_code, target_item in self._single_use_conversions.items():
            if self._order_for_code(target_item.code):
                continue  # a conversion (or some other request) for this item is already in flight

            rule = next((r for r in self.stock_rules if r.code == raw_code), None)
            floor = rule.minimum if rule else 100

            spare = self.held(raw_code) - floor
            if spare <= 0:
                continue

            ingredient = next((i for i in target_item.craft.items if i.code == raw_code), None)
            if not ingredient or ingredient.quantity <= 0:
                continue

            craftable = spare // ingredient.quantity
            if craftable <= 0:
                continue

            produced = craftable * max(1, target_item.craft.quantity)
            self.request_item(target_item.code, produced, tier=Priority.AUTO_CRAFT)

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

    def _available_for_craft(self, character, code: str) -> int:
        """Amount of `code` `character` can actually draw on for crafting
        right now: what's already sitting in their own inventory, plus
        whatever's in the bank (withdrawable by anyone). Deliberately
        excludes every OTHER character's inventory -- held() counts those
        too, but this character has no way to pull materials out of a
        teammate's hands, only out of the bank once that teammate deposits.
        Using held() here was letting the scheduler assign/size craft work
        based on ingredients that were still in transit on someone else."""
        own_inventory = next((i.quantity for i in character.inventory if i.code == code), 0)
        bank_qty = next((i.quantity for i in self.account.bank.items if i.code == code), 0)
        return own_inventory + bank_qty

    def _materials_available(self, character, order: WorkOrder) -> bool:
        """Requirement #1: only assign a craft order to `character` once
        every ingredient is currently available to THEM specifically --
        their own inventory plus the bank, not the roster's total holdings."""
        item = self.db.items.get_item_obj(order.code)
        if not item or not item.craft:
            return True
        return all(
            self._available_for_craft(character, ing.code) >= ing.quantity
            for ing in item.craft.items
        )

    def select_order_for(self, character) -> Optional[WorkOrder]:
        current_id = self._current_order.get(character.name)
        current = self.orders.get(current_id) if current_id else None

        best, best_score = None, float("-inf")

        # Inertia only applies while `current` is still actually workable --
        # previously this branch skipped the target/materials checks applied
        # to every other order below, so once a craft order's ingredients ran
        # dry the character stayed locked onto it via inertia forever
        # (character_eligible alone doesn't catch that; it only checks
        # skill level / lock ownership, not live material stock). That was
        # why crafting characters got stuck spinning on one dead order
        # instead of falling through to other craftable/gatherable work.
        current_still_workable = (
            current is not None
            and not current.done
            and self.character_eligible(character, current)
            and current.target_quantity > self.held(current.code)
            and (current.kind != OrderKind.CRAFT or self._materials_available(character, current))
        )
        if current_still_workable:
            inertia = 0 if current.priority == Priority.DEFAULT else INERTIA_BONUS
            best, best_score = current, self._score(character, current) + inertia

        for order in self.orders.values():
            if order is current or not self.character_eligible(character, order):
                continue
            if order.target_quantity <= self.held(order.code):
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

        # Claim immediately, before any await below -- this is what select_order_for()
        # relies on to make order.locked_to trustworthy. select_order_for() (sync) and
        # this claim() call happen back-to-back on the same event-loop tick with nothing
        # in between that can yield, so no other character's coroutine can run its own
        # select_order_for() in the gap and see this order as still unclaimed. Claiming
        # used to happen AFTER the deposit await below, which meant two (or more)
        # characters could each pass the "not locked" check in select_order_for() before
        # either of them actually set locked_to -- both would then proceed to work the
        # same CRAFT order concurrently (duplicate withdraws/crafts against the same
        # materials). Moving claim() up here closes that window.
        if new_order:
            self.claim(character, new_order)

        if old_order and not character.is_inventory_empty:
            # Requirement #4: deposit inventory whenever switching tasks. No
            # need to walk back afterward -- the character is already
            # claimed onto the new order (or idle) above, so returning to
            # the pre-deposit tile would just be an extra trip for nothing.
            await character.actions.deposit_all(return_to_origin=False)
            await self.account.sync_bank()

    async def _try_deliver_equipment(self, order: WorkOrder) -> None:
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

        Called both right after an order completes (best-effort, immediate)
        and on every tick of _delivery_loop (so recipients who couldn't be
        served at completion time -- e.g. only 1 of 5 requested copies had
        actually landed in the bank yet -- still get theirs once the rest
        of the order's crafting/gathering catches up)."""
        map_db = self.db.maps

        while order.equip_requests:
            bank_qty = next((i.quantity for i in self.account.bank.items if i.code == order.code), 0)
            if bank_qty <= 0:
                break

            char_name, slot = order.equip_requests[0]
            requester = self.account.characters.get(char_name)
            if not requester:
                order.equip_requests.pop(0)
                continue

            # busy_lock serializes this whole move-unequip-deposit-withdraw-
            # equip sequence against `requester`'s own character_loop
            # iteration (see Character.busy_lock) -- without it, this
            # delivery (running from the separate _delivery_loop task)
            # could interleave its moves with a gather/craft step the
            # requester's own loop is mid-way through, leaving them
            # standing somewhere neither side expects (spurious 598/490
            # errors).
            async with requester.busy_lock:
                bank_pos = requester.actions.get_closest_bank(map_db)
                if not bank_pos:
                    print(f"[{requester.name}] Could not resolve a bank to deliver '{order.code}'.")
                    break
                await requester.actions.smart_move(bank_pos, map_db=map_db)

                # Unequip whatever's currently in that slot (if anything)
                # and deposit it, so the slot is free before we try to put
                # the new item on -- and the old gear ends up back in the
                # bank rather than just sitting unequipped in inventory.
                # Stacked utility slots track their held quantity in a
                # matching `<slot>_quantity` attribute; other slots don't
                # have one, so getattr falls back to 1 (a single equipped
                # item, the normal case).
                old_code = getattr(requester.equipment, slot, "") or ""
                if old_code:
                    old_qty = getattr(requester.equipment, f"{slot}_quantity", 1) or 1
                    await requester.actions.unequip(slot, quantity=old_qty)
                    await requester.actions._execute_deposit([{"code": old_code, "quantity": old_qty}])

                await requester.actions._execute_withdraw_items([{"code": order.code, "quantity": 1}])
                await self.account.sync_bank()
                await requester.actions.equip(order.code, slot)

            if old_code:
                print(f"[{requester.name}] Unequipped '{old_code}' from {slot} to the bank, "
                      f"then equipped '{order.code}'.")
            else:
                print(f"[{requester.name}] Equipped '{order.code}' in {slot}.")
            order.equip_requests.pop(0)

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
            await self._try_deliver_equipment(order)

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

        Materials availability is checked via _available_for_craft (own
        inventory + bank only) rather than held() (which also counts every
        other character's inventory) -- a craft can only ever pull from the
        bank or from what's already in this character's hands, never from a
        teammate who hasn't deposited yet. Returns 0 (not a forced minimum
        of 1) if even a single craft's worth isn't actually available, so
        the caller can skip the craft attempt instead of issuing an action
        that's guaranteed to fail with a missing-materials error."""
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
            available = self._available_for_craft(character, ing.code)
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
        remaining = order.target_quantity - self.held(order.code)
        crafts_needed = max(1, -(-remaining // order.produces_per_action))

        item = self.db.items.get_item_obj(order.code)
        batch = crafts_needed

        map_db = self.db.maps
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
                self.release(character, order)
                return
            if batch < crafts_needed:
                print(f"[{character.name}] Sizing '{order.code}' craft batch to {batch}/{crafts_needed} "
                      f"actions to fit available inventory/materials.")

            for ing in item.craft.items:
                needed_qty = ing.quantity * batch
                have_inv = next((i.quantity for i in character.inventory if i.code == ing.code), 0)
                shortfall = needed_qty - have_inv
                if shortfall > 0:
                    bank_qty = next((i.quantity for i in self.account.bank.items if i.code == ing.code), 0)
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
                await self.account.sync_bank()
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
                await self.account.sync_bank()

        if self.held(order.code) >= order.target_quantity:
            self.complete(order)
            await self._try_deliver_equipment(order)

    async def character_loop(self, character) -> None:
        while self.running:
            order = self.select_order_for(character)

            # Holds busy_lock for the whole switch+act cycle so that a
            # concurrent _try_deliver_equipment() call targeting this same
            # character (from _delivery_loop, a separate task) can't
            # interleave its own moves in between this loop's move-then-act
            # steps. See Character.busy_lock for why this can't just reuse
            # action_lock.
            async with character.busy_lock:
                await self._switch_task(character, order)

                if order is None:
                    do_sleep = True
                else:
                    do_sleep = False
                    try:
                        if order.kind == OrderKind.GATHER:
                            await self._run_gather_step(character, order)
                        else:
                            await self._run_craft_step(character, order)
                    except Exception as e:
                        print(f"[{character.name}] Error running order #{order.id} '{order.code}': {e!r}")
                        do_sleep = True

            if do_sleep:
                await asyncio.sleep(self.poll_interval)

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
                await character.actions.deposit_all(return_to_origin=False)
            if character.gold > 0:
                await character.actions.deposit_gold(character.gold, return_to_origin=False)

        await asyncio.gather(*(_clean_slate(c) for c in self.account.characters.values()))
        await self.account.sync_bank()

    async def _auto_convert_loop(self) -> None:
        """Periodically re-runs refresh_auto_convert_orders() so newly
        accumulated surplus of single-use gathered raw materials keeps
        getting picked up over time -- a single startup call (like
        refresh_stock_orders() gets) would only ever see whatever surplus
        happened to exist the instant the engine started, which is
        typically none."""
        while self.running:
            try:
                self.refresh_auto_convert_orders()
            except Exception as e:
                # Mirrors _delivery_loop's per-tick guard: one bad tick must
                # not propagate out of asyncio.gather() in run() and tear
                # down every character's loop along with it.
                print(f"[TaskEngine] Error refreshing auto-convert orders: {e!r}")
            await asyncio.sleep(self.poll_interval)

    async def _stock_config_loop(self) -> None:
        """Periodically re-reads the file passed to load_stock_rules_from_file
        (self._stock_config_path) and re-runs refresh_stock_orders(), so
        edits to that file -- adding, retuning, or removing a keep-in-stock
        target -- take effect while the engine is running instead of only
        at startup. No-ops (just sleeps) if load_stock_rules_from_file was
        never called, so it's harmless to always include this loop in
        run() regardless of whether file-backed stock rules are in use.
        Polls far less often than the per-character action loop (file edits
        are rare; no need to hit disk every poll_interval tick)."""
        interval = max(self.poll_interval * 10, 10.0)
        while self.running:
            if self._stock_config_path:
                try:
                    self.load_stock_rules_from_file(self._stock_config_path)
                    self.refresh_stock_orders()
                except Exception as e:
                    # Mirrors _auto_convert_loop/_delivery_loop's per-tick
                    # guard: one bad reload must not propagate out of
                    # asyncio.gather() in run() and tear down every
                    # character's loop along with it.
                    print(f"[TaskEngine] Error reloading stock config: {e!r}")
            await asyncio.sleep(interval)

    async def _delivery_loop(self) -> None:
        """Periodically sweeps every order for pending equip_requests and
        delivers/equips whatever the bank can currently supply. Decoupled
        from order completion because a single completion event only
        catches whatever's deliverable at that instant -- with multiple
        recipients queued on one order (e.g. 5 characters all wanting a
        copper_boots upgrade), the rest may still be mid-craft or waiting
        on a bank sync at that moment. Previously a 'done' order was never
        revisited, so any recipients not served at that exact instant just
        sat undelivered in the bank forever."""
        while self.running:
            for order in list(self.orders.values()):
                if order.equip_requests:
                    try:
                        await self._try_deliver_equipment(order)
                    except Exception as e:
                        # Mirrors character_loop's per-order guard: one failed
                        # delivery (e.g. a bank/API hiccup) must not propagate
                        # out of asyncio.gather() in run() and tear down every
                        # other character's loop along with it.
                        print(f"[TaskEngine] Error delivering equipment for order #{order.id} "
                              f"'{order.code}': {e!r}")
            await asyncio.sleep(self.poll_interval)

    async def run(self) -> None:
        self.assign_default_gather_tasks()
        self.refresh_stock_orders()
        self.refresh_auto_convert_orders()
        self.verify()
        self.print_plan_tree()

        self.running = True
        loops = [self.character_loop(c) for c in self.account.characters.values()]
        loops.append(self._delivery_loop())
        loops.append(self._auto_convert_loop())
        loops.append(self._stock_config_loop())
        await asyncio.gather(*loops)

    def stop(self) -> None:
        self.running = False
